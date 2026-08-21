"""
Statistiques d'usage du proxy, agrégées PAR MODÈLE tel que le client l'a
demandé (nom préfixé : « albert/openweight-large », « bigchuck/qwen3 »).

Purement en mémoire, remises à zéro au redémarrage : le proxy n'a pas de
base. Le retour est DYNAMIQUE — un bucket n'existe que si le modèle a
réellement servi au moins une requête, donc /v1/stats ne montre jamais un
modèle sans génération.

Comptage des tokens, par ordre de préférence :
  1. le bloc `usage` renvoyé par l'upstream (exact) — présent sur les
     réponses non streamées, et sur les flux SSE quand le client a
     demandé `stream_options.include_usage` (ou que STATS_FORCE_USAGE
     l'injecte) ;
  2. à défaut, une estimation (CHARS_PER_TOKEN caractères par token) :
     corps de la requête pour l'entrée, deltas de contenu accumulés pour
     la sortie.
Les deux sont comptés séparément (`exact_requests` / `estimated_requests`)
pour que le chiffre affiché reste honnête.

Ce module ne connaît ni FastAPI ni httpx.
"""

import json
import os
import time
from collections import Counter, deque

# Nb d'échantillons de latence gardés par modèle (p95 glissant).
LATENCY_SAMPLES = int(os.environ.get("STATS_LATENCY_SAMPLES", "500"))
# Taille max du corps bufferisé pour lire `usage` sur une réponse non
# streamée ; au-delà on renonce (estimation) plutôt que de gonfler la RAM.
MAX_BODY_BYTES = int(os.environ.get("STATS_MAX_BODY_BYTES", str(2 << 20)))
# Même approximation que le limiteur (albert.CHARS_PER_TOKEN).
CHARS_PER_TOKEN = 4

STARTED_AT = time.time()
_started_mono = time.monotonic()


