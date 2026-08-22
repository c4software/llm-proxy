"""
Tout ce qui est SPÉCIFIQUE À ALBERT : le limiteur de quotas, qui
temporise les requêtes pour rester sous ses limites — fenêtres MINUTE
(rpm/tpm) et JOUR (rpd/tpd), limites exactes du compte chargées depuis
GET /v1/me/info.

Le limiteur retarde plutôt que rejeter — sauf si l'attente nécessaire
dépasse MAX_QUEUE_SECONDS (quota JOURNALIER épuisé) : l'appelant reçoit
QuotaWaitTooLong et renvoie un 429 local avec Retry-After.

── Association routeurs ↔ modèles ──────────────────────────────────────
/v1/me/info donne les limites par router_id ; /v1/models donne les modèles
(id + aliases) mais PAS leur router_id (vérifié sur l'API réelle). Le
mapping est donc reconstruit ainsi, par priorité :

  1. [quotas.router_models] (config.toml, <router_id> = ["préfixe", ...])
     — mapping manuel explicite, prioritaire sur tout ;
  2. signature : chaque groupe de modèles est rattaché à une famille
     ([quotas.family_limits], préfixes testés sur le nom complet, sa partie après
     « / », et chaque alias) ; si un seul routeur du compte porte les
     (rpm, tpm) de cette famille, l'association est certaine. Si plusieurs
     routeurs candidats existent mais ont TOUTES leurs limites identiques
     (minute ET jour), l'attribution arbitraire est numériquement sans
     effet et acceptée ; sinon la famille reste sur ses limites statiques
     (sans fenêtres jour).

Dans tous les cas, /v1/models sert à unifier id et aliases dans un même
bucket : « openai/gpt-oss-120b » et « openweight-large » partagent le même
compteur.

Note comptage : sur /v1/chat/completions Albert ne comptabilise QUE les
tokens des messages envoyés (~4 chars/token estimés ici), pas la sortie.

Ce module ne connaît ni FastAPI ni httpx. Chaque backend à quotas de
main.py instancie SA QuotaState : deux backends « albert » avec des clés
différentes ont chacun leurs limiteurs, leur association routeurs et
leur refresh. refresh() reçoit le Backend (attributs .client,
.auth_headers() et .meta_timeout).
"""

import asyncio
import json
import logging
import time
from collections import deque

from . import config

log = logging.getLogger("albert-proxy")

MARGIN = config.num("quotas.margin", 0.9)
LIMITS_REFRESH = config.num("quotas.limits_refresh", 3600)
MAX_QUEUE_SECONDS = config.num("quotas.max_queue_seconds", 900)
GENERIC_RPM = config.integer("quotas.generic_rpm", 30)
GENERIC_TPM = config.integer("quotas.generic_tpm", 128000)
STATUS_INTERVAL = config.num("quotas.status_interval", 600)

DEFAULT_FAMILY_LIMITS = {
    "deepseek": {"rpm": 50, "tpm": 246_000, "models": ["deepseek"]},
    "mistral": {
        "rpm": 50, "tpm": 128_000,
        "models": ["openweight-small", "openweight-medium",
                   "mistral", "ministral"],
    },
    "qwen": {"rpm": 50, "tpm": 128_000, "models": ["openweight-code", "qwen"]},
    "gptoss": {"rpm": 10, "tpm": 128_000, "models": ["openweight-large", "gpt-oss"]},
}

# ── Constantes internes (modifiables ici, pas d'env dédiée) ──────────────
# Estimation de tokens : Albert compte les tokens des messages envoyés ;
# faute de tokenizer, on approxime à N caractères par token.
CHARS_PER_TOKEN = 4
# Longueur max des extraits JSON bruts loggés en cas de schéma non reconnu.
LOG_EXCERPT_CHARS = 280

MINUTE = 60.0
DAY = 86_400.0


def load_family_limits() -> dict:
    parsed = config.get("quotas.family_limits")
    if not parsed:
        return DEFAULT_FAMILY_LIMITS
    if not isinstance(parsed, dict):
        raise SystemExit("quotas.family_limits : table attendue")
    for fam, cfg in parsed.items():
        for key in ("rpm", "tpm", "models"):
            if key not in cfg:
                raise SystemExit(
                    f"quotas.family_limits.{fam} : champ '{key}' manquant")
    return parsed


def load_router_models() -> dict[int, list[str]]:
    """Clés du TOML = router_id (en chaîne, un nom de clé TOML l'est
    toujours), valeurs = préfixes de modèles."""
    parsed = config.get("quotas.router_models")
    if not parsed:
        return {}
    try:
        return {int(rid): [str(p).lower() for p in prefixes]
                for rid, prefixes in parsed.items()}
    except (AttributeError, ValueError, TypeError) as exc:
        raise SystemExit(f"quotas.router_models invalide : {exc}")


