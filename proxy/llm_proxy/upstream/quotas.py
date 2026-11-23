"""
Le LIMITEUR DE QUOTAS des backends à `quotas = true` (du TOML ou des
comptes) : temporise les requêtes pour rester sous des fenêtres MINUTE
(rpm/tpm) et JOUR (rpd/tpd), par bucket.

Il retarde plutôt que rejeter, sauf si l'attente dépasse
MAX_QUEUE_SECONDS : QuotaWaitTooLong, que l'appelant traduit en 429 local
avec Retry-After. Le coût d'une requête est estimé à l'entrée (prompt,
CHARS_PER_TOKEN caractères par token), puis recalé sur l'usage réel,
entrée ET sortie (settle).

Le bucket d'un modèle (nom sans préfixe), par priorité :
  1. une règle `limits` du backend (motifs de noms → rpm/tpm/rpd/tpd ; tous
     les modèles d'une règle partagent son bucket) : posée par l'admin, elle
     l'emporte sur ce que le backend publie ;
  2. un routeur du compte, quand le backend en publie les limites
     (Albert : /v1/limits, voir albert.py) ;
  3. une famille de [quotas.family_limits] (préfixes de noms) ;
  4. le générique ([quotas].generic_rpm / generic_tpm), un par modèle ;
     les plafonds qu'annoncent les en-têtes des réponses (rate_headers.py)
     le remplacent.
[quotas].margin s'applique à toutes ces limites.

Quel que soit le bucket, le RESTANT qu'annoncent ces en-têtes retient
aussi les requêtes (Window.told) : l'amont compte ce que le proxy ne voit
pas (un autre client de la même clé).

Chaque backend à quotas a SA QuotaState (deux clés = deux jeux de
limiteurs). Ce module ne connaît ni FastAPI ni httpx2.
"""

import asyncio
import fnmatch
import logging
import time
from collections import deque

from ..core import config
from . import rate_headers

log = logging.getLogger("albert-proxy")

MARGIN = config.num("quotas.margin", 0.9)
LIMITS_REFRESH = config.num("quotas.limits_refresh", 3600)
MAX_QUEUE_SECONDS = config.num("quotas.max_queue_seconds", 900)
GENERIC_RPM = config.integer("quotas.generic_rpm", 30)
GENERIC_TPM = config.integer("quotas.generic_tpm", 128000)
STATUS_INTERVAL = config.num("quotas.status_interval", 600)
# Buckets par backend au plus : au-delà, les modèles inconnus partagent
# un générique commun — un client qui varie les noms ne fait pas
# grossir la mémoire indéfiniment.
MAX_BUCKETS = 64
# Faute de tokenizer, estimation à N caractères par token.
CHARS_PER_TOKEN = 4

MINUTE = 60.0
DAY = 86_400.0


def load_family_limits() -> dict:
    """[quotas.family_limits.<famille>] : rpm, tpm, models (préfixes).
    Aucune : aucune famille."""
    parsed = config.get("quotas.family_limits") or {}
    if not isinstance(parsed, dict):
        raise SystemExit("quotas.family_limits : table attendue")
    for fam, cfg in parsed.items():
        for key in ("rpm", "tpm", "models"):
            if key not in cfg:
                raise SystemExit(
                    f"quotas.family_limits.{fam} : champ '{key}' manquant")
    return parsed


class QuotaWaitTooLong(Exception):
    def __init__(self, delay: float, window_name: str):
        self.delay = delay
        self.window_name = window_name


class ClientGone(Exception):
    """Le client a raccroché pendant l'attente : la requête quitte la
    file SANS consommer de quota — l'envoyer quand même au backend
    coûterait une génération complète pour personne (et un SDK qui
    retente sur timeout empilerait des doublons dans la file)."""


