"""
La MÉMOIRE des modèles, sous /ui : lire, écrire, renommer, effacer les
fichiers de l'outil `memory` du compte.
"""

import asyncio
from fastapi import Request
from fastapi.responses import JSONResponse
from ... import tools
from ...storage import memories
from ...access.auth import ui_client
from ...core.httputil import error_response, json_only, parse_json

from .page import router


# ── Mémoire, sous /ui ───────────────────────────────────────────────────
# Ce que l'outil `memory` a gardé pour le client CONNECTÉ (la session de
# la page, ou l'en-tête) : la liste, un fichier, l'écriture, la
# suppression, le renommage. Jamais la mémoire d'un autre. Les écritures
# exigent un corps JSON (json_only) : une page tierce ne peut pas les
# déclencher.

def memory_disabled() -> JSONResponse | None:
    if not tools.memory.ENABLED:
        return error_response("openai", 404, "unknown_route",
                              "mémoire désactivée ([tools.memory].enabled)")
    return None


def memory_call(fn, *args):
    """Une opération du magasin → (résultat, None) ou (None, réponse 400)."""
    try:
        return fn(*args), None
    except memories.MemoryError_ as exc:
        return None, error_response("openai", 400, "invalid_request_error", str(exc))


@router.get("/ui/memory")
async def ui_memory_list(request: Request):
    if (off := memory_disabled()) is not None:
        return off
    owner = ui_client(request)
    files = await asyncio.to_thread(memories.list_files, owner)
    return {"object": "list", "data": files,
            "usage": await asyncio.to_thread(memories.usage, owner)}


@router.get("/ui/memory/file")
async def ui_memory_read(request: Request, path: str = ""):
    if (off := memory_disabled()) is not None:
        return off
    content, bad = await asyncio.to_thread(
        memory_call, memories.read, ui_client(request), path)
    if bad is not None:
        return bad
    if content is None:
        return error_response("openai", 404, "not_found", f"{path} n'existe pas")
    return {"path": memories.normalize(path), "content": content}


@router.put("/ui/memory/file")
async def ui_memory_write(request: Request):
    if (off := memory_disabled()) is not None:
        return off
    if (bad := json_only(request)) is not None:
        return bad
    body = parse_json(await request.body())
    if not isinstance(body, dict):
        return error_response("openai", 400, "invalid_request_error",
                              "corps JSON attendu : {path, content}")
    existed, bad = await asyncio.to_thread(
        memory_call, memories.write, ui_client(request),
        body.get("path"), body.get("content"))
    if bad is not None:
        return bad
    return {"path": memories.normalize(body.get("path")), "created": not existed}


@router.delete("/ui/memory/file")
async def ui_memory_delete(request: Request, path: str = ""):
    if (off := memory_disabled()) is not None:
        return off
    n, bad = await asyncio.to_thread(
        memory_call, memories.delete, ui_client(request), path)
    if bad is not None:
        return bad
    if not n:
        return error_response("openai", 404, "not_found", f"{path} n'existe pas")
    return {"deleted": n}


@router.post("/ui/memory/rename")
async def ui_memory_rename(request: Request):
    if (off := memory_disabled()) is not None:
        return off
    if (bad := json_only(request)) is not None:
        return bad
    body = parse_json(await request.body())
    if not isinstance(body, dict):
        return error_response("openai", 400, "invalid_request_error",
                              "corps JSON attendu : {old_path, new_path}")
    n, bad = await asyncio.to_thread(
        memory_call, memories.rename, ui_client(request),
        body.get("old_path"), body.get("new_path"))
    if bad is not None:
        return bad
    return {"renamed": n, "path": memories.normalize(body.get("new_path"))}