class QuotaWaitTooLong(Exception):
    def __init__(self, delay: float, window_name: str):
        self.delay = delay
        self.window_name = window_name


class Window:
    """Fenêtre glissante (minute ou jour). max_req/max_tok à 0 = illimité."""

    def __init__(self, seconds: float, max_req: int, max_tok: int):
        self.seconds = seconds
        self.max_req = max_req
        self.max_tok = max_tok
        self.events: deque = deque()  # (timestamp, tokens)
        self.tokens = 0

    def evict(self, now: float) -> None:
        cutoff = now - self.seconds
        while self.events and self.events[0][0] <= cutoff:
            _, tok = self.events.popleft()
            self.tokens -= tok

    def wait_needed(self, now: float, cost: int) -> float:
        waits = []
        if self.max_req and len(self.events) >= self.max_req:
            waits.append(self.events[0][0] + self.seconds - now)
        if self.max_tok and self.tokens + cost > self.max_tok:
            need = self.tokens + cost - self.max_tok
            freed = 0
            for ts, tok in self.events:
                freed += tok
                if freed >= need:
                    waits.append(ts + self.seconds - now)
                    break
            else:
                if self.events:
                    waits.append(self.events[-1][0] + self.seconds - now)
        return max(waits) if waits else 0.0

    def record(self, now: float, cost: int) -> None:
        if self.max_req or self.max_tok:
            self.events.append((now, cost))
            self.tokens += cost

    def snapshot(self) -> dict:
        return {
            "requests": len(self.events),
            "tokens": self.tokens,
            "max_requests": self.max_req or None,
            "max_tokens": self.max_tok or None,
        }


class Limiter:
    """Un bucket = un routeur du compte, une famille, ou un générique.
    Verrou tenu pendant l'attente : ordre d'arrivée respecté dans le
    bucket, aucun head-of-line blocking entre buckets."""

    def __init__(self, name: str, source: str,
                 rpm: int = 0, tpm: int = 0, rpd: int = 0, tpd: int = 0):
        self.name = name
        self.source = source  # "compte" | "famille" | "générique"
        self.minute = Window(MINUTE, rpm, tpm)
        self.day = Window(DAY, rpd, tpd)
        self.lock = asyncio.Lock()

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

    async def acquire(self, cost: int) -> float:
        waited = 0.0
        async with self.lock:
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
                await asyncio.sleep(step)
                waited += step
            now = time.monotonic()
            self.minute.record(now, cost)
            self.day.record(now, cost)
        return waited

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
ROUTER_MODELS = load_router_models()

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


def _m(value: int) -> int:
    return int(value * MARGIN) if value > 0 else 0


def _family_of(name: str) -> str | None:
    """Famille d'un nom de modèle : préfixes testés sur le nom complet ET
    sur sa partie après « / » (les id Albert sont préfixés par l'orga,
    ex. « openai/gpt-oss-120b »)."""
    n = name.lower()
    basename = n.rsplit("/", 1)[-1]
    for prefix, fam in PREFIX_MAP:
        if n.startswith(prefix) or basename.startswith(prefix):
            return fam
    return None


def _parse_me_info_limits(doc) -> dict[int, dict[str, int]]:
    """UserInfo.limits : liste de {router_id, type: tpm|tpd|rpm|rpd,
    value: int|null}. null/absent = illimité (0 chez nous)."""
    out: dict[int, dict[str, int]] = {}
    limits = doc.get("limits") if isinstance(doc, dict) else None
    if not isinstance(limits, list):
        return {}
    for entry in limits:
        if not isinstance(entry, dict):
            continue
        rid = entry.get("router_id")
        ltype = entry.get("type")
        value = entry.get("value")
        if not isinstance(rid, int) or ltype not in ("rpm", "tpm", "rpd", "tpd"):
            continue
        slot = out.setdefault(rid, {"rpm": 0, "tpm": 0, "rpd": 0, "tpd": 0})
        slot[ltype] = value if isinstance(value, int) and value > 0 else 0
    return out


def _parse_model_groups(doc) -> list[list[str]]:
    """/v1/models → groupes [id + aliases] (noms en minuscules)."""
    groups = []
    data = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(data, list):
        return []
    for model in data:
        if not isinstance(model, dict):
            continue
        names = [model.get("id")] + list(model.get("aliases") or [])
        names = [n.lower() for n in names if isinstance(n, str) and n]
        if names:
            groups.append(names)
    return groups


