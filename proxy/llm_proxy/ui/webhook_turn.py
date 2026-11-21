"""
Le TOUR d'un message reçu par un webhook entrant (webhook_routes.py),
mené par le proxy lui-même, sans navigateur :

  1. la conversation du webhook (une nouvelle, ou toujours la même) reçoit
     le message, marqué `webhook`, et une réponse « en cours »
     (`serverTurn`) : la page affiche les deux. En PASSE PLAT (`target`
     « none »), rien n'est rangé : le modèle ne reçoit que ce message, et
     sa réponse ne va qu'aux sortants ;
  2. le modèle répond : /ui/chat/completions appelé EN INTERNE, au nom
     du compte qui a créé le webhook (ses statistiques, sa part du partage
     équitable, sa mémoire d'outils), avec ses outils HÉBERGÉS seulement
     — ceux de la page (navigateur, appareil) n'existent pas ici ;
  3. la réponse prend la place de celle « en cours » (une erreur aussi :
     rien n'y reste), puis part vers le webhook sortant
     de l'entrant (`reply`) et vers le `response_url` du message, s'il en
     donne un (les commandes slash de Slack).

Le modèle ne reçoit que le MESSAGE REÇU, précédé du prompt système (celui
du webhook s'il en a un, sinon celui de la conversation) : jamais les tours
précédents de la conversation, même « toujours la même ». Chaque message
est une demande à part ; la conversation sert à les ranger et à les
relire dans la page (qui, elle, reprend tout l'historique si l'on y écrit).

Deux messages d'un même webhook « toujours la même conversation » sont
rangés l'un après l'autre. Les enregistrements suivent la révision de la
conversation (conversations.py) : un autre onglet qui l'a changée
entre-temps ne perd rien, le tour relit et ajoute à la suite.
"""

import asyncio
import json
import time
import uuid

from fastapi import Request
from fastapi.responses import StreamingResponse

from ..access import accounts, fairshare, oidc
from ..access.auth import account_owner, auth_required, toml_digests
from ..core.settings import INCOGNITO, log
from ..storage import conversations, stats, webhooks
from ..surfaces import chat_api
from ..surfaces.endpoints import chat
from ..tools import webhook as slack
from ..tools.contract import ToolError
from .dashboard import UI_CHAT_MODEL, chat_tool_groups

ROUTE = "/ui/chat/completions"
SAVE_TRIES = 5
# Un verrou par webhook « toujours la même conversation » ; retiré quand le
# webhook est supprimé (forget).
_locks: dict[tuple[str, str], asyncio.Lock] = {}


def _uid() -> str:
    return uuid.uuid4().hex[:16]


def _now() -> int:
    return round(time.time() * 1000)


# ── le compte au nom duquel le proxy répond ─────────────────────────────

def still_allowed(owner: str, hook: dict) -> bool:
    """Le créateur du webhook peut-il encore dépenser ? Compte actif, clé
    du TOML encore là ; un proxy devenu fermé refuse ce qui a été créé
    ouvert. BLOQUANT."""
    if not auth_required():
        return True
    if hook.get("account") is not None:
        user = accounts.user(hook["account"])
        return user is not None and user["role"] in accounts.ACTIVE \
            and account_owner(user["id"]) == owner
    if not owner or owner not in toml_digests():
        return False
    # Une clé du TOML rattachée depuis à un compte suit son état (auth.key_client).
    link = accounts.bound(owner) if oidc.ENABLED else None
    return link is None or link["active"]


def _identify(owner: str, hook: dict, request: Request) -> None:
    """Ce que le middleware poserait (auth.identify) pour le créateur."""
    request.state.owner = owner
    if auth_required():
        stats.CLIENT.set(hook.get("api_key_id") or owner)
        fairshare.CLIENT.set(owner)
        fairshare.ACCOUNT.set(hook.get("account"))


def _request(body: bytes) -> Request:
    """Une requête du chat de la page, fabriquée : le corps, puis un client
    qui ne raccroche jamais (relaying.drain attend sa déconnexion)."""
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Event().wait()
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "POST", "scheme": "http", "path": ROUTE,
             "raw_path": ROUTE.encode(), "root_path": "", "query_string": b"",
             "headers": [(b"content-type", b"application/json"),
                         (b"user-agent", slack.USER_AGENT.encode())],
             "client": ("127.0.0.1", 0), "server": ("webhook", 80), "state": {}}
    return Request(scope, receive)


