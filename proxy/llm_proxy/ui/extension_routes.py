"""
Les EXTENSIONS, pour la page (Configuration → Extensions) : ce qui est
chargé de [extensions].path, ce qui est écarté et pourquoi, et
l'interrupteur À CHAUD de chaque extension chargée (tools/extensions.py,
gardé dans config.toml, `[extensions.<nom>].active`). Les lire : comme la
configuration du proxy (config_view.forbidden) ; basculer écrit
config.toml : un admin OIDC seulement (config_view.write_forbidden). Et,
pour le chat de toute page connectée, le .js qui exécute les outils du
navigateur d'une extension.

  GET  /ui/extensions               {path, writable, extensions: [{name, enabled,
                                    tools: [{name, runs, offered, configured,
                                    enabled}]} | {name, refused, stopped}]}
  POST /ui/extensions/{nom}         {"enabled": bool, "tool"?: un de ses outils}
                                    → l'entrée mise à jour : toute l'extension,
                                    ou ce seul outil ; 404 : extension non
                                    chargée, outil qui n'est pas le sien ;
                                    409 : bloquée par [extensions.<nom>].enabled,
                                    ou pas de config.toml
  GET  /ui/extensions/{nom}/{nom}.js  le module de la page (session.chat.page
                                    en donne l'adresse) ; 404 : aucun outil du
                                    navigateur actif, inconnue ou sans .js
"""

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from ..core.httputil import error_response, json_only, parse_json
from ..tools import extensions
from .config_view import WRITE_ERRORS, forbidden, writable, write_error, write_forbidden

router = APIRouter()


@router.get("/ui/extensions")
async def ui_extensions(request: Request):
    if (bad := forbidden(request)) is not None:
        return bad
    return {"path": extensions.PATH, "writable": writable(request),
            "extensions": extensions.listing()}


@router.post("/ui/extensions/{name}")
async def ui_extension_switch(request: Request, name: str):
    if (bad := write_forbidden(request)) is not None:
        return bad
    if (bad := json_only(request)) is not None:
        return bad
    body = parse_json(await request.body())
    on = body.get("enabled") if isinstance(body, dict) else None
    tool = body.get("tool", "") if isinstance(body, dict) else ""
    if not isinstance(on, bool) or not isinstance(tool, str):
        return error_response("openai", 400, "invalid_request_error",
                              "corps attendu : {\"enabled\": true|false, \"tool\"?: \"…\"}")
    entry = next((e for e in extensions.listing() if e["name"] == name), None)
    if entry is not None and entry.get("stopped"):
        return error_response("openai", 409, "conflict",
                              f"{name} est bloquée par la configuration "
                              f"([extensions.{name}].enabled) : la page ne la réactive pas")
    try:
        await asyncio.to_thread(extensions.switch, name, on, tool)
    except WRITE_ERRORS as exc:
        return write_error(exc)
    except KeyError:
        loaded = entry is not None and "tools" in entry
        return error_response("openai", 404, "not_found",
                              f"outil «{tool}» : pas un outil de l'extension «{name}»"
                              if tool and loaded else f"extension «{name}» non chargée")
    return next(e for e in extensions.listing() if e["name"] == name)


@router.get("/ui/extensions/{name}/{script}")
async def ui_extension_script(name: str, script: str):
    path = extensions.script(name) if script == f"{name}.js" else None
    if path is None:
        return error_response("openai", 404, "not_found",
                              f"pas de module pour l'extension «{name}» (aucun outil du "
                              f"navigateur actif, inconnue ou sans .js)")
    # L'adresse porte le condensé du fichier (?v=) : rien à garder d'ancien.
    return FileResponse(path, media_type="text/javascript",
                        headers={"Cache-Control": "no-cache"})
