"""
L'EXÉCUTION en cours, pour la page (Configuration → Exécution) : les bacs
de l'exécuteur et les caches du proxy, avec de quoi les nettoyer.

Chacun voit et nettoie les SIENS (ses bacs, ses dépôts téléchargés). Un
admin (avec les comptes OIDC ; sans eux, la page connectée, comme pour la
configuration du proxy) voit en plus l'état de l'exécuteur, les bacs de
tous (avec le compte de chacun) et les caches du proxy, et peut les vider.

  GET  /ui/runtime                  {own: {bacs, repositories}, admin?:
                                    {executor, bacs, caches}} ; bacs à None :
                                    exécuteur non configuré ou injoignable
  POST /ui/runtime/bacs/destroy     {session, client?} : un bac (client :
                                    admin seulement, sinon le sien)
  POST /ui/runtime/bacs/destroy_all {everyone?} : ses bacs, ou ceux de tous
                                    (admin)
  POST /ui/runtime/caches/{nom}/clear   vide un cache du proxy (admin)

Écritures en JSON (json_only). Détruire un bac perd ses fichiers, même en
pleine exécution : la conversation en retrouve un neuf au prochain appel.
"""

import asyncio

from fastapi import APIRouter, Request

from .. import tools
from ..access import accounts, oidc
from ..access.auth import account_owner, toml_digests, ui_client
from ..core.httputil import error_response, json_only, parse_json
from ..surfaces import chat_api
from ..tools import code_execution, repository, transcribe, webcache
from .dashboard.accounts import admin_only

router = APIRouter()

# Les caches qu'un admin peut vider : nom → (libellé, objet).
CACHES = {
    "web": ("Pages et recherches web", webcache.CACHE),
    "repository": ("Archives des dépôts", repository.CACHE),
    "transcribe": ("Transcriptions", transcribe.CACHE),
    "tool_results": ("Résultats d'outils rejoués", tools.MEMORY),
    "chat_exchanges": ("Échanges d'outils des conversations", chat_api.MEMORY),
}


def _executor() -> bool:
    return bool(code_execution.URL and code_execution.TOKEN)


def _is_admin(request: Request) -> bool:
    return (admin_only(request) if oidc.ENABLED else None) is None


async def _labels() -> dict[str, str]:
    """Le client d'un bac (le condensé de son compte ou de sa clé) → de quoi
    le reconnaître dans la page."""
    out = {"": "proxy ouvert"}
    if oidc.ENABLED:
        for u in await asyncio.to_thread(accounts.users):
            out[account_owner(u["id"])] = u.get("email") or u.get("name") or f"#{u['id']}"
    for digest in toml_digests():
        out.setdefault(digest, f"clé du TOML #{digest[:8]}")
    return out


def _cache(name: str, label: str, cache) -> dict:
    # La taille en octets n'a de sens que pour les archives des dépôts : les
    # autres caches comptent en caractères, ou pas du tout.
    return {"name": name, "label": label, "entries": len(cache),
            "bytes": cache.size if cache is repository.CACHE else None, "ttl": cache.ttl}


@router.get("/ui/runtime")
async def ui_runtime(request: Request):
    owner = ui_client(request)
    mine = await code_execution.sessions(owner) if _executor() else None
    entries, size = repository.CACHE.count(lambda key: key[0] == owner)
    out = {"own": {"bacs": mine,
                   "repositories": {"entries": entries, "bytes": size,
                                    "ttl": repository.CACHE.ttl}}}
    if _is_admin(request):
        status, bacs, labels = (await asyncio.gather(code_execution.status(),
                                                     code_execution.sessions(), _labels())) \
            if _executor() else (None, None, {})
        for b in bacs or []:
            b["account"] = labels.get(b["client"], f"#{b['client'][:8]}")
        out["admin"] = {"executor": status, "bacs": bacs,
                        "caches": [_cache(n, label, c) for n, (label, c) in CACHES.items()]}
    return out


async def _body(request: Request):
    if (bad := json_only(request)) is not None:
        return None, bad
    body = parse_json(await request.body())
    if not isinstance(body, dict):
        return None, error_response("openai", 400, "invalid_request_error", "corps JSON attendu")
    return body, None


def _unreachable():
    return error_response("openai", 502, "executor_unreachable",
                          "l'exécuteur n'a pas pu être joint : les bacs expireront seuls")


@router.post("/ui/runtime/bacs/destroy")
async def ui_bac_destroy(request: Request):
    body, bad = await _body(request)
    if bad is not None:
        return bad
    session = body.get("session")
    if not isinstance(session, str) or not session or session == "~":
        return error_response("openai", 400, "invalid_request_error", "`session` attendue")
    client = ui_client(request)
    if "client" in body and body["client"] != client:
        if not _is_admin(request):
            return error_response("openai", 403, "forbidden", "réservé aux admins")
        client = str(body["client"])
    if not _executor():
        return _unreachable()
    done = await code_execution.destroy(client, session)
    return _unreachable() if done is None else {"destroyed": done}


@router.post("/ui/runtime/bacs/destroy_all")
async def ui_bacs_destroy_all(request: Request):
    body, bad = await _body(request)
    if bad is not None:
        return bad
    everyone = body.get("everyone") is True
    if everyone and not _is_admin(request):
        return error_response("openai", 403, "forbidden", "réservé aux admins")
    if not _executor():
        return _unreachable()
    n = await code_execution.destroy_all(None if everyone else ui_client(request))
    return _unreachable() if n is None else {"destroyed": n}


@router.post("/ui/runtime/caches/{name}/clear")
async def ui_cache_clear(request: Request, name: str):
    if (bad := json_only(request)) is not None:
        return bad
    if not _is_admin(request):
        return error_response("openai", 403, "forbidden", "réservé aux admins")
    if name not in CACHES:
        return error_response("openai", 404, "not_found", "cache inconnu")
    cache = CACHES[name][1]
    n = len(cache)
    cache.clear()
    return {"name": name, "cleared": n}
