"""
Le TOUR d'un message reçu par un webhook entrant (webhook_routes.py),
mené par le proxy lui-même, sans navigateur :

  1. la conversation du webhook (une nouvelle, ou toujours la même) reçoit
     le message, marqué `webhook` — la page l'affiche comme tel ;
  2. le modèle répond : /ui/chat/completions appelé EN INTERNE, au nom
     du compte qui a créé le webhook (ses statistiques, sa part du partage
     équitable, sa mémoire d'outils), avec ses outils HÉBERGÉS seulement
     — ceux de la page (navigateur, appareil) n'existent pas ici ;
  3. la réponse rejoint la conversation, puis part vers le webhook sortant
     de l'entrant (`reply`) et vers le `response_url` du message, s'il en
     donne un (les commandes slash de Slack).

L'échange envoyé au modèle est celui que la page enverrait (chat/api.js,
apiMessages) : prompt système (celui du webhook s'il en a un, sinon celui
de la conversation), dernier résumé, messages et tours d'outils de la page,
fichiers joints, images de l'historique relues en data URL.

Deux messages d'un même webhook « toujours la même conversation » passent
l'un après l'autre : le second voit la réponse au premier. Les
enregistrements suivent la révision de la conversation (conversations.py) :
un autre onglet qui l'a changée entre-temps ne perd rien, le tour relit et
ajoute à la suite.
"""

import asyncio
import base64
import json
import time
import uuid

from fastapi import Request
from fastapi.responses import StreamingResponse

from ..access import accounts, fairshare, oidc
from ..access.auth import account_owner, auth_required, toml_digests
from ..core.settings import log
from ..storage import conversations, stats, webhooks
from ..surfaces import chat_api
from ..surfaces.endpoints import chat
from ..tools import webhook as slack
from ..tools.contract import ToolError
from .dashboard import UI_CHAT_MODEL, chat_tool_groups

ROUTE = "/ui/chat/completions"
SAVE_TRIES = 5
NOTHING = "Rien de particulier (plus d'artifact, de dossier ouvert…)."
# Un verrou par webhook « toujours la même conversation » (autant que de
# tels webhooks : bornés par [webhooks].max_per_user).
_locks: dict[tuple[str, str], asyncio.Lock] = {}


def _uid() -> str:
    return uuid.uuid4().hex[:16]


def _now() -> int:
    return round(time.time() * 1000)


# ── l'échange, comme la page l'enverrait (chat/api.js) ──────────────────

def _summary_done(m) -> bool:
    return isinstance(m, dict) and m.get("role") == "summary" and not m.get("running") \
        and not m.get("error") and bool(str(m.get("content") or "").strip())


def _preamble(m) -> str:
    return ("RÉSUMÉ DE LA CONVERSATION JUSQU'ICI (les messages antérieurs ne te sont "
            "plus envoyés ; ce résumé les remplace, tu peux t'y fier) :\n\n"
            + str(m.get("content") or "").strip())


def _content(m: dict, state: str = ""):
    """apiContent : texte seul, ou parties (texte, images)."""
    attachments = [a for a in m.get("attachments") or [] if isinstance(a, dict)]
    text = str(m.get("content") or "")
    for f in attachments:
        if f.get("kind") == "text":
            name = str(f.get("name") or "").replace('"', "'")
            text += f'\n\n<file name="{name}">\n{f.get("data") or ""}\n</file>'
    if state:
        text += f"\n\n[État du moment, ajouté par la page]\n{state}"
    images = [a for a in attachments if a.get("kind") == "image" and a.get("data")]
    if not images:
        return text
    return ([{"type": "text", "text": text}] if text else []) + [
        {"type": "image_url", "image_url": {"url": a["data"]}} for a in images]


def _steps(steps) -> list[dict]:
    """tools.stepMessages : les tours d'outils de la page, rejoués."""
    out = []
    for step in steps or []:
        calls = [c for c in step.get("calls") or [] if isinstance(c, dict)]
        out.append({"role": "assistant", "content": step.get("content") or None,
                    "tool_calls": [{"id": c.get("id"), "type": "function", "function": {
                        "name": c.get("name"), "arguments": c.get("arguments") or "{}"}}
                        for c in calls]})
        out += [{"role": "tool", "tool_call_id": c.get("id"), "content": c.get("result") or ""}
                for c in calls]
        images = [c for c in calls if c.get("image")]
        if images:
            out.append({"role": "user", "content": [
                {"type": "text", "text": "Image(s) rendue(s) par "
                 + ", ".join(str(c.get("name")) for c in images) + " :"},
                *({"type": "image_url", "image_url": {"url": c["image"]}} for c in images)]})
    return out


