"""
Les routes de l'HISTORIQUE du chat de la page (conversations.py), sous
/ui pour la session : toujours celles du compte connecté, jamais d'un
autre. Les écritures exigent un corps JSON (json_only) : une page tierce
ne peut pas les déclencher.

  GET    /ui/conversations          les conversations du compte ;
                                    ?summary=1 : leurs résumés (sans les
                                    messages), par pages : limit, offset ;
                                    ?q=… : les résumés de celles qui
                                    contiennent chaque mot
  GET    /ui/conversations/{id}     une conversation (le document entier)
  PUT    /ui/conversations/{id}     crée ou remplace (le document entier)
  DELETE /ui/conversations/{id}     supprime, images comprises
  POST   /ui/assets                 garde une image ou un audio : {conversation, name,
                                    data (data URL)} → {id, url}
  GET    /ui/assets/{id}            la rend
"""

import asyncio
import base64
import binascii
import re

from fastapi import APIRouter, Request, Response

from ..access.auth import ui_client
from ..core.httputil import error_response, json_only, parse_json
from ..storage import conversations
from . import dashboard

router = APIRouter()

DATA_URL = re.compile(r"data:([\w.+-]+/[\w.+-]+)?(?:;[\w=.+-]+)*;base64,([A-Za-z0-9+/=\s]+)\Z")


def closed() -> Response | None:
    if not dashboard.UI_CHAT:
        return error_response("openai", 404, "unknown_route",
                              "chat de la page désactivé ([ui].chat)")
    return None


async def run(fn, *args):
    """Une opération du magasin → (résultat, None) ou (None, réponse)."""
    try:
        return await asyncio.to_thread(fn, *args), None
    except conversations.HistoryError as exc:
        return None, error_response("openai", exc.status, "invalid_request_error", str(exc))


MAX_PAGE = 200


def _int(value, default: int, top: int) -> int:
    try:
        return max(0, min(int(value), top))
    except (TypeError, ValueError):
        return default


@router.get("/ui/conversations")
async def ui_conversations(request: Request):
    if (off := closed()) is not None:
        return off
    owner = ui_client(request)
    params = request.query_params
    limit = _int(params.get("limit"), 50, MAX_PAGE)
    if params.get("q") is not None:
        data = await asyncio.to_thread(conversations.search, owner, params["q"], limit)
        return {"object": "list", "data": data}
    if params.get("summary") in ("1", "true"):
        offset = _int(params.get("offset"), 0, 10**9)
        data = await asyncio.to_thread(conversations.summaries, owner, limit + 1, offset)
        more = len(data) > limit
        return {"object": "list", "data": data[:limit],
                "next": offset + limit if more else None,
                "usage": await asyncio.to_thread(conversations.usage, owner)}
    data = await asyncio.to_thread(conversations.list_all, owner)
    return {"object": "list", "data": data,
            "usage": await asyncio.to_thread(conversations.usage, owner)}


@router.get("/ui/conversations/{cid}")
async def ui_conversation(request: Request, cid: str):
    if (off := closed()) is not None:
        return off
    doc, bad = await run(conversations.get, ui_client(request), cid)
    if bad is not None:
        return bad
    if doc is None:
        return error_response("openai", 404, "not_found", "conversation inconnue")
    return doc


@router.put("/ui/conversations/{cid}")
async def ui_conversation_save(request: Request, cid: str):
    if (off := closed()) is not None:
        return off
    if (bad := json_only(request)) is not None:
        return bad
    _, bad = await run(conversations.save, ui_client(request), cid,
                       parse_json(await request.body()))
    return bad if bad is not None else {"id": cid, "saved": True}


@router.delete("/ui/conversations/{cid}")
async def ui_conversation_delete(request: Request, cid: str):
    if (off := closed()) is not None:
        return off
    n, bad = await run(conversations.delete, ui_client(request), cid)
    if bad is not None:
        return bad
    return {"id": cid, "deleted": bool(n)}


@router.post("/ui/assets")
async def ui_asset_add(request: Request):
    if (off := closed()) is not None:
        return off
    if (bad := json_only(request)) is not None:
        return bad
    body = parse_json(await request.body())
    found = DATA_URL.match(str(body.get("data") or "")) if isinstance(body, dict) else None
    if found is None:
        return error_response("openai", 400, "invalid_request_error",
                              "corps JSON attendu : {conversation, name, data (data URL)}")
    try:
        data = base64.b64decode(found.group(2))
    except (binascii.Error, ValueError):
        return error_response("openai", 400, "invalid_request_error", "`data` illisible")
    got, bad = await run(conversations.add_asset, ui_client(request),
                         body.get("conversation"), str(body.get("name") or ""), data)
    return bad if bad is not None else got


@router.get("/ui/assets/{aid}")
async def ui_asset(request: Request, aid: str):
    if (off := closed()) is not None:
        return off
    row = await asyncio.to_thread(conversations.asset, ui_client(request), aid)
    if row is None:
        return error_response("openai", 404, "not_found", "image inconnue")
    kind, name, data = row
    # Une image ne change jamais sous son identifiant ; privée : liée à la
    # session.
    return Response(data, media_type=kind, headers={
        "cache-control": "private, max-age=31536000, immutable",
        "x-content-type-options": "nosniff",
        "content-disposition": f'inline; filename="{name}"'})