def _associate_routers(
    rlimits: dict[int, dict[str, int]],
    groups: list[list[str]],
) -> tuple[dict[int, list[str]], dict[str, str], dict[str, str]]:
    """Associe chaque GROUPE de modèles (id + aliases) à un routeur.

    Sur l'API réelle, un routeur = un modèle (vérifié : autant de routeurs
    que d'entrées au catalogue). L'association se fait par priorité :
      1. ROUTER_MODELS (mapping manuel, préfixes) ;
      2. signature (rpm, tpm) de la famille du groupe : bijection entre
         les groupes d'une signature et les routeurs qui la portent,
         chaque groupe recevant SON routeur. Quand plusieurs routeurs
         partagent la signature, l'attribution n'est acceptée que s'ils
         sont strictement identiques (fenêtres jour comprises) — le choix
         est alors numériquement sans effet. Sinon, limites statiques de
         la famille (sans fenêtres jour).

    Retourne (router_families, name_to_bucket, bucket_display)."""
    manual_prefix: list[tuple[str, int]] = sorted(
        ((p, rid) for rid, prefixes in ROUTER_MODELS.items() for p in prefixes),
        key=lambda x: len(x[0]), reverse=True,
    )

    # Routeurs regroupés par signature (rpm, tpm), avec test d'uniformité.
    sig_pool: dict[tuple[int, int], list[int]] = {}
    for rid, l in rlimits.items():
        sig_pool.setdefault((l["rpm"], l["tpm"]), []).append(rid)
    sig_uniform = {
        sig: len({tuple(sorted(rlimits[r].items())) for r in rids}) == 1
        for sig, rids in sig_pool.items()
    }

    assigned: set[int] = set()
    warned_sigs: set[tuple[int, int]] = set()
    n2b: dict[str, str] = {}
    display: dict[str, str] = {}
    r_fams: dict[int, list[str]] = {}

    def manual_bucket(names: list[str]) -> str | None:
        for n in names:
            base = n.rsplit("/", 1)[-1]
            for prefix, rid in manual_prefix:
                if n.startswith(prefix) or base.startswith(prefix):
                    assigned.add(rid)
                    return f"router:{rid}"
        return None

    # Ordre déterministe : groupes triés par nom canonique.
    for names in sorted(groups, key=lambda ns: ns[0]):
        fam = next((f for f in (_family_of(n) for n in names) if f), None)
        key = manual_bucket(names)

        if key is None and fam is not None:
            sig = (FAMILY_LIMITS[fam]["rpm"], FAMILY_LIMITS[fam]["tpm"])
            candidates = sorted(sig_pool.get(sig, []))
            if candidates and (len(candidates) == 1 or sig_uniform[sig]):
                free = [r for r in candidates if r not in assigned]
                rid = free[0] if free else candidates[0]
                if not free:
                    log.warning(
                        "signature %s : plus de routeurs libres que de "
                        "groupes attendus — %r partagera le routeur %d",
                        sig, names[0], rid,
                    )
                assigned.add(rid)
                key = f"router:{rid}"
            elif candidates:
                if sig not in warned_sigs:
                    warned_sigs.add(sig)
                    log.warning(
                        "signature %s : routeurs %s aux limites journalières "
                        "différentes — impossible de trancher, familles "
                        "concernées en limites statiques (sans fenêtres "
                        "jour). Fixer via ROUTER_MODELS.",
                        sig, candidates,
                    )
                key = fam
            else:
                key = fam

        if key is None:
            key = fam if fam else f"generic:{min(names, key=len)}"

        if key.startswith("router:") and fam:
            rid = int(key.split(":", 1)[1])
            fams = r_fams.setdefault(rid, [])
            if fam not in fams:
                fams.append(fam)

        for n in names:
            n2b[n] = key
        short = min(names, key=len)
        if key not in display or len(short) < len(display[key]):
            display[key] = short

    return r_fams, n2b, display


