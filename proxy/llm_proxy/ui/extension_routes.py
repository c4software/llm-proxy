"""
Les EXTENSIONS, pour la page (Configuration → Extensions, admin ; sans
comptes OIDC, la page connectée, comme la configuration du proxy) : ce qui
est chargé de [extensions].path, ce qui est écarté et pourquoi, et
l'interrupteur À CHAUD de chaque extension chargée (tools/extensions.py,
gardé par storage/extension_state.py). Et, pour le chat de toute page
connectée, le .js qui exécute les outils du navigateur d'une extension.

  GET  /ui/extensions               {path, extensions: [{name, enabled, tools:
                                    [{name, runs, offered, configured}]} |
                                    {name, refused, stopped}]}
  POST /ui/extensions/{nom}         {"enabled": bool} → l'entrée mise à jour ;
                                    404 : extension non chargée ; 409 : arrêtée
                                    par [extensions.<nom>].enabled = false
  GET  /ui/extensions/{nom}/{nom}.js  le module de la page (session.chat.page
                                    en donne l'adresse) ; 404 : éteinte,
                                    inconnue ou sans .js
"""

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from ..core.httputil import error_response, json_only, parse_json
from ..tools import extensions
from .config_view import forbidden

router = APIRouter()


@router.get("/ui/extensions")
async def ui_extensions(request: Request):
    if (bad := forbidden(request)) is not None:
        return bad
    return {"path": extensions.PATH, "extensions": extensions.listing()}


@router.post("/ui/extensions/{name}")
async def ui_extension_switch(request: Request, name: str):
    if (bad := forbidden(request)) is not None:
        return bad
    if (bad := json_only(request)) is not None:
        return bad
    body = parse_json(await request.body())
    on = body.get("enabled") if isinstance(body, dict) else None
    if not isinstance(on, bool):
        return error_response("openai", 400, "invalid_request_error",
                              "corps attendu : {\"enabled\": true|false}")
    entry = next((e for e in extensions.listing() if e["name"] == name), None)
    if entry is not None and entry.get("stopped"):
        return error_response("openai", 409, "conflict",
                              f"{name} est désactivée par la configuration "
                              f"([extensions.{name}].enabled) : la page ne la rallume pas")
    try:
        await asyncio.to_thread(extensions.switch, name, on)
    except KeyError:
        return error_response("openai", 404, "not_found",
                              f"extension «{name}» non chargée")
    return next(e for e in extensions.listing() if e["name"] == name)


@router.get("/ui/extensions/{name}/{script}")
async def ui_extension_script(name: str, script: str):
    path = extensions.script(name) if script == f"{name}.js" else None
    if path is None:
        return error_response("openai", 404, "not_found",
                              f"pas de module pour l'extension «{name}» (éteinte, "
                              f"inconnue ou sans .js)")
    # L'adresse porte le condensé du fichier (?v=) : rien à garder d'ancien.
    return FileResponse(path, media_type="text/javascript",
                        headers={"Cache-Control": "no-cache"})
