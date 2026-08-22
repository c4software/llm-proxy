"""
Statistiques d'usage du proxy, agrégées PAR MODÈLE tel que le client l'a
demandé (nom préfixé : « albert/openweight-large », « bigchuck/qwen3 »).

PERSISTANTES : une ligne SQLite par requête servie (table `requests`),
donc les compteurs survivent au redémarrage et toute fenêtre temporelle
est calculable après coup — c'est ce qui rend possibles les vues
All / Week / Day du tableau de bord.

La lecture se fait par UNE SEULE route, à la forme de l'Usage API
d'OpenAI (`/v1/organization/usage/completions`) : le tableau de bord
consomme la même chose qu'un client tiers.

L'écriture ne touche jamais la boucle d'événements : record() dépose la
ligne dans une file et rend la main ; un unique thread écrivain vide la
file par lots et purge les lignes trop vieilles (stats.retention_days).
La base est en WAL, les lecteurs ouvrent leur propre connexion et ne
bloquent donc pas l'écrivain.

Le retour reste DYNAMIQUE — un modèle n'apparaît que s'il a réellement
servi au moins une requête dans la fenêtre demandée.

Comptage des tokens, par ordre de préférence :
  1. le bloc `usage` renvoyé par l'upstream (exact) — présent sur les
     réponses non streamées, et sur les flux SSE quand le client a
     demandé `stream_options.include_usage` ;
  2. à défaut, une estimation (CHARS_PER_TOKEN caractères par token) :
     corps de la requête pour l'entrée, deltas de contenu accumulés pour
     la sortie.
Les deux sont comptés séparément (`num_estimated_requests` dans la
réponse) pour que le chiffre affiché reste honnête.

Ce module ne connaît ni FastAPI ni httpx.
"""

import json
import logging
import os
import queue
import sqlite3
import threading
import time

from . import config

log = logging.getLogger("albert-proxy")

DB_PATH = config.resolve(config.text("stats.database", "stats.db"))
# Purge des lignes plus vieilles que N jours (0 = conservation illimitée).
RETENTION_DAYS = config.num("stats.retention_days", 90)
# Taille max du corps bufferisé pour lire `usage` sur une réponse non
# streamée ; au-delà on renonce (estimation) plutôt que de gonfler la RAM.
MAX_BODY_BYTES = config.integer("stats.max_body_bytes", 2 << 20)
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


# ── Base ────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                REAL    NOT NULL,
  model_key         TEXT    NOT NULL,
  backend           TEXT    NOT NULL,
  model             TEXT    NOT NULL,
  endpoint          TEXT    NOT NULL,
  status            INTEGER NOT NULL,
  latency           REAL    NOT NULL,
  prompt_tokens     INTEGER NOT NULL,
  completion_tokens INTEGER NOT NULL,
  exact             INTEGER NOT NULL,
  streamed          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS requests_ts       ON requests(ts);