class QuotaState:
    """Quotas d'UN backend Albert (un compte / une clé) : limiteurs,
    association routeurs ↔ modèles, refresh périodique. Chaque backend
    à quotas de main.py possède la sienne."""

    def __init__(self, name: str):
        self.name = name
        self.router_limits: dict[int, dict[str, int]] = {}  # rid → rpm/tpm/rpd/tpd (0=∞)
        self.name_to_bucket: dict[str, str] = {}            # nom minuscule → clé bucket
        self.bucket_display: dict[str, str] = {}            # clé bucket → nom court
        self.router_families: dict[int, list[str]] = {}     # rid → familles des groupes
        self.limiters: dict[str, Limiter] = {}
        _states.append(self)

    async def refresh(self, backend) -> bool:
        """Charge /v1/me/info + /v1/models du backend et reconstruit
        l'association. N'écrase jamais des données valides par un échec.
        `backend` : objet avec .client (httpx.AsyncClient), .auth_headers()
        et .meta_timeout (secondes)."""
        if backend is None or backend.client is None or not backend.api_key:
            return False
        client = backend.client
        headers = backend.auth_headers()
        timeout = backend.meta_timeout
        try:
            info = await client.get("/v1/me/info", headers=headers, timeout=timeout)
            models = await client.get("/v1/models", headers=headers, timeout=timeout)
        except Exception as exc:
            log.warning("[%s] refresh limites impossible : %s — fallbacks conservés",
                        self.name, exc)
            return False
        if info.status_code != 200 or models.status_code != 200:
            log.warning(
                "[%s] refresh : /v1/me/info→%d, /v1/models→%d — fallbacks conservés",
                self.name, info.status_code, models.status_code,
            )
            return False

        rlimits = _parse_me_info_limits(info.json())
        if not rlimits:
            excerpt = json.dumps(info.json(), ensure_ascii=False)[:LOG_EXCERPT_CHARS]
            log.warning("[%s] /v1/me/info : limits vide/non reconnu. Extrait : %s",
                        self.name, excerpt)
            return False
        groups = _parse_model_groups(models.json())

        r_fams, n2b, display = _associate_routers(rlimits, groups)
        self.router_limits, self.name_to_bucket = rlimits, n2b
        self.bucket_display, self.router_families = display, r_fams

        routed = sorted({k for k in n2b.values() if k.startswith("router:")})
        log.info(
            "[%s] limites du compte : %d routeurs, %d assignés à des groupes "
            "de modèles, %d groupes au catalogue",
            self.name, len(rlimits), len(routed), len(groups),
        )
        for key in routed:
            rid = int(key.split(":", 1)[1])
            l = rlimits[rid]
            log.info(
                "  routeur %d ← %s : %s rpm, %s tpm, %s rpd, %s tpd",
                rid, display.get(key, "?"),
                l["rpm"] or "∞", l["tpm"] or "∞", l["rpd"] or "∞", l["tpd"] or "∞",
            )
        # Répercuter à chaud sur les buckets routeur existants.
        for key, lim in self.limiters.items():
            if key.startswith("router:"):
                rid = int(key.split(":", 1)[1])
                r = rlimits.get(rid)
                if r:
                    lim.update(_m(r["rpm"]), _m(r["tpm"]),
                               _m(r["rpd"]), _m(r["tpd"]), "compte")
        return True

    async def refresh_loop(self, backend) -> None:
        while True:
            await asyncio.sleep(LIMITS_REFRESH)
            await self.refresh(backend)

    def get_limiter(self, payload) -> Limiter:
        """Priorité : bucket routeur (compte) > famille statique > générique."""
        model = ""
        if isinstance(payload, dict):
            model = str(payload.get("model", "") or "")
        m = model.lower()

        key = self.name_to_bucket.get(m)
        if key is None:
            fam = _family_of(m) if m else None
            key = fam if fam else f"generic:{m or 'sans-modele'}"

        lim = self.limiters.get(key)
        if lim is not None:
            return lim

        if key.startswith("router:"):
            rid = int(key.split(":", 1)[1])
            r = self.router_limits[rid]
            lim = Limiter(
                self.bucket_display.get(key, key), "compte",
                rpm=_m(r["rpm"]), tpm=_m(r["tpm"]),
                rpd=_m(r["rpd"]), tpd=_m(r["tpd"]),
            )
            log.info(
                "[%s] bucket routeur %d (%s) créé : %s rpm, %s tpm, %s rpd, %s tpd",
                self.name, rid, lim.name,
                lim.minute.max_req or "∞", lim.minute.max_tok or "∞",
                lim.day.max_req or "∞", lim.day.max_tok or "∞",
            )
        elif key in FAMILY_LIMITS:
            cfg = FAMILY_LIMITS[key]
            lim = Limiter(key, "famille", rpm=_m(cfg["rpm"]), tpm=_m(cfg["tpm"]))
        else:
            lim = Limiter(self.bucket_display.get(key, key), "générique",
                          rpm=_m(GENERIC_RPM), tpm=_m(GENERIC_TPM))
            log.info(
                "[%s] modèle %r hors compte et familles → bucket générique "
                "(%d RPM, %d TPM)", self.name, model, GENERIC_RPM, GENERIC_TPM,
            )
        self.limiters[key] = lim
        return lim

    def snapshot(self) -> dict:
        """Bloc quotas pour /healthz."""
        return {
            "account_limits_loaded": bool(self.router_limits),
            "routers": {
                str(rid): {
                    "assigned_to": self.bucket_display.get(f"router:{rid}"),
                    "families": self.router_families.get(rid, []),
                    **lim,
                }
                for rid, lim in self.router_limits.items()
            },
            "model_buckets": dict(sorted(self.name_to_bucket.items())),
            "rate_limits": {key: lim.snapshot()
                            for key, lim in self.limiters.items()},
        }


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