class Window:
    """Fenêtre glissante (minute ou jour). max_req/max_tok à 0 = illimité.
    Chaque événement est une liste [instant, tokens, vivant] : son coût,
    estimé à l'entrée, est recalé sur l'usage réel une fois la réponse
    lue (`adjust`) tant qu'il n'est pas sorti de la fenêtre."""

    def __init__(self, seconds: float, max_req: int, max_tok: int):
        self.seconds = seconds
        self.max_req = max_req
        self.max_tok = max_tok
        self.events: deque = deque()  # [timestamp, tokens, vivant]
        self.tokens = 0
        # Les admissions de la dernière minute, [instant, coût], plafonnée
        # ou non : ce qui est parti depuis une annonce de l'amont.
        self.admitted: deque = deque()
        # La dernière annonce de l'amont (announce) : (instant, {"req" |
        # "tok": [place, valable jusqu'à]}) ; None : rien d'annoncé.
        self.told: tuple | None = None
        # La prochaine requête admise sondera l'amont (_told_wait, record).
        self.probe = False

    def evict(self, now: float) -> None:
        cutoff = now - self.seconds
        while self.events and self.events[0][0] <= cutoff:
            entry = self.events.popleft()
            entry[2] = False
            self.tokens -= entry[1]
        while self.admitted and self.admitted[0][0] <= now - MINUTE:
            self.admitted.popleft()

    def newer_than(self, since: float) -> bool:
        """L'annonce en cours date-t-elle d'une requête admise après `since` ?"""
        return self.told is not None and self.told[0] > since

    def announce(self, since: float, room: dict) -> None:
        """L'amont, au moment où il admettait la requête placée à `since`,
        laissait encore `room` ({"req" | "tok": [place, valable jusqu'à]}) :
        remplace l'annonce précédente."""
        self.told = (since, room)

    def _told_wait(self, now: float, cost: int) -> float:
        """L'attente qu'impose la dernière annonce : jusqu'à la fin de la
        place épuisée par les admissions qui l'ont suivie. Passé ce délai,
        la place toujours épuisée, une seule requête part sonder l'amont ;
        les suivantes attendent sa réponse, une minute au plus. La sonde
        n'est posée qu'à son admission (record) : une requête retenue
        ensuite par autre chose ne bloque personne."""
        self.probe = False
        if self.told is None:
            return 0.0
        since, room = self.told
        newer = [c for ts, c in self.admitted if ts > since]
        used = {"req": len(newer) + 1, "tok": sum(newer) + cost}
        wait, probe = 0.0, False
        for dim, (place, until) in list(room.items()):
            if used[dim] <= place:
                if now >= until:
                    del room[dim]
            elif now < until:
                wait = max(wait, until - now)
            else:
                probe = True
        if probe and not wait:
            self.probe = True
        elif not room:
            self.told = None
        return wait

    def wait_needed(self, now: float, cost: int) -> float:
        waits = [self._told_wait(now, cost)]
        if self.max_req and len(self.events) >= self.max_req:
            waits.append(self.events[0][0] + self.seconds - now)
        if self.max_tok and self.tokens + cost > self.max_tok:
            need = self.tokens + cost - self.max_tok
            freed = 0
            for ts, tok, _ in self.events:
                freed += tok
                if freed >= need:
                    waits.append(ts + self.seconds - now)
                    break
            else:
                if self.events:
                    waits.append(self.events[-1][0] + self.seconds - now)
        return max(waits) if waits else 0.0

    def record(self, now: float, cost: int) -> list | None:
        """Enregistre un événement ; rend son entrée (pour `adjust`), ou
        None si la fenêtre est illimitée (rien n'est gardé)."""
        self.admitted.append([now, cost])
        if self.probe:                  # cette requête est la sonde : plus rien après elle
            self.told, self.probe = (now, {"req": [0, now + MINUTE]}), False
        if self.max_req or self.max_tok:
            entry = [now, cost, True]
            self.events.append(entry)
            self.tokens += cost
            return entry
        return None

    def adjust(self, entry: list | None, tokens: int) -> None:
        """Recale le coût de `entry` sur `tokens`. Sans effet sur un
        événement déjà sorti de la fenêtre (son coût n'y pèse plus)."""
        if entry is None or not entry[2]:
            return
        tokens = max(int(tokens), 0)
        self.tokens += tokens - entry[1]
        entry[1] = tokens

    def frees_in(self, now: float) -> float:
        """Secondes avant que la plus ancienne requête quitte la fenêtre
        (0 : vide)."""
        return max(self.events[0][0] + self.seconds - now, 0.0) if self.events else 0.0

    def snapshot(self) -> dict:
        return {
            "frees_in": round(self.frees_in(time.monotonic()), 1),
            "requests": len(self.events),
            "tokens": self.tokens,
            "max_requests": self.max_req or None,
            "max_tokens": self.max_tok or None,
        }


