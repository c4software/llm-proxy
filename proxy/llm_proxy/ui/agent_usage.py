"""
L'USAGE D'UN COMPTE au format des enregistrements du panneau d'agents
d'Omarchy (schemaVersion 1) : GET /v1/usage/agent avec une clé du proxy,
déposé tel quel dans ~/.local/state/omarchy/agents/usage/ (le service que
propose Configuration → Clients) ; la même chose sous /ui pour la vue Usage
de la page.

Ce que voit le client de la requête : son compte (toutes ses clés et sa
page), sa clé seule pour une clé du TOML non rattachée, tout pour un
proxy ouvert.

  limits  la part équitable du compte (une heure glissante), le forfait
          mensuel des backends à quotas qui sont les siens (ceux du TOML
          aussi pour un admin) ;
  stats   ses tokens par jour et par modèle, tout ce que la base garde
          ([stats].retention_days) : une requête vaut un « prompt », sans
          notion de session.
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ..access import accounts, fairshare
from ..storage import stats
from ..upstream import backends, relaying
from .usage_api import client_keys

router = APIRouter()

RECORD_ID = "llm-proxy"
RECORD_NAME = "LLM proxy"
# Les noms de forfait (mistral.KINDS) tels que la page Quotas les montre.
BUDGET_LABELS = {"api": "API", "vibe": "Vibe"}


async def _admin() -> bool:
    """Le client voit-il les backends du TOML ? Un admin, ou tout client
    sans comptes (une clé du TOML seule, un proxy ouvert)."""
    uid = fairshare.ACCOUNT.get()
    if uid is None:
        return True
    user = await asyncio.to_thread(accounts.user, uid)
    return bool(user) and user["role"] == "admin"


async def _limits() -> list[dict]:
    out = []
    client = fairshare.CLIENT.get()
    share = await relaying.share_of() if client is not None else None
    if share:
        out.append({"label": "Part équitable", "title": "Session",
                    "percent": min(fairshare.used(client) / share, 1.0), "resetsAt": ""})
    me, admin = fairshare.CLIENT.get() or "", await _admin()
    mine = [b for b in backends.quota_backends()
            if b.owner == me or (b.owner is None and admin)]
    await asyncio.gather(*(backends.read_for_page(b) for b in mine))
    for b in mine:
        for f in b.quota_state.budget or []:
            out.append({"label": f"{b.name} {BUDGET_LABELS.get(f['kind'], f['kind'])}",
                        "title": "Monthly", "percent": f["used_percent"] / 100,
                        "resetsAt": f["reset_at"] or ""})
    return out


def _stats(rows, tz) -> dict:
    """Le bloc de statistiques d'Omarchy (empty_stats/Stats.snapshot) :
    les jours sont ceux du fuseau `tz` (None : l'heure locale du proxy)."""
    now = datetime.now(tz) if tz else datetime.now()
    today = now.strftime("%Y-%m-%d")
    recent = {(now - timedelta(days=d)).strftime("%Y-%m-%d"): 0 for d in range(6, -1, -1)}
    out = {"todayPrompts": 0, "todaySessions": 0, "todayTotalTokens": 0,
           "todayTokensByModel": {}, "totalPrompts": 0, "totalSessions": 0,
           "modelUsage": {}}
    dates = set()
    for start, model, prompt, completion, cached, count in rows:
        # tz None : l'heure locale de la tranche, heure d'été comprise.
        day = datetime.fromtimestamp(start, tz).strftime("%Y-%m-%d")
        cached = min(cached or 0, prompt or 0)
        total = (prompt or 0) + (completion or 0)
        out["totalPrompts"] += count
        dates.add(day)
        if total > 0:
            bucket = out["modelUsage"].setdefault(model, {
                "inputTokens": 0, "outputTokens": 0,
                "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0})
            bucket["inputTokens"] += (prompt or 0) - cached
            bucket["outputTokens"] += completion or 0
            bucket["cacheReadInputTokens"] += cached
        if day in recent:
            recent[day] += total
        if day == today:
            out["todayPrompts"] += count
            out["todayTotalTokens"] += total
            if total > 0:
                by = out["todayTokensByModel"]
                by[model] = by.get(model, 0) + total
    out["recentDays"] = [{"date": d, "messageCount": n} for d, n in recent.items()]
    out["activeDays"] = len(dates)
    out["activeDates"] = sorted(dates)
    return out


async def record(request: Request):
    """L'enregistrement du client de la requête ; `?tz=Europe/Paris` date
    les jours dans ce fuseau (défaut : celui du proxy)."""
    name = request.query_params.get("tz")
    try:
        tz = ZoneInfo(name) if name else None
    except (ValueError, KeyError, OSError):
        return JSONResponse({"error": {"message": f"fuseau inconnu : {name}",
                                       "type": "invalid_request_error", "param": "tz"}},
                            status_code=400)
    rows = await run_in_threadpool(stats.slots, await client_keys())
    limits = await _limits()
    block = _stats(rows, tz)
    return {
        "schemaVersion": 1, "id": RECORD_ID, "name": RECORD_NAME,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "ready": bool(limits or block["totalPrompts"]),
        "hasLocalStats": False, "hasPromptStats": False, "scope": "account",
        "tierLabel": "", "usageStatusText": "", "authHelpText": "",
        "limits": limits, "limitsStale": False,
        "limitsFetchedAt": int(time.time() * 1000) if limits else 0,
        "retryAdvised": False,
        **block,
    }


@router.get("/v1/usage/agent")
async def agent_usage(request: Request):
    return await record(request)


@router.get("/ui/usage/agent")
async def ui_agent_usage(request: Request):
    """La même chose sous /ui, pour le cookie de la page."""
    return await record(request)