def api_messages(doc: dict, upto: int, system: str | None = None) -> list[dict]:
    """apiMessages : l'échange jusqu'au message `upto` (exclu). `system` :
    le prompt système du webhook, à la place de celui de la conversation."""
    messages = [m for m in doc.get("messages") or [] if isinstance(m, dict)]
    upto = min(upto, len(messages))
    start = next((i + 1 for i in range(upto - 1, -1, -1) if _summary_done(messages[i])), 0)
    head = (system if system is not None else str(doc.get("system") or "")).strip()
    system_text = "\n\n".join(x for x in (head, _preamble(messages[start - 1]) if start else "") if x)
    out = [{"role": "system", "content": system_text}] if system_text else []
    shown = ""
    for m in messages[start:upto]:
        role = m.get("role")
        if role == "summary":
            continue
        if role == "user":
            now = m["state"] if isinstance(m.get("state"), str) else shown
            joined = "" if now == shown else (now or NOTHING)
            shown = now
            out.append({"role": "user", "content": _content(m, joined)})
        elif m.get("steps"):
            out += _steps(m["steps"])
            if m.get("final"):
                out.append({"role": "assistant", "content": m["final"]})
        elif m.get("content"):
            out.append({"role": "assistant", "content": m["content"]})
        elif m.get("attachments"):
            out.append({"role": "assistant", "content": "(image générée : " + ", ".join(
                str(a.get("name")) for a in m["attachments"] if isinstance(a, dict)) + ")"})
    return out


def inline_assets(owner: str, messages: list[dict]) -> None:
    """Les images de l'historique (/ui/assets/<id>) en data URL, comme la
    page les envoie (history.inline) ; une image disparue est retirée.
    BLOQUANT (SQLite)."""
    for m in messages:
        parts = m.get("content")
        if not isinstance(parts, list):
            continue
        kept = []
        for p in parts:
            url = (p.get("image_url") or {}).get("url") if isinstance(p, dict) else None
            if isinstance(url, str) and url.startswith(conversations.ASSET_PATH):
                row = conversations.asset(owner, url[len(conversations.ASSET_PATH):])
                if row is None:
                    continue
                kind, _, data = row
                p = {"type": "image_url", "image_url": {
                    "url": f"data:{kind};base64,{base64.b64encode(data).decode()}"}}
            kept.append(p)
        m["content"] = kept


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


def _append(owner: str, cid: str, fresh: dict | None, added: list[dict]) -> dict | None:
    """Ajoute `added` à la fin de la conversation (créée d'après `fresh`
    si elle n'existe pas ; None : pas créée, rien n'est fait), sur sa
    dernière révision. → le document enregistré, None. BLOQUANT."""
    for _ in range(SAVE_TRIES):
        doc = conversations.get(owner, cid)
        base = 0
        if doc is None:
            if fresh is None:
                return None
            doc = json.loads(json.dumps(fresh))
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


async def handle(owner: str, hook: dict, text: str, meta: dict) -> None:
    """Le tour entier d'un message reçu. `meta` : `model` (demandé par le
    corps), `user` (qui l'a écrit, côté Slack), `response_url`,
    `thread_ts`. Ne lève pas : une erreur est gardée sur le webhook, dans
    la réponse de la conversation, et envoyée aux sortants — même quand
    le tour échoue en route (_turn)."""
    key = (owner, hook["id"])
    lock = _locks.setdefault(key, asyncio.Lock()) if hook["target"] == "conversation" else None
    try:
        if lock is not None:
            async with lock:
                await _turn(owner, hook, text, meta)
        else:
            await _turn(owner, hook, text, meta)
    except Exception as exc:          # dernier filet : rien ne remonte
        log.exception("webhook %s : tour en échec", hook["id"])
        await asyncio.to_thread(webhooks.used_in, owner, hook["id"], f"panne : {exc!r}")


