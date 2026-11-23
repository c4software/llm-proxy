"""
Les LIMITES DU COMPTE d'un backend Albert, pour le limiteur (quotas.py) :
GET /v1/limits donne, pour chaque modèle permis, son routeur, ses plafonds
(rpm, tpm, rpd, tpd ; null = illimité) et ce qu'il en reste dans la fenêtre
en cours. Les modèles d'un même routeur partagent un bucket ; les alias
d'un modèle (/v1/models, `aliases`) le suivent.

Un backend sans clé, ou qui n'a pas /v1/limits (404, 405 : pas Albert), n'a
pas de limites de compte : refresh le dit (None), et le limiteur s'en tient
aux règles du backend, aux familles et au générique.
"""

import time

from ..core.limits import KINDS
from . import quotas
from .quotas import log


def _parse_limits(doc) -> dict[str, tuple[int, dict, dict]]:
    """/v1/limits → {modèle (minuscule): (router_id, plafonds, restants)} ;
    un plafond null = 0 (illimité). Une entrée mal formée est ignorée."""
    out = {}
    data = doc.get("data") if isinstance(doc, dict) else None
    for entry in data if isinstance(data, list) else ():
        if not isinstance(entry, dict):
            continue
        rid, model = entry.get("router_id"), entry.get("model")
        if not isinstance(rid, int) or not isinstance(model, str) or not model:
            continue
        caps = dict.fromkeys(KINDS, 0)
        left = {}
        for limit in entry.get("limits") or ():
            kind = limit.get("type") if isinstance(limit, dict) else None
            if kind not in KINDS:
                continue
            value = limit.get("value")
            if isinstance(value, int) and value > 0:
                caps[kind] = value
                if isinstance(limit.get("remaining"), int):
                    left[kind] = limit["remaining"]
        out[model.lower()] = (rid, caps, left)
    return out


def _aliases(doc) -> dict[str, list[str]]:
    """/v1/models → {id (minuscule): [alias…]}."""
    out = {}
    data = doc.get("data") if isinstance(doc, dict) else None
    for model in data if isinstance(data, list) else ():
        if isinstance(model, dict) and isinstance(model.get("id"), str):
            out[model["id"].lower()] = [a.lower() for a in model.get("aliases") or ()
                                        if isinstance(a, str) and a]
    return out


async def refresh(state: "quotas.QuotaState", backend) -> bool | None:
    """Lit /v1/limits (et les alias de /v1/models) du backend dans `state`.
    True : lues ; False : échec passager (injoignable, réponse illisible),
    rien n'est écrasé ; None : pas de limites de compte (sans clé, ou
    /v1/limits → 404 ou 405). `backend` : objet avec .client
    (httpx2.AsyncClient), .auth_headers() et .meta_timeout."""
    if backend is None or backend.client is None:
        return False
    if not backend.api_key:
        return None
    client, headers, timeout = backend.client, backend.auth_headers(), backend.meta_timeout
    try:
        r = await client.get("/v1/limits", headers=headers, timeout=timeout)
        if r.status_code in (404, 405):
            return None
        if r.status_code != 200:
            log.warning("[%s] /v1/limits → %d — limites précédentes conservées",
                        state.name, r.status_code)
            return False
        limits = _parse_limits(r.json())
    except Exception as exc:
        log.warning("[%s] /v1/limits illisible ou injoignable : %s — limites "
                    "précédentes conservées", state.name, exc)
        return False
    if not limits:
        log.warning("[%s] /v1/limits : aucun modèle reconnu — limites précédentes "
                    "conservées", state.name)
        return False
    try:
        m = await client.get("/v1/models", headers=headers, timeout=timeout)
        aliases = _aliases(m.json()) if m.status_code == 200 else None
    except Exception:
        aliases = None             # catalogue illisible : les alias connus restent

    routers: dict[int, dict] = {}
    left: dict[int, dict] = {}
    names: dict[str, str] = {}
    shown: dict[str, str] = {}
    for model, (rid, caps, remaining) in sorted(limits.items()):
        routers[rid], left[rid] = caps, remaining
        key = f"router:{rid}"
        for name in (model, *(aliases or {}).get(model, ())):
            names[name] = key
        shown.setdefault(key, model)
    if aliases is None:
        for name, key in state.name_to_bucket.items():
            if int(key.split(":", 1)[1]) in routers:
                names.setdefault(name, key)
    log.info("[%s] limites du compte : %d modèles, %d routeurs",
             state.name, len(limits), len(routers))
    state.set_account(routers, names, shown, left, time.time())
    return True
