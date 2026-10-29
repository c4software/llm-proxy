"""
Les routes des VOIX CLONÉES du chat de la page (storage/voices.py), sous
/ui pour la session : toujours celles du compte connecté. Les écritures
exigent un corps JSON (json_only) : une page tierce ne peut pas les
déclencher.

  GET    /ui/voices             les voix du compte (sans leur référence)
  PUT    /ui/voices/{id}        crée ou remplace : {name, text, duration,
                                source, created?, data (WAV en data URL)}
  PATCH  /ui/voices/{id}        renomme : {name}
  DELETE /ui/voices/{id}        supprime
  GET    /ui/voices/{id}/audio  la référence (WAV)
"""

import asyncio
import base64
import binascii

from fastapi import APIRouter, Request, Response

from ..access.auth import ui_client
from ..core.httputil import error_response, json_only, parse_json
from ..storage import voices
from .conversation_routes import DATA_URL, closed, run

router = APIRouter()


@router.get("/ui/voices")
async def ui_voices(request: Request):
    if (off := closed()) is not None:
        return off
    data = await asyncio.to_thread(voices.list_all, ui_client(request))
    return {"object": "list", "data": data}


@router.put("/ui/voices/{vid}")
async def ui_voice_save(request: Request, vid: str):
    if (off := closed()) is not None:
        return off
    if (bad := json_only(request)) is not None:
        return bad
    body = parse_json(await request.body())
    found = DATA_URL.match(str(body.get("data") or "")) if isinstance(body, dict) else None
    if found is None:
        return error_response("openai", 400, "invalid_request_error",
                              "corps JSON attendu : {name, text, duration, source, data (data URL)}")
    try:
        wav = base64.b64decode(found.group(2))
    except (binascii.Error, ValueError):
        return error_response("openai", 400, "invalid_request_error", "`data` illisible")
    got, bad = await run(voices.save, ui_client(request), vid, body, wav)
    return bad if bad is not None else got


@router.patch("/ui/voices/{vid}")
async def ui_voice_rename(request: Request, vid: str):
    if (off := closed()) is not None:
        return off
    if (bad := json_only(request)) is not None:
        return bad
    body = parse_json(await request.body())
    got, bad = await run(voices.rename, ui_client(request), vid,
                         body.get("name") if isinstance(body, dict) else None)
    if bad is not None:
        return bad
    if got is None:
        return error_response("openai", 404, "not_found", "voix inconnue")
    return got


@router.delete("/ui/voices/{vid}")
async def ui_voice_delete(request: Request, vid: str):
    if (off := closed()) is not None:
        return off
    n, bad = await run(voices.delete, ui_client(request), vid)
    if bad is not None:
        return bad
    return {"id": vid, "deleted": bool(n)}


@router.get("/ui/voices/{vid}/audio")
async def ui_voice_audio(request: Request, vid: str):
    if (off := closed()) is not None:
        return off
    data = await asyncio.to_thread(voices.wav, ui_client(request), vid)
    if data is None:
        return error_response("openai", 404, "not_found", "voix inconnue")
    # Remplaçable sous le même identifiant (PUT) : pas de cache immuable.
    return Response(data, media_type="audio/wav", headers={
        "cache-control": "private, no-cache",
        "x-content-type-options": "nosniff",
        "content-disposition": 'inline; filename="voix.wav"'})