async def _turn(owner: str, hook: dict, text: str, meta: dict) -> None:
    # Relu : une modification ou une suppression depuis la réception compte.
    hook = await asyncio.to_thread(webhooks.get_in, owner, hook["id"])
    if hook is None:
        return
    if not await asyncio.to_thread(still_allowed, owner, hook):
        await asyncio.to_thread(webhooks.used_in, owner, hook["id"],
                                "compte bloqué ou clé retirée : message ignoré")
        return
    cid = hook["conversation"] if hook["target"] == "conversation" else ""
    errors: list[str] = []
    try:
        reply = await _answer(owner, hook, cid, text, meta, errors)
    except Exception as exc:          # historique plein, conflits sans fin, panne
        log.exception("webhook %s : tour en échec", hook["id"])
        errors.append(f"panne : {exc}")
        reply = f"Erreur : {exc}"
    errors += await _deliver(owner, hook, meta, reply)
    await asyncio.to_thread(webhooks.used_in, owner, hook["id"], " ; ".join(errors))


async def _answer(owner: str, hook: dict, cid: str, text: str, meta: dict,
                  errors: list[str]) -> str:
    """La question rangée, le modèle appelé, la réponse rangée → le texte
    à envoyer (« Erreur : … » si le modèle a échoué)."""
    doc = await asyncio.to_thread(conversations.get, owner, cid) if cid else None
    if doc is None:
        dedicated = hook["target"] == "conversation" and not hook["conversation"]
        cid = cid or _uid()
    else:
        dedicated = False
    model = _pick_model(hook, meta.get("model") or "", doc)
    fresh = new_doc(hook, cid, text, model)
    question = {"id": _uid(), "role": "user", "content": text, "attachments": [],
                "webhook": {"id": hook["id"], "name": hook["name"],
                            "from": meta.get("user") or "", "at": _now()}}
    doc = await asyncio.to_thread(_append, owner, cid, fresh, [question])
    if dedicated:
        # Gardée tout de suite : un tour qui échoue ensuite ne la perd pas.
        await asyncio.to_thread(webhooks.set_conversation, owner, hook["id"], cid)

    started = _now()
    upto = next(i for i, m in enumerate(doc["messages"]) if m.get("id") == question["id"]) + 1
    messages = api_messages(doc, upto, hook.get("system") or None)
    await asyncio.to_thread(inline_assets, owner, messages)
    tool_types = declarable(hook.get("tools") or doc.get("tools") or [])
    if not model:
        got = {"error": "aucun modèle : le webhook n'en nomme pas, ni [ui].chat_model"}
    else:
        try:
            got = await complete(owner, hook, _body(doc, model, messages, tool_types))
        except Exception as exc:
            log.exception("webhook %s : appel du modèle en échec", hook["id"])
            got = {"error": f"panne : {exc}"}
    answer = {"id": _uid(), "role": "assistant", "content": got.get("content") or "",
              "reasoning": got.get("reasoning") or "", "tools": [],
              "sources": got.get("sources") or [], "model": model,
              "usage": got.get("usage"), "error": got.get("error") or "",
              "started": started,
              # En secondes, comme la page (une durée nulle se lirait « en cours »).
              "duration": max(0.001, (_now() - started) / 1000),
              "webhook": {"id": hook["id"], "name": hook["name"]}}
    if got.get("cut"):
        answer["cut"] = got["cut"]
    if answer["error"]:
        errors.append(answer["error"])
    try:
        # Supprimée dans la page pendant le tour : pas recréée pour la seule réponse.
        if await asyncio.to_thread(_append, owner, cid, None, [answer]) is None:
            errors.append("conversation supprimée pendant la réponse")
    except conversations.HistoryError as exc:
        errors.append(f"réponse non rangée : {exc}")
    return answer["content"] if not answer["error"] else f"Erreur : {answer['error']}"


async def _deliver(owner: str, hook: dict, meta: dict, reply: str) -> list[str]:
    """La réponse envoyée au sortant de l'entrant et au `response_url` du
    message → les erreurs d'envoi."""
    errors = []
    if hook.get("reply"):
        out = await asyncio.to_thread(webhooks.get_out, owner, hook["reply"])
        if out is not None:
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