class Limiter:
    """Un bucket = un routeur du compte, une règle du backend, une famille,
    ou un générique.
    Verrou tenu pendant l'attente : ordre d'arrivée respecté dans le
    bucket, aucun head-of-line blocking entre buckets."""

    def __init__(self, name: str, source: str,
                 rpm: int = 0, tpm: int = 0, rpd: int = 0, tpd: int = 0):
        self.name = name
        # "compte" | "backend" | "famille" | "générique" | "en-têtes"
        self.source = source
        self.minute = Window(MINUTE, rpm, tpm)
        self.day = Window(DAY, rpd, tpd)
        self.lock = asyncio.Lock()
        # Le générique d'UN modèle : les plafonds qu'annoncent les en-têtes
        # le remplacent (announced). Ni une famille ni le générique commun.
        self.own_generic = False
        self.left: dict[str, int] = {}   # restants annoncés par les en-têtes
        self.left_until = 0.0            # … montrés jusque-là

    def update(self, rpm: int, tpm: int, rpd: int, tpd: int, source: str) -> None:
        old = (self.minute.max_req, self.minute.max_tok,
               self.day.max_req, self.day.max_tok)
        if old != (rpm, tpm, rpd, tpd):
            log.info(
                "[%s] limites mises à jour : rpm %s→%s, tpm %s→%s, "
                "rpd %s→%s, tpd %s→%s (%s)",
                self.name, old[0], rpm, old[1], tpm, old[2], rpd, old[3], tpd,
                source,
            )
        self.minute.max_req, self.minute.max_tok = rpm, tpm
        self.day.max_req, self.day.max_tok = rpd, tpd
        self.source = source

    async def acquire(self, cost: int, wait_gone=None,
                      ticket: list | None = None) -> float:
        """Attend une place puis l'enregistre. `wait_gone` : coroutine
        sans argument qui NE REND LA MAIN QU'À LA DÉCONNEXION du client
        (pas de sondage : elle bloque). Mise en course avec l'attente —
        celle du verrou (les requêtes arrivées avant) comme celle du
        quota : si elle termine avant, la requête est abandonnée
        (ClientGone) et le verrou rendu au suivant. Rien n'est lancé quand
        il n'y a pas à attendre, le chemin sans contention ne coûte rien
        de plus. MAX_QUEUE_SECONDS compte TOUTE l'attente, verrou compris.
        `ticket` : liste où ranger (limiteur, entrée minute, entrée jour,
        coût), de quoi recaler la fenêtre sur l'usage réel
        (relaying.Call.done → settle)."""
        started = time.monotonic()
        waited = 0.0
        watcher: asyncio.Future | None = None

        def gone() -> bool:
            """La surveillance a-t-elle vu le client partir ? Une
            surveillance en échec est abandonnée (on attend sans elle)."""
            nonlocal watcher, wait_gone
            if watcher is None or not watcher.done():
                return False
            if not watcher.cancelled() and watcher.exception() is None:
                return True
            log.debug("surveillance de déconnexion perdue : %r",
                      None if watcher.cancelled() else watcher.exception())
            wait_gone, watcher = None, None
            return False

        contended = self.lock.locked()
        try:
            if contended and wait_gone is not None:
                # File derrière une autre requête : le client peut partir
                # avant même d'avoir le verrou.
                watcher = asyncio.ensure_future(wait_gone())
                locking = asyncio.ensure_future(self.lock.acquire())
                try:
                    while not locking.done():
                        await asyncio.wait({locking, watcher},
                                           return_when=asyncio.FIRST_COMPLETED)
                        if not locking.done() and gone():
                            raise ClientGone()
                        if watcher is None:
                            await locking
                except BaseException:
                    if locking.done() and not locking.cancelled():
                        self.lock.release()
                    else:
                        locking.cancel()
                    raise
            else:
                await self.lock.acquire()
            try:
                waited = time.monotonic() - started if contended else 0.0
                while True:
                    now = time.monotonic()
                    self.minute.evict(now)
                    self.day.evict(now)
                    delays = {
                        "minute": self.minute.wait_needed(now, cost),
                        "jour": self.day.wait_needed(now, cost),
                    }
                    worst = max(delays, key=delays.get)
                    delay = delays[worst]
                    if delay <= 0:
                        break
                    if waited + delay > MAX_QUEUE_SECONDS:
                        raise QuotaWaitTooLong(delay, worst)
                    step = min(delay, MINUTE)
                    log.info(
                        "[%s] quota %s atteint : temporisation %.1fs",
                        self.name, worst, step,
                    )
                    if wait_gone is not None and watcher is None:
                        watcher = asyncio.ensure_future(wait_gone())
                    if watcher is None:
                        await asyncio.sleep(step)
                    else:
                        await asyncio.wait({watcher}, timeout=step)
                        if gone():
                            raise ClientGone()
                    waited += step
                now = time.monotonic()
                minute = self.minute.record(now, cost)
                day = self.day.record(now, cost)
                if ticket is not None and (minute or day):
                    ticket.append((self, minute, day, cost))
            finally:
                self.lock.release()
        finally:
            if watcher is not None:
                watcher.cancel()
        return waited

    def adjust(self, minute: list | None, day: list | None,
               tokens: int) -> None:
        self.minute.adjust(minute, tokens)
        self.day.adjust(day, tokens)

    def announced(self, told: dict, since: float) -> None:
        """Ce qu'une réponse de l'amont annonce (rate_headers.parse) pour
        la requête placée à `since` : les plafonds remplacent, type par
        type, ceux du générique du modèle ; le restant, moins la marge,
        retient les requêtes suivantes jusqu'au délai annoncé, une minute
        au plus après `since` (Window.announce). Une annonce plus ancienne
        que celle en cours est ignorée."""
        if self.minute.newer_than(since) or self.day.newer_than(since):
            return
        if self.own_generic:
            caps = {"rpm": self.minute.max_req, "tpm": self.minute.max_tok,
                    "rpd": self.day.max_req, "tpd": self.day.max_tok}
            caps.update({k: max(with_margin(cap), 1) for k, (cap, _, _) in told.items()})
            self.update(caps["rpm"], caps["tpm"], caps["rpd"], caps["tpd"], "en-têtes")
        until = {k: since + min(MINUTE, max(reset, 1.0) if reset is not None else MINUTE)
                 for k, (_, _, reset) in told.items()}
        room = {k: [left - (cap - max(with_margin(cap), 1)), until[k]]
                for k, (cap, left, _) in told.items()}
        for window, req, tok in ((self.minute, "rpm", "tpm"), (self.day, "rpd", "tpd")):
            dims = {d: room[k] for d, k in (("req", req), ("tok", tok)) if k in room}
            if dims:
                window.announce(since, dims)
        self.left = {k: left for k, (_, left, _) in told.items()}
        self.left_until = max(until.values())

    def snapshot(self) -> dict:
        now = time.monotonic()
        self.minute.evict(now)
        self.day.evict(now)
        return {
            "limits_source": self.source,
            "minute": self.minute.snapshot(),
            "day": self.day.snapshot(),
        }