def _est(chars: int) -> int:
    return max(chars // CHARS_PER_TOKEN, 1) if chars else 0


class UsageCollector:
    """Lit les tokens dans le flux de réponse upstream, sans le retenir.

    - réponse JSON (non streamée) : le corps est bufferisé (borné) puis
      parsé à la fin pour son `usage` ;
    - flux SSE : parsing incrémental ligne à ligne — on retient le dernier
      `usage` non nul vu, et on accumule la longueur des deltas de contenu
      comme filet de sécurité quand l'upstream n'envoie aucun `usage`.
    """

    def __init__(self, content_type: str):
        ct = (content_type or "").lower()
        self.sse = "text/event-stream" in ct
        self.json = not self.sse and "json" in ct
        self._line = bytearray()
        self._body = bytearray()
        self._overflow = False
        self.usage: dict | None = None
        self.out_chars = 0

    def feed(self, chunk: bytes) -> None:
        try:
            if self.sse:
                self._feed_sse(chunk)
            elif self.json and not self._overflow:
                if len(self._body) + len(chunk) > MAX_BODY_BYTES:
                    self._overflow = True
                    self._body.clear()
                else:
                    self._body += chunk
        except Exception:
            # Une réponse au schéma inattendu ne doit jamais casser le relais.
            self.sse = self.json = False

    def _feed_sse(self, chunk: bytes) -> None:
        self._line += chunk
        while True:
            nl = self._line.find(b"\n")
            if nl < 0:
                break
            line = bytes(self._line[:nl]).strip()
            del self._line[: nl + 1]
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == b"[DONE]":
                continue
            try:
                event = json.loads(payload)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            usage = event.get("usage")
            if isinstance(usage, dict):
                self.usage = usage
            for choice in event.get("choices") or []:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta") or choice.get("message") or {}
                content = delta.get("content") if isinstance(delta, dict) else None
                if isinstance(content, str):
                    self.out_chars += len(content)

    def finish(self) -> None:
        """Fin du flux : parse le corps JSON bufferisé le cas échéant."""
        if self.json and not self._overflow and self._body:
            try:
                doc = json.loads(bytes(self._body))
            except (json.JSONDecodeError, UnicodeDecodeError):
                doc = None
            if isinstance(doc, dict):
                usage = doc.get("usage")
                if isinstance(usage, dict):
                    self.usage = usage
                else:
                    # /v1/embeddings & co. : pas d'usage → longueur du corps.
                    self.out_chars = self.out_chars or 0
        self._body.clear()
        self._line.clear()

    def tokens(self, fallback_prompt: int) -> tuple[int, int, bool]:
        """(prompt_tokens, completion_tokens, exact)."""
        u = self.usage or {}
        p = u.get("prompt_tokens")
        c = u.get("completion_tokens")
        if isinstance(p, int) or isinstance(c, int):
            return (p if isinstance(p, int) else fallback_prompt,
                    c if isinstance(c, int) else _est(self.out_chars),
                    True)
        return fallback_prompt, _est(self.out_chars), False


class ModelStats:
    """Compteurs d'un modèle. Créé à la PREMIÈRE requête le concernant :
    un modèle jamais appelé n'a pas d'entrée, donc n'apparaît pas."""

    def __init__(self, key: str, backend: str, model: str):
        self.key = key
        self.backend = backend
        self.model = model
        self.requests = 0
        self.ok = 0
        self.errors = 0
        self.streamed = 0
        self.exact_requests = 0      # tokens issus de l'`usage` upstream
        self.estimated_requests = 0  # tokens estimés (pas d'`usage`)
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.status_codes: Counter = Counter()
        self.endpoints: Counter = Counter()
        self.first_request = 0.0
        self.last_request = 0.0
        self.latency_sum = 0.0
        self.latency_min = None
        self.latency_max = 0.0
        self.latencies: deque = deque(maxlen=LATENCY_SAMPLES)

    def record(self, endpoint: str, status: int, latency: float,
               prompt_tokens: int, completion_tokens: int,
               exact: bool, streamed: bool) -> None:
        now = time.time()
        self.requests += 1
        if 200 <= status < 400:
            self.ok += 1
        else:
            self.errors += 1
        if streamed:
            self.streamed += 1
        if exact:
            self.exact_requests += 1
        else:
            self.estimated_requests += 1
        self.prompt_tokens += max(prompt_tokens, 0)
        self.completion_tokens += max(completion_tokens, 0)
        self.status_codes[str(status)] += 1
        self.endpoints[endpoint] += 1
        self.first_request = self.first_request or now
        self.last_request = now
        self.latency_sum += latency
        self.latency_max = max(self.latency_max, latency)
        self.latency_min = latency if self.latency_min is None \
            else min(self.latency_min, latency)
        self.latencies.append(latency)

    def _p(self, q: float) -> float:
        if not self.latencies:
            return 0.0
        ordered = sorted(self.latencies)
        idx = min(int(q * len(ordered)), len(ordered) - 1)
        return round(ordered[idx], 3)

    def snapshot(self) -> dict:
        total = self.prompt_tokens + self.completion_tokens
        return {
            "object": "model.stats",
            "id": self.key,
            "backend": self.backend,
            "model": self.model,
            "requests": self.requests,
            "requests_ok": self.ok,
            "requests_error": self.errors,
            "streamed_requests": self.streamed,
            "usage": {
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": total,
            },
            "tokens_accounting": {
                "exact_requests": self.exact_requests,
                "estimated_requests": self.estimated_requests,
            },
            "avg_tokens_per_request": round(total / self.requests, 1)
            if self.requests else 0.0,
            "latency_seconds": {
                "avg": round(self.latency_sum / self.requests, 3)
                if self.requests else 0.0,
                "min": round(self.latency_min or 0.0, 3),
                "max": round(self.latency_max, 3),
                "p95": self._p(0.95),
            },
            "status_codes": dict(sorted(self.status_codes.items())),
            "endpoints": dict(sorted(self.endpoints.items())),
            "first_request": round(self.first_request, 3),
            "last_request": round(self.last_request, 3),
        }


_models: dict[str, ModelStats] = {}
# Compteur de changements : incrémenté à chaque requête enregistrée (et à
# chaque reset). Le tableau de bord le compare à celui de son dernier
# rendu : inchangé → rien n'a bougé, il ne se redessine pas.
_revision = 0


def record(model_key: str, backend: str, model: str, endpoint: str,
           status: int, latency: float, prompt_tokens: int,
           completion_tokens: int, exact: bool, streamed: bool) -> None:
    """Enregistre UNE requête. `model_key` est le nom préfixé demandé par
    le client — c'est lui qui crée (à la volée) le bucket."""
    if not model_key:
        return
    global _revision
    _revision += 1
    entry = _models.get(model_key)
    if entry is None:
        entry = _models[model_key] = ModelStats(model_key, backend, model)
    entry.record(endpoint, status, latency, prompt_tokens,
                 completion_tokens, exact, streamed)


def snapshot() -> dict:
    """Vue complète : uniquement les modèles ayant réellement généré."""
    # Ordre d'APPARITION, volontairement stable : le tableau de bord ne
    # voit donc jamais ses lignes se réordonner sous le curseur.
    data = [m.snapshot() for m in _models.values()]
    totals = {
        "models": len(data),
        "requests": sum(m["requests"] for m in data),
        "requests_ok": sum(m["requests_ok"] for m in data),
        "requests_error": sum(m["requests_error"] for m in data),
        "streamed_requests": sum(m["streamed_requests"] for m in data),
        "prompt_tokens": sum(m["usage"]["prompt_tokens"] for m in data),
        "completion_tokens": sum(m["usage"]["completion_tokens"] for m in data),
        "total_tokens": sum(m["usage"]["total_tokens"] for m in data),
    }
    per_backend: dict[str, dict] = {}
    for m in data:
        b = per_backend.setdefault(m["backend"], {
            "models": 0, "requests": 0, "total_tokens": 0,
        })
        b["models"] += 1
        b["requests"] += m["requests"]
        b["total_tokens"] += m["usage"]["total_tokens"]
    return {
        "object": "list",
        "data": data,
        "totals": totals,
        "backends": dict(sorted(per_backend.items())),
        "revision": _revision,
        "since": round(STARTED_AT, 3),
        "uptime_seconds": round(time.monotonic() - _started_mono, 1),
    }


def reset() -> None:
    global _revision
    _models.clear()
    _revision += 1
