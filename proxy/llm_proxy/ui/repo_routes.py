"""
Les SOURCES DE DÉPÔTS du compte connecté (storage/repos.py), pour la page
(Configuration → Dépôts), avec [repos].enabled. Jamais celles d'un autre ;
le jeton n'est jamais rendu. Écritures en JSON (json_only).

  GET    /ui/repos          {sources (les siennes), shared (celles de l'admin,
                            sans jeton), max_bytes}
  POST   /ui/repos          crée {name, forge, url, token, username?, projects?}
  PUT    /ui/repos/{id}     modifie (token absent ou vide : inchangé, sauf
                            si l'adresse ou la forge change)
  DELETE /ui/repos/{id}
"""

import asyncio

from fastapi import APIRouter, Request

from ..access.auth import ui_client
from ..core.httputil import error_response, json_only, parse_json
from ..storage import repos
from ..tools import repository
from .conversation_routes import run

router = APIRouter()


def _closed():
    if not repository.ENABLED:
        return error_response("openai", 404, "unknown_route",
                              "dépôts inactifs ([repos].enabled)")
    return None


@router.get("/ui/repos")
async def ui_repos(request: Request):
    if (bad := _closed()) is not None:
        return bad
    return {"sources": await asyncio.to_thread(repos.list_own, ui_client(request)),
            "shared": repos.shared(), "max_bytes": repository.MAX_BYTES}


async def _write(request: Request, fn, *args):
    if (bad := _closed()) is not None:
        return bad
    if (bad := json_only(request)) is not None:
        return bad
    got, bad = await run(fn, ui_client(request), *args, parse_json(await request.body()))
    return bad if bad is not None else {"source": got}


@router.post("/ui/repos")
async def ui_repo_create(request: Request):
    return await _write(request, repos.create)


@router.put("/ui/repos/{sid}")
async def ui_repo_update(request: Request, sid: str):
    return await _write(request, repos.update, sid)


@router.delete("/ui/repos/{sid}")
async def ui_repo_delete(request: Request, sid: str):
    if (bad := _closed()) is not None:
        return bad
    n, bad = await run(repos.delete, ui_client(request), sid)
    return bad if bad is not None else {"id": sid, "deleted": bool(n)}