FAMILY_LIMITS = load_family_limits()

PREFIX_MAP: list[tuple[str, str]] = sorted(
    ((prefix.lower(), fam)
     for fam, cfg in FAMILY_LIMITS.items()
     for prefix in cfg["models"]),
    key=lambda pair: len(pair[0]),
    reverse=True,
)

# Toutes les QuotaState créées (pour le cartouche de statut).
_states: list["QuotaState"] = []

_request_count = 0
_last_status_ts = float("-inf")


def with_margin(value: int) -> int:
    return int(value * MARGIN) if value > 0 else 0


def family_of(name: str) -> str | None:
    """Famille d'un nom de modèle : préfixes testés sur le nom complet ET
    sur sa partie après « / » (les id Albert sont préfixés par l'orga,
    ex. « openai/gpt-oss-120b »)."""
    n = name.lower()
    basename = n.rsplit("/", 1)[-1]
    for prefix, fam in PREFIX_MAP:
        if n.startswith(prefix) or basename.startswith(prefix):
            return fam
    return None


class QuotaState:
    """Quotas d'UN backend : ses règles `limits`, les limites de son compte
    quand il en publie (set_account), ses limiteurs."""

    def __init__(self, name: str, limits: list[dict] | None = None):
        self.name = name
        self.rules: list[dict] = list(limits or [])
        self.router_limits: dict[int, dict[str, int]] = {}  # rid → rpm/tpm/rpd/tpd (0=∞)
        self.name_to_bucket: dict[str, str] = {}            # nom minuscule → «router:rid»
        self.bucket_display: dict[str, str] = {}            # clé bucket → nom court
        self.account_left: dict[int, dict[str, int]] = {}   # rid → restants annoncés
        self.account_read = 0.0                             # heure de la lecture (s)
        self.account_tried = 0.0                            # heure du dernier essai (s)
        self.no_account = False                             # pas de limites de compte
        self.budget: list[dict] | None = None               # forfait mensuel (mistral.py)
        self.budget_read = 0.0                              # heure de sa lecture (s)
        self.budget_tried = 0.0                             # heure du dernier essai (s)
        self.no_budget = False                              # pas de forfait
        self.upstream_429 = 0                               # 429 reçus malgré le limiteur
        self.daily_requests = False                         # `requests` à la journée (Groq)
        self.limiters: dict[str, Limiter] = {}
        _states.append(self)

    def close(self) -> None:
        """Le backend est retiré : son état quitte le cartouche."""
        if self in _states:
            _states.remove(self)

    def set_rules(self, limits: list[dict]) -> None:
        """Nouvelles règles `limits` : leurs buckets repartent de zéro, les
        autres (compte, familles, génériques) gardent leurs fenêtres. Une
        requête déjà en file finit sur l'ancien bucket."""
        if limits == self.rules:
            return
        self.rules = list(limits)
        for key in [k for k in self.limiters if k.startswith("rule:")]:
            del self.limiters[key]

    def set_account(self, rlimits: dict, n2b: dict, display: dict, left: dict,
                    read: float) -> None:
        """Les limites lues sur le compte du backend (albert.refresh), avec
        les restants qu'il annonce et l'heure de la lecture : répercutées à
        chaud sur les buckets routeur existants."""
        self.router_limits, self.name_to_bucket = rlimits, n2b
        self.bucket_display, self.account_left, self.account_read = display, left, read
        for key in [k for k in self.limiters if k.startswith("router:")
                    and int(k.split(":", 1)[1]) not in rlimits]:
            del self.limiters[key]          # un routeur que le compte ne publie plus
        for key, lim in self.limiters.items():
            if key.startswith("router:"):
                r = rlimits.get(int(key.split(":", 1)[1]))
                if r:
                    lim.update(with_margin(r["rpm"]), with_margin(r["tpm"]),
                               with_margin(r["rpd"]), with_margin(r["tpd"]), "compte")

    def observe(self, limiter: Limiter, headers, since: float) -> None:
        """Les en-têtes d'une réponse de l'amont, sur le limiteur qui a
        placé sa requête à `since` (Limiter.announced). Les `requests` de
        Groq reconnues à la journée : le rpm qu'elles avaient donné aux
        génériques des modèles revient au générique."""
        daily = self.daily_requests
        told, self.daily_requests = rate_headers.parse(headers, daily)
        if self.daily_requests and not daily:
            for lim in self.limiters.values():
                if lim.source == "en-têtes":
                    lim.update(with_margin(GENERIC_RPM), lim.minute.max_tok,
                               lim.day.max_req, lim.day.max_tok, lim.source)
        if told:
            limiter.announced(told, since)

    def _rule_of(self, m: str) -> int | None:
        base = m.rsplit("/", 1)[-1]
        for i, rule in enumerate(self.rules):
            if any(fnmatch.fnmatchcase(m, p) or fnmatch.fnmatchcase(base, p)
                   for p in rule["models"]):
                return i
        return None

    def bucket_of(self, model: str) -> str:
        """La clé du bucket d'un modèle (nom sans préfixe), sans le créer :
        «rule:<n>», «router:<id>», une famille, ou «generic:<modèle>».
        Priorité : règle du backend > routeur du compte > famille >
        générique."""
        m = model.lower()
        rule = self._rule_of(m) if m else None
        if rule is not None:
            return f"rule:{rule}"
        return self.name_to_bucket.get(m) or (family_of(m) if m else None) \
            or f"generic:{m or 'sans-modele'}"

    def get_limiter(self, payload) -> Limiter:
        """Le limiteur du modèle de `payload`, créé au premier besoin."""
        model = ""
        if isinstance(payload, dict):
            model = str(payload.get("model", "") or "")
        key = self.bucket_of(model)

        lim = self.limiters.get(key)
        if lim is not None:
            return lim

        if key.startswith("router:"):
            rid = int(key.split(":", 1)[1])
            r = self.router_limits[rid]
            lim = Limiter(
                self.bucket_display.get(key, key), "compte",
                rpm=with_margin(r["rpm"]), tpm=with_margin(r["tpm"]),
                rpd=with_margin(r["rpd"]), tpd=with_margin(r["tpd"]),
            )
            log.info(
                "[%s] bucket routeur %d (%s) créé : %s rpm, %s tpm, %s rpd, %s tpd",
                self.name, rid, lim.name,
                lim.minute.max_req or "∞", lim.minute.max_tok or "∞",
                lim.day.max_req or "∞", lim.day.max_tok or "∞",
            )
        elif key.startswith("rule:"):
            r = self.rules[int(key.split(":", 1)[1])]
            lim = Limiter(",".join(r["models"]), "backend",
                          rpm=with_margin(r["rpm"]), tpm=with_margin(r["tpm"]),
                          rpd=with_margin(r["rpd"]), tpd=with_margin(r["tpd"]))
        elif key in FAMILY_LIMITS:
            cfg = FAMILY_LIMITS[key]
            lim = Limiter(key, "famille", rpm=with_margin(cfg["rpm"]), tpm=with_margin(cfg["tpm"]))
        else:
            if len(self.limiters) >= MAX_BUCKETS:
                key = "generic:*"
                lim = self.limiters.get(key)
                if lim is not None:
                    return lim
                log.warning(
                    "[%s] %d buckets atteints : les modèles inconnus "
                    "partagent désormais un générique commun",
                    self.name, MAX_BUCKETS,
                )
            lim = Limiter(key.removeprefix("generic:"), "générique",
                          rpm=with_margin(GENERIC_RPM), tpm=with_margin(GENERIC_TPM))
            lim.own_generic = key != "generic:*"
            log.info(
                "[%s] modèle %r hors compte, règles et familles → bucket "
                "générique (%d RPM, %d TPM)", self.name, model, GENERIC_RPM, GENERIC_TPM,
            )
        self.limiters[key] = lim
        return lim

    def snapshot(self) -> dict:
        """Bloc quotas pour /healthz."""
        return {
            "account_limits_loaded": bool(self.router_limits),
            "account_read": self.account_read or None,
            "upstream_429": self.upstream_429,
            "limits": self.rules,
            "routers": {
                str(rid): {
                    "assigned_to": self.bucket_display.get(f"router:{rid}"),
                    **lim,
                    "remaining": self.account_left.get(rid, {}),
                }
                for rid, lim in self.router_limits.items()
            },
            "model_buckets": dict(sorted(self.name_to_bucket.items())),
            "rate_limits": {key: lim.snapshot()
                            for key, lim in self.limiters.items()},
        }