async def complete(owner: str, hook: dict, body: dict) -> dict:
    """Le corps chat/completions envoyé comme par la page → {content,
    reasoning, usage, sources, error}."""
    request = _request(json.dumps(body, ensure_ascii=False).encode())
    _identify(owner, hook, request)
    response = await chat(request, page=True)
    if isinstance(response, StreamingResponse):
        raw = b"".join([c if isinstance(c, bytes) else c.encode()
                        async for c in response.body_iterator])
    else:
        raw = bytes(response.body or b"")
    if response.background is not None:
        await response.background()
    try:
        j = json.loads(raw)
    except ValueError:
        j = None
    if response.status_code >= 400 or not isinstance(j, dict) or "choices" not in j:
        err = j.get("error") if isinstance(j, dict) else None
        message = (err.get("message") if isinstance(err, dict) else err) or \
            f"HTTP {response.status_code}"
        return {"error": str(message)}
    choice = (j.get("choices") or [{}])[0] or {}
    msg = choice.get("message") or {}
    sources = []
    for a in msg.get("annotations") or []:
        cite = a.get("url_citation") if isinstance(a, dict) else None
        if isinstance(cite, dict) and cite.get("url") \
                and not any(s["url"] == cite["url"] for s in sources):
            sources.append({"url": cite["url"], "title": cite.get("title") or cite["url"]})
    return {"content": msg.get("content") or "",
            "reasoning": msg.get("reasoning_content") or msg.get("reasoning") or "",
            "usage": j.get("usage"), "sources": sources,
            "cut": "length" if choice.get("finish_reason") == "length" else "",
            "error": ""}


# ── la conversation ─────────────────────────────────────────────────────

def new_doc(hook: dict, cid: str, text: str, model: str) -> dict:
    """Une conversation neuve, comme ChatStore.newConv la ferait."""
    first = " ".join(text.split())
    now = _now()
    return {"id": cid, "title": (f"{hook['name']} · {first[:50]}"
                                 + ("…" if len(first) > 50 else "")),
            "created": now, "updated": now, "model": model,
            "system": hook.get("system") or "", "temperature": None,
            "showReasoning": True, "tools": list(hook.get("tools") or []),
            "reasoning": "auto", "imageCount": 1, "imageSteps": 0,
            "imageLora": "", "imageAlone": False, "incognito": False,
            "webhook": hook["id"], "messages": []}


def _append(owner: str, cid: str, fresh, added: list[dict]) -> dict:
    """Ajoute `added` à la fin de la conversation (créée par `fresh()` si
    elle n'existe pas), sur sa dernière révision. → le document enregistré.
    BLOQUANT."""
    for _ in range(SAVE_TRIES):
        doc = conversations.get(owner, cid)
        base = 0
        if doc is None:
            doc = fresh()
        else:
            base = doc.pop("rev", 0)
        doc.setdefault("messages", []).extend(added)
        doc["updated"] = _now()
        try:
            conversations.save(owner, cid, doc, base)
            return doc
        except conversations.Conflict:
            continue
    raise conversations.HistoryError("conversation modifiée sans cesse ailleurs", 409)


def _replace(owner: str, cid: str, question: str, message: dict) -> bool:
    """La réponse « en cours » (même id que `message`) remplacée, sur la
    dernière révision. Retirée entre-temps, la réponse est ajoutée à la
    fin — sauf si sa QUESTION a disparu aussi (message modifié, réponse
    écrasée par la page) : rien n'est ajouté. → faux si rien n'est rangé
    (conversation supprimée, question retirée). BLOQUANT."""
    for _ in range(SAVE_TRIES):
        doc = conversations.get(owner, cid)
        if doc is None:
            return False
        base = doc.pop("rev", 0)
        msgs = doc.setdefault("messages", [])
        ids = [m.get("id") if isinstance(m, dict) else None for m in msgs]
        if message["id"] in ids:
            msgs[ids.index(message["id"])] = message
        elif question in ids:
            msgs.append(message)
        else:
            return False
        doc["updated"] = _now()
        try:
            conversations.save(owner, cid, doc, base)
            return True
        except conversations.Conflict:
            continue
    raise conversations.HistoryError("conversation modifiée sans cesse ailleurs", 409)


def _messages(hook: dict, doc: dict | None, text: str) -> list[dict]:
    """L'échange envoyé au modèle : le prompt système (du webhook, sinon de
    la conversation) et le seul message reçu — jamais l'historique."""
    system = (hook.get("system") or (doc or {}).get("system") or "").strip()
    return ([{"role": "system", "content": system}] if system else []) \
        + [{"role": "user", "content": text}]


def _pick_model(hook: dict, asked: str, doc: dict | None) -> str:
    if asked and hook.get("model_override"):
        return asked
    return hook.get("model") or (doc or {}).get("model") or UI_CHAT_MODEL or asked


def declarable(wanted) -> list[str]:
    """Les types d'outils à déclarer, comme la page le ferait : ceux des
    outils hébergés ACTIFS du chat (dashboard.chat_tool_groups), rien si le
    chat n'en présente pas ([chat].hosted_tools) — un outil déclaré mais
    inactif vaudrait un 400."""
    if not chat_api.ENABLED:
        return []
    active = {g["type"] for g in chat_tool_groups()}
    return [t for t in wanted if isinstance(t, str) and t in active]


