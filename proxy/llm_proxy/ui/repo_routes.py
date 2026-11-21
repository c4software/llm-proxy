"""
Les SOURCES DE DÉPÔTS du compte connecté (storage/repos.py), pour la page
(Configuration → Dépôts), avec [repos].enabled. Jamais celles d'un autre ;
le jeton n'est jamais rendu. Écritures en JSON (json_only).

  GET    /ui/repos          {sources (les siennes), shared (celles de l'admin,
                            sans jeton), max_bytes, ttl (durée de vie d'un
                            dépôt téléchargé)}
  POST   /ui/repos          crée {name, forge, url, token, username?, projects?}
  PUT    /ui/repos/{id}     modifie (token absent ou vide : inchangé, sauf
                            si l'adresse ou la forge change)
  DELETE /ui/repos/{id}
  POST   /ui/repos/forget   efface tout ce que le compte a téléchargé (cache
                            et bac des dépôts) → {forgotten} (faux : bac non
                            joint, il expirera seul) — aussi fait à chaque
                            modification ou suppression d'une source
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
    if not repository.TOOL.enabled:
        return error_response("openai", 404, "unknown_route",
                              "dépôts inactifs ([repos].enabled, exécuteur de [tools.code_execution])")
    return None


@router.get("/ui/repos")
async def ui_repos(request: Request):
    if (bad := _closed()) is not None:
        return bad
    return {"sources": await asyncio.to_thread(repos.list_own, ui_client(request)),
            "shared": repos.shared(), "max_bytes": repository.MAX_BYTES,
            "ttl": repository.CACHE.ttl}


async def _write(request: Request, fn, *args):
    if (bad := _closed()) is not None:
        return bad
    if (bad := json_only(request)) is not None:
        return bad
    owner = ui_client(request)
    got, bad = await run(fn, owner, *args, parse_json(await request.body()))
    if bad is not None:
        return bad
    if fn is repos.update:
        # Ce qui a été lu avec l'ancienne adresse, l'ancien jeton ou les
        # anciens projets permis ne doit plus servir.
        await repository.forget(owner)
    return {"source": got}


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
    owner = ui_client(request)
    n, bad = await run(repos.delete, owner, sid)
    if bad is not None:
        return bad
    if n:
        await repository.forget(owner)
    return {"id": sid, "deleted": bool(n)}


@router.post("/ui/repos/forget")
async def ui_repos_forget(request: Request):
    if (bad := _closed()) is not None:
        return bad
    if (bad := json_only(request)) is not None:
        return bad
    # Faux : l'exécuteur n'a pas pu être joint, son bac expirera seul.
    return {"forgotten": await repository.forget(ui_client(request))}
