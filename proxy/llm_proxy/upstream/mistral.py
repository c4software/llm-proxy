"""
Le FORFAIT MENSUEL d'un compte Mistral, pour Configuration → Quotas :
GET /v1/billing/budget donne, pour l'API et pour Vibe & Studio, le montant
alloué pour la période, la part consommée (en %), la devise, la date de
réinitialisation et si le paiement à l'usage prend le relais.

Lu avec la clé (ou les en-têtes propres) du backend. Un backend sans
l'une ni les autres, d'un autre protocole, ou dont la route répond 401,
403, 404, 405, un corps illisible ou sans forfait reconnu (pas Mistral)
n'en a pas : refresh le dit (None), et il n'est plus interrogé.
"""

import math
import time

from .quotas import log

# Les forfaits de la réponse, dans l'ordre de la page.
KINDS = {"api_budget": "api", "vibe_budget": "vibe"}


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value) and value >= 0


def _parse(doc) -> list[dict]:
    """GetBudgetResponse → [{kind, initial, used_percent, currency,
    reset_at, payg}] ; un forfait mal formé est ignoré."""
    out = []
    for field, kind in KINDS.items():
        d = doc.get(field) if isinstance(doc, dict) else None
        if not isinstance(d, dict) or not _number(d.get("initial_budget")) \
                or not _number(d.get("usage_percentage")):
            continue
        text = lambda k: d[k][:40] if isinstance(d.get(k), str) else None
        out.append({"kind": kind, "initial": float(d["initial_budget"]),
                    "used_percent": min(float(d["usage_percentage"]), 100.0),
                    "currency": text("currency"), "reset_at": text("reset_at"),
                    "payg": d.get("payg_enabled") is True})
    return out


async def refresh(state, backend) -> bool | None:
    """Lit le forfait du compte du backend dans `state` (QuotaState :
    `budget`, `budget_read`). True : lu ; False : échec passager, rien
    n'est écrasé ; None : pas de forfait (ni clé ni en-têtes propres,
    autre protocole, 401, 403, 404, 405, corps sans forfait). `backend` :
    objet avec .api, .api_key, .headers, .client, .auth_headers() et
    .meta_timeout."""
    if backend.api != "openai" or not (backend.api_key or getattr(backend, "headers", None)):
        return None
    if backend.client is None:
        return False
    try:
        r = await backend.client.get("/v1/billing/budget", headers=backend.auth_headers(),
                                     timeout=backend.meta_timeout)
        if r.status_code in (404, 405):
            return None
        if r.status_code in (401, 403):
            log.warning("[%s] /v1/billing/budget → %d : pas de forfait lisible avec ces "
                        "identifiants", state.name, r.status_code)
            return None
        if r.status_code != 200:
            log.warning("[%s] /v1/billing/budget → %d — forfait précédent conservé",
                        state.name, r.status_code)
            return False
    except Exception as exc:
        log.warning("[%s] /v1/billing/budget injoignable : %s — forfait précédent "
                    "conservé", state.name, exc)
        return False
    try:
        got = _parse(r.json())
    except Exception:               # pas du JSON lisible : pas une API Mistral
        got = []
    if not got:
        return None
    state.budget, state.budget_read = got, time.time()
    return True