def _body(doc: dict, model: str, messages: list[dict], tool_types: list[str]) -> dict:
    body = {"model": model, "stream": False, "messages": messages}
    temperature = doc.get("temperature")
    if temperature not in (None, ""):
        try:
            body["temperature"] = float(temperature)
        except (TypeError, ValueError):
            pass
    effort = doc.get("reasoning")
    if effort in ("low", "medium", "high"):
        body["reasoning_effort"] = effort
    elif effort == "none":
        body["chat_template_kwargs"] = {"enable_thinking": False}
    if tool_types:
        body["tools"] = [{"type": t} for t in tool_types]
    return body


NO_MODEL = "aucun modèle : le webhook n'en nomme pas, ni [ui].chat_model"


async def _ask(owner: str, hook: dict, model: str, body) -> dict:
    """La réponse du modèle (complete), ou {"error"} : pas de modèle, panne.
    `body` : une fonction qui fabrique le corps (elle peut lever, la panne
    est dite pareil)."""
    if not model:
        return {"error": NO_MODEL}
    try:
        return await complete(owner, hook, await body())
    except Exception as exc:
        log.exception("webhook %s : appel du modèle en échec", hook["id"])
        return {"error": f"panne : {exc}"}


async def handle(owner: str, hook: dict, text: str, meta: dict) -> None:
    """Le tour entier d'un message reçu. `meta` : `model` (demandé par le
    corps), `user` (qui l'a écrit, côté Slack), `response_url`,
    `thread_ts`. Ne lève pas : une erreur est gardée sur le webhook, dans
    la réponse de la conversation, et envoyée aux sortants."""
    key = (owner, hook["id"])
    lock = _locks.setdefault(key, asyncio.Lock()) if hook["target"] == "conversation" else None
    try:
        if lock is not None:
            async with lock:
                await _turn(owner, hook, text, meta)
        else:
            await _turn(owner, hook, text, meta)
    except Exception as exc:          # dernier filet (la base, un envoi) : rien ne remonte
        log.exception("webhook %s : panne hors du tour", hook["id"])
        await asyncio.to_thread(webhooks.used_in, owner, hook["id"], f"panne : {exc!r}")


def forget(owner: str, wid: str) -> None:
    """Un webhook supprimé : son verrou ne sert plus."""
    _locks.pop((owner, wid), None)


async def _turn(owner: str, hook: dict, text: str, meta: dict) -> None:
    # Relu : une modification ou une suppression depuis la réception compte.
    hook = await asyncio.to_thread(webhooks.get_in, owner, hook["id"])
    if hook is None:
        return
    if not await asyncio.to_thread(still_allowed, owner, hook):
        await asyncio.to_thread(webhooks.used_in, owner, hook["id"],
                                "compte bloqué ou clé retirée : message ignoré")
        return
    if hook["target"] == "none" and not hook.get("reply") and not meta.get("response_url"):
        # Un passe plat dont le sortant a disparu : la réponse n'irait nulle
        # part, le modèle n'est pas appelé.
        await asyncio.to_thread(webhooks.used_in, owner, hook["id"],
                                "passe plat sans webhook sortant : message ignoré")
        return
    errors: list[str] = []
    try:
        reply = await (_passthrough(owner, hook, text, meta, errors) if hook["target"] == "none"
                       else _in_conversation(owner, hook, text, meta, errors))
    except Exception as exc:          # la question n'a pas pu être rangée
        log.exception("webhook %s : tour en échec", hook["id"])
        errors.append(f"panne : {exc}")
        reply = f"Erreur : {exc}"
    errors += await _deliver(owner, hook, meta, reply)
    await asyncio.to_thread(webhooks.used_in, owner, hook["id"], " ; ".join(errors))


async def _conversation(owner: str, hook: dict) -> tuple[str, dict | None, bool]:
    """(identifiant, document ou None, dédiée à créer) : une nouvelle à
    chaque message ; ou toujours la même — supprimée (ou jamais créée), une
    nouvelle, dédiée, que le webhook gardera."""
    if hook["target"] == "conversation" and hook["conversation"]:
        doc = await asyncio.to_thread(conversations.get, owner, hook["conversation"])
        if doc is not None:
            return hook["conversation"], doc, False
    return _uid(), None, hook["target"] == "conversation"


