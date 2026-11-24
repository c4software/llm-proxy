"""
Les BACKENDS du compte connecté (storage/backends.py), pour la page
(Configuration → Backends), avec [user_backends].enabled. Jamais ceux d'un
autre, sauf les partagés, en lecture ; la clé n'est jamais rendue.
Écritures en JSON (json_only). Chaque écriture recharge le routage
(upstream/backends.refresh) et oublie les catalogues lus.

  GET    /ui/backends        {backends (les siens), shared (ceux que d'autres
                             comptes partagent, sans clé, avec `by` : qui),
                             reserved (les noms des backends du proxy)} ;
                             ?all=1 (admin) : `all`, ceux de tous les comptes
  POST   /ui/backends        crée {name, api, url, api_key?, headers? (valeurs
                             jamais rendues, seulement les noms), shared, images?
                             (vide : selon le protocole), tokenize_path?,
                             enabled? (vrai par défaut ; faux : gardé, pas routé),
                             tool_choice? ("", auto, required, none),
                             quotas?, limits? (le limiteur et ses règles : posés par un admin)}
  PUT    /ui/backends/{id}   modifie (api_key absente ou vide : inchangée,
                             sauf si l'adresse change d'origine) ; ?all=1
                             (admin) : celui de n'importe qui (ses quotas…)
  DELETE /ui/backends/{id}   le sien ; ?all=1 (admin) : celui de n'importe qui
  GET    /ui/quotas          (admin) le limiteur de chaque backend à quotas :
                             buckets, consommation, plafonds, restant annoncé
                             par le compte, 429 reçus,
                             forfait mensuel (Mistral) ; ?refresh=1 relit
                             d'abord les limites et le forfait du compte

Admin : un compte admin, ou toute session sans comptes OIDC (comme les
parties Extensions et Proxy).
"""

import asyncio

from fastapi import APIRouter, Request

from ..access import accounts, oidc
from ..access.auth import account_owner, live_owners, request_user, ui_client
from ..core.httputil import error_response, json_only, parse_json
from ..storage import backends as user_backends
from ..upstream import backends, catalog, quotas
from .conversation_routes import run

router = APIRouter()


def _closed():
    if not user_backends.ENABLED:
        return error_response("openai", 404, "unknown_route",
                              "backends des comptes inactifs ([user_backends].enabled)")
    return None


async def changed() -> None:
    """Après une écriture, ou un changement de rôle d'un compte (ses
    backends servent ou cessent de servir) : le routage relu, les
    catalogues oubliés."""
    await backends.refresh(live_owners)
    catalog.forget()


def _admin(request: Request) -> bool:
    return not oidc.ENABLED or (request_user(request) or {}).get("role") == "admin"


def _named(rows: list[dict]) -> list[dict]:
    """Chaque ligne avec `by`, son créateur lisible, au lieu de son client.
    BLOQUANT."""
    if oidc.ENABLED:
        names = {account_owner(u["id"]): u["email"] or u["name"] or f"#{u['id']}"
                 for u in accounts.users()}
    else:
        names = {}
    out = []
    for r in rows:
        owner = r.pop("owner")
        r["by"] = names.get(owner) or (f"clé {owner[:8]}" if owner else "")
        out.append(r)
    return out


@router.get("/ui/backends")
async def ui_backends(request: Request):
    if (bad := _closed()) is not None:
        return bad
    owner = ui_client(request)

    def read():
        out = {"backends": [{k: v for k, v in r.items() if k != "owner"}
                            for r in user_backends.list_own(owner)],
               "shared": _named(user_backends.list_shared(owner)),
               "reserved": sorted(backends.BACKENDS)}
        if request.query_params.get("all") in ("1", "true") and _admin(request):
            out["all"] = _named(user_backends.list_all())
        return out
    return await asyncio.to_thread(read)


def _anyone(request: Request):
    """?all=1 : (True, None) pour un admin (tous les comptes), (True, 403)
    sinon ; (False, None) sans lui."""
    if request.query_params.get("all") not in ("1", "true"):
        return False, None
    if not _admin(request):
        return True, error_response("openai", 403, "forbidden", "réservé aux admins")
    return True, None


async def _write(request: Request, fn, *args, anyone: bool = False):
    if (bad := _closed()) is not None:
        return bad
    if (bad := json_only(request)) is not None:
        return bad
    got, bad = await run(fn, None if anyone else ui_client(request), *args,
                         parse_json(await request.body()), frozenset(backends.BACKENDS),
                         _admin(request))
    if bad is not None:
        return bad
    await changed()
    got.pop("owner")
    return {"backend": got}


@router.get("/ui/quotas")
async def ui_quotas(request: Request):
    """Configuration → Quotas (admin) : le limiteur de chaque backend à
    quotas, du TOML et de tous les comptes (upstream/backends.quota_view),
    avec le forfait mensuel de leur compte quand il en a un (Mistral,
    backends.read_for_page). ?refresh=1 relit d'abord les limites et le
    forfait de leur compte (une fois par backends.REREAD_SECONDS au plus)."""
    if not _admin(request):
        return error_response("openai", 403, "forbidden", "réservé aux admins")
    chosen = backends.quota_backends()
    refresh = request.query_params.get("refresh") in ("1", "true")
    await asyncio.gather(*(backends.read_for_page(b, refresh) for b in chosen))
    views = [backends.quota_view(b) for b in chosen]
    # `by` : le compte qui l'a déclaré (vide : un backend du TOML).
    owners = [{"owner": v.pop("owner") or ""} for v in views]
    for v, r in zip(views, await asyncio.to_thread(_named, owners)):
        v["by"] = r["by"]
    return {"backends": views, "margin": quotas.MARGIN}


@router.post("/ui/backends")
async def ui_backend_create(request: Request):
    return await _write(request, user_backends.create)


@router.put("/ui/backends/{bid}")
async def ui_backend_update(request: Request, bid: str):
    anyone, bad = _anyone(request)
    if bad is not None:
        return bad
    return await _write(request, user_backends.update, bid, anyone=anyone)


@router.delete("/ui/backends/{bid}")
async def ui_backend_delete(request: Request, bid: str):
    if (bad := _closed()) is not None:
        return bad
    anyone, bad = _anyone(request)
    if bad is not None:
        return bad
    n, bad = await run(user_backends.delete, None if anyone else ui_client(request), bid)
    if bad is not None:
        return bad
    if n:
        await changed()
    return {"id": bid, "deleted": bool(n)}
