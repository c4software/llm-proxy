"""
Les WEBHOOKS (storage/webhooks.py) : la réception, et leur gestion par la
page. Actifs avec [webhooks].enabled ET [ui].chat (un message reçu va dans
le chat de la page).

  POST /hooks/{jeton}       un message reçu, SANS clé : le jeton vaut droit
                            d'accès. Comme un webhook entrant de Slack :
                            JSON {"text", "blocks", "attachments"}, ou
                            formulaire `payload=<JSON>`, ou les champs d'une
                            commande slash (`text`, `user_name`,
                            `response_url`). En plus : `model` (si le
                            webhook le permet), `thread_ts` (repris par la
                            réponse à un sortant JSON). Ou le corps natif
                            d'une forge (forge_events : GitHub, GitLab,
                            Gitea), reconnu à son en-tête d'événement. → 200 « ok » tout de suite ; la
                            réponse du modèle part ensuite vers le sortant
                            du webhook et le `response_url` (webhook_turn).
                            Erreurs comme Slack, en texte : 400
                            invalid_payload, no_text, msg_too_long,
                            model_not_found ; 403 action_prohibited ; 404
                            no_service ; 429 rate_limited.

Sous /ui, ceux du compte connecté (jamais d'un autre) ; écritures en JSON
(json_only) :

  GET    /ui/webhooks                     {incoming, outgoing, path (/hooks/),
                                          max (par compte)}
  POST   /ui/webhooks/in                  crée → {webhook, token}
  PUT    /ui/webhooks/in/{id}             modifie (tous les réglages)
  GET    /ui/webhooks/in/{id}/token       le jeton (l'adresse que la page
                                          réaffiche)
  POST   /ui/webhooks/in/{id}/rotate      nouveau jeton → {webhook, token}
  DELETE /ui/webhooks/in/{id}
  POST   /ui/webhooks/out                 crée {name, url, tool?}
  PUT    /ui/webhooks/out/{id}            modifie (url ou tool absents :
                                          inchangés)
  POST   /ui/webhooks/out/{id}/test       envoie un message d'essai
  DELETE /ui/webhooks/out/{id}            409 s'il sert à un passe plat
"""

import asyncio
import json
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from ..access import fairshare
from ..access.auth import HOOK_PREFIX, ui_client
from ..core import config
from ..core.window import Window
from ..core.httputil import error_response, json_only, parse_json
from ..core.settings import log
from ..storage import stats, webhooks
from ..tools import webhook as slack
from ..upstream.backends import route_backend
from . import dashboard, forge_events, webhook_turn
from .conversation_routes import run

router = APIRouter()

CALLS_PER_MINUTE = config.integer("webhooks.calls_per_minute", 30)
MAX_RUNNING = config.integer("webhooks.max_running", 3)
MAX_TEXT = config.integer("webhooks.max_text_chars", 100_000)
MAX_BODY = 1_000_000

RECENT = Window(60)                  # id d'un entrant → ses derniers messages
_running: dict[str, int] = {}        # client → tours en cours
_tasks: set[asyncio.Task] = set()    # gardés : une tâche sans référence peut disparaître


def enabled() -> bool:
    return slack.ENABLED and dashboard.UI_CHAT


def _slack(status: int, text: str) -> PlainTextResponse:
    return PlainTextResponse(text, status_code=status)


def _payload(raw: bytes, content_type: str) -> dict | None:
    """Le corps d'un message reçu, aux formes de Slack."""
    if "application/x-www-form-urlencoded" in content_type:
        try:
            form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8"),
                                                  keep_blank_values=True).items()}
        except UnicodeDecodeError:
            return None
        if "payload" in form:
            try:
                got = json.loads(form["payload"])
            except ValueError:
                return None
            return got if isinstance(got, dict) else None
        return form
    if content_type.startswith("text/plain"):
        try:
            return {"text": raw.decode("utf-8")}
        except UnicodeDecodeError:
            return None
    got = parse_json(raw)
    return got if isinstance(got, dict) else None