async def _in_conversation(owner: str, hook: dict, text: str, meta: dict,
                           errors: list[str]) -> str:
    """La question et une réponse « en cours » rangées, le modèle appelé, la
    réponse rangée à la place de celle « en cours » → le texte à envoyer."""
    cid, doc, dedicated = await _conversation(owner, hook)
    model = _pick_model(hook, meta.get("model") or "", doc)
    question = {"id": _uid(), "role": "user", "content": text, "attachments": [],
                "webhook": {"id": hook["id"], "name": hook["name"],
                            "from": meta.get("user") or "", "at": _now()}}
    started = _now()
    # La page affiche `serverTurn` comme une réponse en cours, menée par le
    # proxy ; _settle la remplace, quoi qu'il arrive ensuite.
    pending = {"id": _uid(), "role": "assistant", "content": "", "reasoning": "",
               "tools": [], "sources": [], "model": model, "usage": None, "error": "",
               "started": started, "duration": 0, "serverTurn": True,
               "webhook": {"id": hook["id"], "name": hook["name"]}}
    doc = await asyncio.to_thread(_append, owner, cid, lambda: new_doc(hook, cid, text, model),
                                  [question, pending])

    async def body():
        return _body(doc, model, _messages(hook, doc, text),
                     declarable(hook.get("tools") or doc.get("tools") or []))

    try:
        if dedicated:
            await asyncio.to_thread(webhooks.set_conversation, owner, hook["id"], cid)
        got = await _ask(owner, hook, model, body)
    except Exception as exc:                  # la base : la réponse en cours sera l'erreur
        log.exception("webhook %s : conversation dédiée non retenue", hook["id"])
        got = {"error": f"panne : {exc}"}
    answer = {**pending, "content": got.get("content") or "",
              "reasoning": got.get("reasoning") or "", "sources": got.get("sources") or [],
              "usage": got.get("usage"), "error": got.get("error") or "",
              # En secondes, comme la page (une durée nulle se lirait « en cours »).
              "duration": max(0.001, (_now() - started) / 1000),
              **({"cut": got["cut"]} if got.get("cut") else {})}
    del answer["serverTurn"]
    if answer["error"]:
        errors.append(answer["error"])
    await _settle(owner, cid, question["id"], answer, errors)
    return answer["content"] if not answer["error"] else f"Erreur : {answer['error']}"


async def _settle(owner: str, cid: str, question: str, answer: dict, errors: list[str]) -> None:
    """La réponse rangée à la place de celle « en cours ». Si elle ne peut pas
    l'être (trop grande, historique plein, conflits sans fin), une réponse
    courte en erreur la remplace : jamais de « en cours » laissé en base."""
    try:
        if not await asyncio.to_thread(_replace, owner, cid, question, answer):
            errors.append("conversation supprimée, ou question retirée, pendant la réponse")
        return
    except conversations.HistoryError as exc:
        errors.append(f"réponse non rangée : {exc}")
        short = {**answer, "content": "", "reasoning": "", "sources": [], "usage": None,
                 "error": f"réponse non rangée : {exc}"}
    try:
        await asyncio.to_thread(_replace, owner, cid, question, short)
    except conversations.HistoryError:
        log.warning("webhook : réponse en cours de %s non remplacée", cid)


async def _passthrough(owner: str, hook: dict, text: str, meta: dict,
                       errors: list[str]) -> str:
    """Le PASSE PLAT : rien n'est rangé. Le modèle reçoit le seul message
    (et le prompt système du webhook), sa réponse ne va qu'aux sortants. Le
    tour est INCOGNITO (settings.INCOGNITO) : rien de lui ne survit à la
    réponse — ni échange d'outils gardé, ni cache, ni dépôt dans un bac
    (tools/repository.py : un bac d'un seul appel)."""
    model = _pick_model(hook, meta.get("model") or "", None)

    async def body():
        return _body({}, model, _messages(hook, None, text), declarable(hook.get("tools") or []))

    token = INCOGNITO.set(True)
    try:
        got = await _ask(owner, hook, model, body)
    finally:
        INCOGNITO.reset(token)
    if got.get("error"):
        errors.append(got["error"])
        return f"Erreur : {got['error']}"
    return got.get("content") or ""


async def _deliver(owner: str, hook: dict, meta: dict, reply: str) -> list[str]:
    """La réponse envoyée au sortant de l'entrant et au `response_url` du
    message → les erreurs d'envoi."""
    errors = []
    if hook.get("reply"):
        out = await asyncio.to_thread(webhooks.get_out, owner, hook["reply"])
        if out is None:
            errors.append("webhook sortant de réponse introuvable")
        else:
            extra = {"thread_ts": meta["thread_ts"]} if meta.get("thread_ts") else None
            if (failed := await slack.post(owner, out, reply, extra)):
                errors.append(f"envoi à « {out['name']} » : {failed}")
    if meta.get("response_url"):
        try:
            # Une URL donnée par l'appelant, pas par le compte : jamais une
            # adresse privée, quel que soit [webhooks].allow_private.
            await slack.send(meta["response_url"], reply, {"response_type": "in_channel"},
                             allow_private=False)
        except ToolError as exc:
            errors.append(f"response_url : {exc.message}")
    return errors