CREATE INDEX IF NOT EXISTS requests_ts_model ON requests(ts, model_key);
"""

INSERT = """
INSERT INTO requests (ts, model_key, backend, model, endpoint, status,
                      latency, prompt_tokens, completion_tokens, exact,
                      streamed)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _connect(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


# File d'écriture : record() y dépose et rend la main immédiatement, la
# boucle d'événements n'attend jamais le disque. Bornée : sous un afflux
# anormal on préfère perdre des lignes de STATISTIQUES que de la mémoire.
_pending: queue.Queue = queue.Queue(maxsize=10_000)
_dropped = 0
_writer: threading.Thread | None = None
_lock = threading.Lock()


def _purge(conn: sqlite3.Connection) -> None:
    if RETENTION_DAYS <= 0:
        return
    cutoff = time.time() - RETENTION_DAYS * 86_400
    removed = conn.execute("DELETE FROM requests WHERE ts < ?",
                           (cutoff,)).rowcount
    conn.commit()
    if removed:
        log.info("stats : %d ligne(s) de plus de %.0f jours purgée(s)",
                 removed, RETENTION_DAYS)


def _write_loop(conn: sqlite3.Connection) -> None:
    """Unique écrivain : vide la file par lots, purge une fois par heure."""
    next_purge = time.monotonic()
    while True:
        try:
            row = _pending.get()
        except Exception:  # pragma: no cover - arrêt du processus
            return
        if row is None:  # sentinelle d'arrêt
            return
        batch = [row]
        stopping = False
        while len(batch) < 500:
            try:
                extra = _pending.get_nowait()
            except queue.Empty:
                break
            if extra is None:  # sentinelle croisée pendant le lot
                stopping = True
                break
            batch.append(extra)
        try:
            conn.executemany(INSERT, batch)
            conn.commit()
        except sqlite3.Error as exc:
            log.error("stats : écriture impossible (%d ligne(s)) : %s",
                      len(batch), exc)
        if stopping:
            # Le lot en cours est écrit AVANT de sortir : à l'arrêt, on ne
            # perd pas les requêtes déjà servies.
            return
        if time.monotonic() >= next_purge:
            next_purge = time.monotonic() + 3600
            try:
                _purge(conn)
            except sqlite3.Error as exc:
                log.error("stats : purge impossible : %s", exc)


def init() -> None:
    """Ouvre/crée la base et démarre l'écrivain. Idempotent."""
    global _writer
    with _lock:
        if _writer is not None:
            return
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        conn = _connect()
        conn.executescript(SCHEMA)
        conn.commit()
        _purge(conn)
        _writer = threading.Thread(target=_write_loop, args=(conn,),
                                   name="stats-writer", daemon=True)
        _writer.start()
    total, oldest = 0, None
    with _reader() as r:
        row = r.execute("SELECT COUNT(*), MIN(ts) FROM requests").fetchone()
        total, oldest = row[0], row[1]
    log.info(
        "stats : %s | %d requête(s) déjà en base%s | rétention %s",
        DB_PATH, total,
        f" depuis {time.strftime('%Y-%m-%d', time.localtime(oldest))}"
        if oldest else "",
        f"{RETENTION_DAYS:.0f} j" if RETENTION_DAYS > 0 else "illimitée",
    )


def close() -> None:
    """Vide la file et arrête l'écrivain (arrêt propre du proxy)."""
    global _writer
    if _writer is None:
        return
    _pending.put(None)
    _writer.join(timeout=5.0)
    _writer = None


class _reader:
    """Connexion de lecture éphémère (WAL : ne bloque pas l'écrivain)."""

    def __enter__(self) -> sqlite3.Connection:
        self.conn = _connect()
        return self.conn

    def __exit__(self, *exc) -> None:
        self.conn.close()


def record(model_key: str, backend: str, model: str, endpoint: str,
           status: int, latency: float, prompt_tokens: int,
           completion_tokens: int, exact: bool, streamed: bool) -> None:
    """Enregistre UNE requête. `model_key` est le nom préfixé demandé par
    le client. Non bloquant : la ligne part dans la file d'écriture."""
    if not model_key:
        return
    global _dropped
    row = (time.time(), model_key, backend, model, "/" + endpoint.strip("/"),
           int(status), max(float(latency), 0.0), max(int(prompt_tokens), 0),
           max(int(completion_tokens), 0), int(bool(exact)),
           int(bool(streamed)))
    try:
        _pending.put_nowait(row)
    except queue.Full:
        _dropped += 1
        if _dropped % 100 == 1:
            log.warning("stats : file saturée, %d ligne(s) perdue(s) "
                        "(le relais n'est pas affecté)", _dropped)


# ── Usage API (forme OpenAI) ────────────────────────────────────────────
#
# Deux écarts au schéma, tous deux additifs (un SDK OpenAI ignore ce
# qu'il ne connaît pas, la compatibilité reste entière) :
#   * bucket_width accepte « all » : un seul seau couvrant toute la
#     plage — un p95 ne se recompose pas à partir de seaux plus fins ;
#   * chaque `result` porte en plus num_errors, num_streamed_requests,
#     num_estimated_requests, p95_latency_seconds, first_request_time
#     et last_request_time.

BUCKET_WIDTHS = {"1m": 60, "1h": 3600, "1d": 86_400}
# Défauts d'OpenAI : une journée de minutes, une journée d'heures, une
# semaine de jours.
DEFAULT_LIMITS = {"1m": 60, "1h": 24, "1d": 7, "all": 1}
MAX_LIMIT = 180
# Le proxy n'a ni projets, ni comptes utilisateurs, ni clés nommées :
# seul `model` porte de l'information. Les autres sont acceptés — pour ne
# pas casser un client — mais rendent une valeur nulle.
GROUP_FIELDS = ("model", "project_id", "user_id", "api_key_id", "batch",
                "service_tier")

# Agrégat d'un couple (seau, modèle). Avec bucket_width=all la largeur
# vaut toute la plage, donc l'indice `b` est 0 partout.
_AGG = """
SELECT CAST((ts - ?) / ? AS INTEGER)      AS b,
       {model}                            AS mk,
       SUM(prompt_tokens),
       SUM(completion_tokens),
       COUNT(*),
       SUM(status NOT BETWEEN 200 AND 399),
       SUM(streamed),
       SUM(NOT exact),
       MIN(ts),
       MAX(ts)
FROM requests WHERE ts >= ? AND ts < ?{filter}
GROUP BY b, mk
ORDER BY b, mk
"""

# p95 du même découpage : la latence dont le rang vaut
# min(int(0,95·n), n−1) dans sa partition.
_P95 = """
SELECT b, mk, latency FROM (
  SELECT CAST((ts - ?) / ? AS INTEGER) AS b, {model} AS mk, latency,
         ROW_NUMBER() OVER (PARTITION BY CAST((ts - ?) / ? AS INTEGER),
                                         {model} ORDER BY latency) - 1 AS rn,
         COUNT(*)     OVER (PARTITION BY CAST((ts - ?) / ? AS INTEGER),
                                         {model})                      AS n
  FROM requests WHERE ts >= ? AND ts < ?{filter}
)
WHERE rn = MIN(CAST(n * 0.95 AS INTEGER), n - 1)
"""


def parse_group_by(values) -> list[str]:
    """`group_by` accepte la forme répétée (?group_by=a&group_by=b) et la
    forme séparée par des virgules. Un champ inconnu est une erreur, comme
    à l'upstream."""
    out = []
    for value in values or []:
        for field in str(value).split(","):
            field = field.strip()
            if not field:
                continue
            if field not in GROUP_FIELDS:
                raise ValueError(
                    f"group_by « {field} » inconnu ; attendu : "
                    + ", ".join(GROUP_FIELDS)
                )
            if field not in out:
                out.append(field)
    return out


def recording_since() -> float:
    """Horodatage de la plus ancienne requête connue (0 si base vide)."""
    with _reader() as conn:
        return conn.execute("SELECT MIN(ts) FROM requests").fetchone()[0] or 0.0


def default_limit(bucket_width: str) -> int:
    return DEFAULT_LIMITS.get(bucket_width, 7)


def usage_completions(start_time: int, end_time: int | None = None,
                      bucket_width: str = "1d", group_by=(),
                      limit: int | None = None, page: str | None = None,
                      models=(), empty: bool = False) -> dict:
    """`empty` : un filtre porte sur une dimension que le proxy ne
    possède pas (projet, utilisateur, clé nommée, lot). Aucune ligne ne
    peut y correspondre — on rend une page vide plutôt que d'ignorer
    silencieusement le filtre et de sur-déclarer l'usage."""
    if empty:
        return {"object": "page", "data": [], "has_more": False,
                "next_page": None}
    now = int(time.time())
    start_time = max(int(start_time), 0)
    end_time = int(end_time) if end_time else now
    if end_time <= start_time:
        return {"object": "page", "data": [], "has_more": False,
                "next_page": None}
    group_by = list(group_by)
    by_model = "model" in group_by
    model_expr = "model_key" if by_model else "NULL"

    single = bucket_width == "all"
    if single:
        # Un seul seau : sa largeur, c'est la plage entière.
        width = max(end_time - start_time, 1)
        origin = start_time
        total = 1
    else:
        width = BUCKET_WIDTHS.get(bucket_width, 86_400)
        # Seaux alignés sur des multiples de leur largeur depuis l'epoch,
        # comme chez OpenAI : deux appels décalés rendent les mêmes bornes.
        origin = (start_time // width) * width
        total = ((end_time - 1) // width) - (origin // width) + 1

    first = max(int(page or 0), 0)
    count = min(limit if limit else default_limit(bucket_width), MAX_LIMIT)
    count = max(min(count, total - first), 0)
    if count <= 0:
        return {"object": "page", "data": [], "has_more": False,
                "next_page": None}

    lo = origin + first * width
    hi = min(lo + count * width, end_time) if single else lo + count * width
    # Filtre `models` : les ids sont ceux que le client emploie, donc les
    # noms PRÉFIXÉS (« albert/openweight-large »).
    models = [str(m) for m in models if str(m)]
    where = ""
    tail = ()
    if models:
        where = " AND model_key IN (%s)" % ", ".join("?" * len(models))
        tail = tuple(models)
    args_agg = (origin, width, lo, hi) + tail
    args_p95 = (origin, width, origin, width, origin, width, lo, hi) + tail

    with _reader() as conn:
        rows = conn.execute(_AGG.format(model=model_expr, filter=where),
                            args_agg).fetchall()
        p95 = {(b, mk): latency for b, mk, latency in
               conn.execute(_P95.format(model=model_expr, filter=where),
                            args_p95)}

    grouped: dict[int, list] = {}
    seen_first: dict[int, float] = {}
    for (b, mk, prompt, completion, requests, errors, streamed, estimated,
         first_ts, last_ts) in rows:
        grouped.setdefault(b, []).append({
            "object": "organization.usage.completions.result",
            "input_tokens": prompt or 0,
            "output_tokens": completion or 0,
            # Le proxy ne sait rien du cache ni de l'audio : 0, jamais
            # une valeur inventée.
            "input_cached_tokens": 0,
            "input_audio_tokens": 0,
            "output_audio_tokens": 0,
            "num_model_requests": requests,
            "project_id": None,
            "user_id": None,
            "api_key_id": None,
            "model": mk,
            "batch": None,
            # ── extensions du proxy (hors schéma OpenAI) ──
            "num_errors": errors or 0,
            "num_streamed_requests": streamed or 0,
            "num_estimated_requests": estimated or 0,
            "p95_latency_seconds": round(p95.get((b, mk), 0.0), 3),
            "first_request_time": int(first_ts),
            "last_request_time": int(last_ts),
        })
        seen_first[b] = min(seen_first.get(b, first_ts), first_ts)

    def bounds(i: int) -> tuple[int, int]:
        if not single:
            return origin + i * width, origin + (i + 1) * width
        # Seau unique : on le borne sur les données réellement présentes,
        # ce qui donne au client la date du premier enregistrement sans
        # ajouter de champ au schéma.
        return int(seen_first.get(i, lo)), end_time

    data = []
    for i in range(first, first + count):
        lo_i, hi_i = bounds(i)
        data.append({
            "object": "bucket",
            "start_time": lo_i,
            "end_time": hi_i,
            "results": grouped.get(i, []),
        })
    more = first + count < total
    return {
        "object": "page",
        "data": data,
        "has_more": more,
        "next_page": str(first + count) if more else None,
    }


def reset() -> None:
    """Efface l'historique persistant."""
    with _reader() as conn:
        conn.execute("DELETE FROM requests")
        conn.commit()