def _limited(wid: str) -> bool:
    return RECENT.wait(wid, CALLS_PER_MINUTE) > 0


def _start(owner: str, hook: dict, text: str, meta: dict) -> None:
    _running[owner] = _running.get(owner, 0) + 1

    async def run():
        try:
            await webhook_turn.handle(owner, hook, text, meta)
        finally:
            _running[owner] -= 1
            if not _running[owner]:
                _running.pop(owner, None)
    task = asyncio.create_task(run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


@router.post(HOOK_PREFIX + "{token}")
async def receive_hook(request: Request, token: str):
    if not enabled():
        return _slack(404, "no_service")
    found = await asyncio.to_thread(webhooks.by_token, token)
    if found is None:
        return _slack(404, "no_service")
    owner, hook = found
    # Un message posté par un proxy (un sortant pointé sur un entrant) : la
    # réponse en ferait un nouveau message, sans fin.
    if request.headers.get("user-agent", "") == slack.USER_AGENT:
        return _slack(403, "action_prohibited")
    try:
        if int(request.headers.get("content-length") or 0) > MAX_BODY:
            return _slack(400, "msg_too_long")
    except ValueError:
        return _slack(400, "invalid_payload")
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_BODY:
            return _slack(400, "msg_too_long")
    raw = bytes(raw)
    payload = _payload(raw, request.headers.get("content-type", "").lower())
    if payload is None:
        return _slack(400, "invalid_payload")
    author, scope = "", None
    try:
        native = forge_events.event(request.headers, payload)
        if native is None:
            text = slack.from_slack(payload)
        else:
            text, author, scope = native
            if not text:
                # Un essai (ping), ou un événement sur une branche du proxy.
                return _slack(200, "ok")
    except (TypeError, ValueError, AttributeError, RecursionError):
        return _slack(400, "invalid_payload")
    if not text:
        return _slack(400, "no_text")
    if len(text) > MAX_TEXT:
        return _slack(400, "msg_too_long")
    asked = payload.get("model")
    asked = asked.strip() if isinstance(asked, str) else ""
    if asked and not hook["model_override"]:
        return _slack(403, "action_prohibited")
    if asked and route_backend({"model": asked})[0] is None:
        return _slack(400, "model_not_found")
    if not await asyncio.to_thread(webhook_turn.still_allowed, owner, hook):
        return _slack(403, "action_prohibited")
    if _running.get(owner, 0) >= MAX_RUNNING or _limited(hook["id"]):
        return _slack(429, "rate_limited")
    response_url = payload.get("response_url")
    meta = {"model": asked,
            "user": str(author or payload.get("user_name") or payload.get("username")
                        or "")[:80],
            "thread_ts": payload.get("thread_ts") if isinstance(payload.get("thread_ts"), str) else "",
            "response_url": response_url if isinstance(response_url, str)
            and response_url.startswith("https://") else "",
            # Un événement de forge : ses outils de forge bornés à son projet.
            "forge": scope}
    log.info("webhook %s (« %s ») : message reçu (%d caractères)", hook["id"],
             hook["name"], len(text))
    _start(owner, hook, text, meta)
    return _slack(200, "ok")


# ── la gestion, sous /ui ────────────────────────────────────────────────

def _closed():
    if not enabled():
        return error_response("openai", 404, "unknown_route",
                              "webhooks inactifs ([webhooks].enabled, [ui].chat)")
    return None


async def _body(request: Request):
    if (bad := _closed()) is not None:
        return None, bad
    if (bad := json_only(request)) is not None:
        return None, bad
    return parse_json(await request.body()), None


@router.get("/ui/webhooks")
async def ui_webhooks(request: Request):
    if (bad := _closed()) is not None:
        return bad
    owner = ui_client(request)
    incoming = [_public(h) for h in await asyncio.to_thread(webhooks.list_in, owner)]
    return {"incoming": incoming,
            "outgoing": await asyncio.to_thread(webhooks.list_out, owner),
            "path": HOOK_PREFIX, "max": webhooks.MAX_PER_USER}


def _public(hook: dict) -> dict:
    return {k: v for k, v in hook.items() if k not in ("account", "api_key_id")}


@router.post("/ui/webhooks/in")
async def ui_webhook_in_create(request: Request):
    body, bad = await _body(request)
    if bad is not None:
        return bad
    got, bad = await run(webhooks.create_in, ui_client(request), body,
                         fairshare.ACCOUNT.get(), stats.CLIENT.get() or ui_client(request))
    if bad is not None:
        return bad
    hook, token = got
    return {"webhook": _public(hook), "token": token}


@router.put("/ui/webhooks/in/{wid}")
async def ui_webhook_in_update(request: Request, wid: str):
    body, bad = await _body(request)
    if bad is not None:
        return bad
    hook, bad = await run(webhooks.update_in, ui_client(request), wid, body)
    return bad if bad is not None else {"webhook": _public(hook)}


@router.post("/ui/webhooks/in/{wid}/rotate")
async def ui_webhook_in_rotate(request: Request, wid: str):
    _, bad = await _body(request)
    if bad is not None:
        return bad
    got, bad = await run(webhooks.rotate_in, ui_client(request), wid)
    if bad is not None:
        return bad
    hook, token = got
    return {"webhook": _public(hook), "token": token}


@router.get("/ui/webhooks/in/{wid}/token")
async def ui_webhook_in_token(request: Request, wid: str):
    """Le jeton d'un entrant du compte : le lien que la page copie."""
    if (bad := _closed()) is not None:
        return bad
    token, bad = await run(webhooks.token_in, ui_client(request), wid)
    if bad is not None:
        return bad
    if token is None:
        return error_response("openai", 404, "not_found", "webhook inconnu")
    if not token:
        return error_response("openai", 409, "token_unknown",
                              "jeton non gardé (webhook créé avant v2026.11.21.2) : "
                              "régénérez-le pour pouvoir copier son lien")
    return {"id": wid, "token": token}


@router.delete("/ui/webhooks/in/{wid}")
async def ui_webhook_in_delete(request: Request, wid: str):
    if (bad := _closed()) is not None:
        return bad
    owner = ui_client(request)
    n, bad = await run(webhooks.delete_in, owner, wid)
    if bad is not None:
        return bad
    if n:
        RECENT.forget(wid)
        webhook_turn.forget(owner, wid)
    return {"id": wid, "deleted": bool(n)}


@router.post("/ui/webhooks/out")
async def ui_webhook_out_create(request: Request):
    body, bad = await _body(request)
    if bad is not None:
        return bad
    hook, bad = await run(webhooks.create_out, ui_client(request), body)
    return bad if bad is not None else {"webhook": hook}


@router.put("/ui/webhooks/out/{wid}")
async def ui_webhook_out_update(request: Request, wid: str):
    body, bad = await _body(request)
    if bad is not None:
        return bad
    hook, bad = await run(webhooks.update_out, ui_client(request), wid, body)
    return bad if bad is not None else {"webhook": hook}


@router.post("/ui/webhooks/out/{wid}/test")
async def ui_webhook_out_test(request: Request, wid: str):
    _, bad = await _body(request)
    if bad is not None:
        return bad
    owner = ui_client(request)
    hook, bad = await run(webhooks.get_out, owner, wid)
    if bad is not None:
        return bad
    if hook is None:
        return error_response("openai", 404, "not_found", "webhook inconnu")
    error = await slack.post(owner, hook, "Message d'essai du proxy : ce webhook fonctionne.")
    if error:
        return error_response("openai", 502, "webhook_failed", error)
    return {"id": wid, "sent": True}


@router.delete("/ui/webhooks/out/{wid}")
async def ui_webhook_out_delete(request: Request, wid: str):
    if (bad := _closed()) is not None:
        return bad
    n, bad = await run(webhooks.delete_out, ui_client(request), wid)
    return bad if bad is not None else {"id": wid, "deleted": bool(n)}
