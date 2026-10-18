"""
Le PARTAGE ÉQUITABLE des backends : chaque client a une PART de tokens sur
une heure GLISSANTE. Ce n'est pas un quota — rien n'est refusé, aucun 429 :
une requête dont le client a dépassé sa part ATTEND (relaying.gate) que la
fenêtre redescende sous sa part, puis part. But : qu'un compte (un agent
qui boucle) ne sature pas les backends au détriment des autres.

  * le client : le COMPTE pour une clé gérée, une clé du TOML rattachée ou
    la page connectée ; la clé elle-même pour une clé du TOML seule.
    Proxy ouvert : personne n'est compté (CLIENT reste None) ;
  * la part : [fair_share].tokens_per_hour par défaut (0 = aucune limite),
    que l'admin règle par compte (accounts.py : défaut, illimitée, ou un
    nombre) ;
  * ce qui compte : entrée + sortie de chaque requête relayée (Call.done),
    sauf les routes exemptées du limiteur ([proxy].exempt_paths :
    embeddings, transcription…) et les backends `fair_share = false` ;
  * la fenêtre vit en mémoire ; au démarrage elle est relue dans la base
    de statistiques (seed), pour qu'un redémarrage ne remette pas tout le
    monde à zéro.

Le dépassement est possible, et voulu : une requête n'est comptée qu'une
fois faite (ses tokens ne sont connus qu'alors), plusieurs requêtes
parallèles d'un client sous sa part partent toutes. La part borne la
moyenne, pas la pointe.

Ce module ne connaît ni FastAPI ni les comptes : la part d'un client lui
est donnée par l'appelant (une fonction, relue pendant l'attente — un
admin qui la change libère aussitôt qui attend).
"""

import asyncio
import collections
import contextvars
import threading
import time

from ..core import config

WINDOW = 3600.0
DEFAULT_SHARE = config.integer("fair_share.tokens_per_hour", 0)
if DEFAULT_SHARE < 0:
    raise SystemExit(f"{config.CONFIG_PATH} : fair_share.tokens_per_hour "
                     f"doit être positif ou nul (reçu {DEFAULT_SHARE})")
# Pendant une attente, la part est relue au plus toutes les CHECK_EVERY s.
CHECK_EVERY = 5.0

# Le client de la requête en cours et son compte (None : clé du TOML
# seule) — posés par le middleware d'auth, comme stats.CLIENT. CLIENT
# None = personne n'est compté (proxy ouvert).
CLIENT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "fair_share_client", default=None)
ACCOUNT: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "fair_share_account", default=None)


class ClientGone(Exception):
    """Le client a raccroché pendant l'attente."""


_lock = threading.Lock()
_usage: dict[str, collections.deque] = {}
_waiting = 0


def _prune(q: collections.deque, now: float) -> None:
    while q and q[0][0] <= now - WINDOW:
        q.popleft()


def charge(client: str | None, tokens: int, now: float | None = None) -> None:
    """Ajoute `tokens` à la fenêtre de `client` (None : rien)."""
    if client is None or tokens <= 0:
        return
    now = time.time() if now is None else now
    with _lock:
        q = _usage.setdefault(client, collections.deque())
        q.append((now, int(tokens)))
        _prune(q, now)


def used(client: str, now: float | None = None) -> int:
    """Les tokens de `client` sur la dernière heure."""
    now = time.time() if now is None else now
    with _lock:
        q = _usage.get(client)
        if not q:
            return 0
        _prune(q, now)
        return sum(t for _, t in q)


def free_at(client: str, share: int, now: float | None = None) -> float | None:
    """L'instant où `client` repassera sous sa part, None s'il y est déjà."""
    now = time.time() if now is None else now
    with _lock:
        q = _usage.get(client)
        if not q:
            return None
        _prune(q, now)
        total = sum(t for _, t in q)
        if total < share:
            return None
        for ts, tokens in q:
            total -= tokens
            if total < share:
                return ts + WINDOW
    return now                           # inatteignable : total < share à la fin


async def wait(client: str, share_of, gone) -> float:
    """Attend que `client` soit sous sa part. `share_of()` (coroutine) rend
    la part, None = illimitée ; relue toutes les CHECK_EVERY s. `gone()`
    (coroutine) ne rend la main qu'au départ du client → ClientGone. Rend
    le temps attendu (0 : rien)."""
    global _waiting
    started = time.monotonic()
    watcher = None
    try:
        while True:
            share = await share_of()
            until = None if share is None else free_at(client, share)
            if until is None:
                return time.monotonic() - started if watcher else 0.0
            if watcher is None:
                watcher = asyncio.ensure_future(gone())
                _waiting += 1
            delay = min(max(until - time.time(), 0.05), CHECK_EVERY)
            done, _ = await asyncio.wait({watcher}, timeout=delay)
            if done:
                raise ClientGone()
    finally:
        if watcher is not None:
            _waiting -= 1
            watcher.cancel()


def seed(rows) -> int:
    """Remplit les fenêtres depuis (ts, client, tokens) — la dernière heure
    de la base de statistiques, au démarrage. Rend le nombre de lignes."""
    n = 0
    for ts, client, tokens in sorted(rows):
        charge(client, tokens, now=ts)
        n += 1
    return n


def snapshot() -> dict:
    """Pour /healthz : la part par défaut, les clients suivis, ceux qui
    attendent."""
    with _lock:
        tracked = sum(1 for q in _usage.values() if q)
    return {"tokens_per_hour": DEFAULT_SHARE or None, "window_seconds":
            int(WINDOW), "tracked": tracked, "waiting": _waiting}


def reset() -> None:
    """Tests : fenêtres vides."""
    with _lock:
        _usage.clear()