def settle(ticket: list, tokens: int) -> None:
    """Recale les fenêtres des places prises (`ticket`, rempli par
    Limiter.acquire) sur les tokens réellement consommés : l'estimation
    d'entrée ignore la sortie et surcompte une image en base64. Plusieurs
    places (tours de la boucle des outils hébergés) : le total réparti au
    prorata des estimations."""
    if not ticket:
        return
    estimated = sum(cost for *_, cost in ticket) or 1
    for limiter, minute, day, cost in ticket:
        limiter.adjust(minute, day, round(tokens * cost / estimated))


# Délai entre deux essais tant que les limites du compte n'ont jamais pu
# être lues.
RETRY_UNLOADED = 60.0


def estimate_chat_cost(raw: bytes) -> int:
    return max(len(raw) // CHARS_PER_TOKEN, 1)


def estimate_generic_cost(raw: bytes, payload) -> int:
    cost = max(len(raw) // CHARS_PER_TOKEN, 1)
    if isinstance(payload, dict):
        declared = payload.get("max_completion_tokens") or payload.get("max_tokens")
        if isinstance(declared, int) and declared > 0:
            cost += declared
    return cost


def _fmt(used: int, limit: int) -> str:
    if limit <= 0:
        return "∞"
    return f"{limit - used}/{limit} ({used * 100 // limit}%)"


def maybe_log_status() -> None:
    """Cartouche local (aucun appel réseau), au plus un par
    STATUS_INTERVAL, seulement s'il y a du trafic."""
    global _request_count, _last_status_ts
    if STATUS_INTERVAL <= 0:
        return
    _request_count += 1
    now = time.monotonic()
    if now - _last_status_ts < STATUS_INTERVAL:
        return
    _last_status_ts = now

    lines = [
        "┌─ albert-proxy ── quotas ── %d requêtes servies %s"
        % (_request_count, "─" * 20),
        "│ %-22s %-11s %-28s %s" % ("bucket", "source",
                                    "minute (req | tok)", "jour (req | tok)"),
    ]
    active = False
    for st in _states:
        for key in sorted(st.limiters):
            lim = st.limiters[key]
            lim.minute.evict(now)
            lim.day.evict(now)
            if not lim.minute.events and not lim.day.events:
                continue
            active = True
            shown = lim.name if len(_states) == 1 else f"{st.name}:{lim.name}"
            lines.append(
                "│ %-22s %-11s %-13s | %-12s %-11s | %s" % (
                    shown[:22], f"[{lim.source}]",
                    _fmt(len(lim.minute.events), lim.minute.max_req),
                    _fmt(lim.minute.tokens, lim.minute.max_tok),
                    _fmt(len(lim.day.events), lim.day.max_req),
                    _fmt(lim.day.tokens, lim.day.max_tok),
                )
            )
    if not active:
        lines.append("│ aucun bucket actif")
    lines.append("└" + "─" * 78)
    log.info("\n".join(lines))

