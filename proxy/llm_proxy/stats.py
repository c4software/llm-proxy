"""
Statistiques d'usage du proxy, agrégées PAR MODÈLE tel que le client l'a
demandé (nom préfixé : « albert/openweight-large », « bigchuck/qwen3 »).

Une ligne SQLite par requête servie (table `requests`) : toute fenêtre
temporelle est calculable après coup. La lecture suit la forme de
l'Usage API d'OpenAI (`/v1/organization/usage/completions`) : le tableau
de bord consomme la même chose qu'un client tiers.

L'écriture ne touche jamais la boucle d'événements : record() dépose la
ligne dans une file et rend la main ; un unique thread écrivain vide la
file par lots et purge les lignes trop vieilles (stats.retention_days).
La base est en WAL, les lecteurs ouvrent leur propre connexion et ne
bloquent donc pas l'écrivain.

Les exécutions d'outils hébergés ont leur table, `tool_calls`, déposée
par tools.Hosted.run dans la même file. Elle ne porte QUE des mesures :
ni requête de recherche, ni URL, ni résultat — ce que les utilisateurs
cherchent ou lisent n'a pas à survivre dans une base de statistiques.
Une réponse qui a utilisé des outils garde UNE ligne dans `requests`,
avec son usage de tokens cumulé.

Comptage des tokens, par ordre de préférence :
  1. le bloc `usage` renvoyé par l'upstream (exact) — présent sur les
     réponses non streamées, et sur les flux SSE quand le client a
     demandé `stream_options.include_usage` ;
  2. à défaut, une estimation (CHARS_PER_TOKEN caractères par token) :
     corps de la requête pour l'entrée, deltas de contenu accumulés pour
     la sortie.
Les deux sont comptés séparément (`num_estimated_requests` dans la
réponse) pour que le chiffre affiché reste honnête.

Chaque ligne porte `api_key_id`, le condensé de la clé présentée
(jamais la clé elle-même), «» pour un proxy ouvert. Il n'est passé à
aucune fonction : l'auth le pose dans CLIENT (variable de contexte),
record() et record_tool() l'y lisent, loin de la route.

Ce module ne connaît ni FastAPI ni httpx2.
"""

import contextvars
import json
import logging
import os
import queue
import re
import sqlite3
import threading
import time

from . import config

log = logging.getLogger("albert-proxy")

# Le client de la requête en cours : condensé de sa clé (tools.owner), «»
# pour un proxy ouvert. Posé par l'application à l'entrée de chaque requête.
CLIENT: contextvars.ContextVar[str] = contextvars.ContextVar(
    "stats_client", default="")

DB_PATH = config.resolve(config.text("stats.database", "stats.db"))
# Purge des lignes plus vieilles que N jours (0 = conservation illimitée).
RETENTION_DAYS = config.num("stats.retention_days", 90)
# Réponse non streamée : seules la TÊTE et la QUEUE du corps sont
# retenues, TAIL_BYTES chacune — `usage` vit à l'une ou l'autre, jamais au
# milieu d'une réponse volumineuse.
TAIL_BYTES = 64 << 10
# Flux SSE : une ligne sans retour chariot ne peut pas dépasser ceci —
# au-delà ce n'est pas du SSE, on cesse de l'inspecter.
MAX_LINE_BYTES = 1 << 20
# Même approximation que le limiteur (albert.CHARS_PER_TOKEN).
CHARS_PER_TOKEN = 4


def _est(chars: int) -> int:
    return max(chars // CHARS_PER_TOKEN, 1) if chars else 0


# Ce qu'on cherche dans le flux, SANS le parser : json.loads sur chaque
# événement SSE pèse sur la boucle d'événements à chaque token.
#   * _CONTENT_RE : la valeur de `content` quand c'est une chaîne — on
#     n'en retient que la longueur. `"content":null` ne matche pas, ni
#     `"reasoning_content"` (le guillemet ouvrant est exigé), ni un
#     `\"content\"` échappé dans un texte ;
#   * _USAGE_RE : `"usage":{` — l'OBJET, pas le `"usage":null` qu'OpenAI
#     répète sur chaque chunk quand include_usage est demandé.
_CONTENT_RE = re.compile(rb'"content"\s*:\s*"((?:[^"\\]|\\.)*)"')
_USAGE_RE = re.compile(rb'"usage"\s*:\s*\{')
_DECODER = json.JSONDecoder()


def _chars(raw: bytes) -> int:
    """Longueur en CARACTÈRES d'une chaîne JSON encore échappée, sans la
    désérialiser : octets décodés, moins un par échappement («\\n»,
    «\\"»…). Un «\\uXXXX» reste compté 5 — rare, l'upstream envoie de
    l'UTF-8 brut — et c'est une estimation à 4 caractères par token."""
    text = raw.decode("utf-8", "ignore")
    return len(text) - text.count("\\")


def _usage_at(buf: bytes, pos: int) -> dict | None:
    """Décode l'objet JSON ouvert en `pos` (un `{`) ; None s'il n'est pas
    un `usage` plausible. raw_decode s'arrête à l'accolade fermante : le
    reste du corps n'est jamais parsé."""
    try:
        obj, _ = _DECODER.raw_decode(
            buf[pos:pos + TAIL_BYTES].decode(errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return None
    if isinstance(obj, dict) and (
        isinstance(obj.get("prompt_tokens"), int)
        or isinstance(obj.get("completion_tokens"), int)
        or isinstance(obj.get("total_tokens"), int)
    ):
        return obj
    return None


class UsageCollector:
    """Lit les tokens dans le flux de réponse upstream, sans le retenir et
    SANS le désérialiser — le relais doit rester le plus léger possible.

    - réponse JSON (non streamée) : seules la tête et la queue du corps
      (TAIL_BYTES chacune) sont conservées ; à la fin, on y cherche
      `"usage":{` et on ne décode que cet objet-là ;
    - flux SSE : sur chaque lot de lignes complètes, une regex accumule
      la longueur des deltas de contenu (filet de sécurité quand
      l'upstream n'envoie aucun `usage`) et une autre repère l'éventuel
      objet `usage`, seul morceau décodé.
    """

    def __init__(self, content_type: str):
        ct = (content_type or "").lower()
        self.sse = "text/event-stream" in ct
        self.json = not self.sse and "json" in ct
        self._buf = bytearray()   # SSE : ligne en cours ; JSON : queue
        self._head = bytearray()  # JSON : tête du corps
        self.usage: dict | None = None
        self.out_chars = 0

    def feed(self, chunk: bytes) -> bytes:
        """Inspecte `chunk` et le rend TEL QUEL : ce robinet est
        l'identité. (Même interface qu'anthropic_api.Translator, qui
        lui réécrit — relaying.forward ne distingue pas les deux.)"""
        try:
            if self.sse:
                self._feed_sse(chunk)
            elif self.json:
                if len(self._head) < TAIL_BYTES:
                    self._head += chunk[:TAIL_BYTES - len(self._head)]
                self._buf += chunk
                # Amorti : on ne tronque qu'une fois sur deux tailles.
                if len(self._buf) > 2 * TAIL_BYTES:
                    del self._buf[:-TAIL_BYTES]
        except Exception:
            # Une réponse au schéma inattendu ne doit jamais casser le relais.
            self.sse = self.json = False
            self._buf.clear()
            self._head.clear()
        return chunk

    def _feed_sse(self, chunk: bytes) -> None:
        # Cas courant (un flush par événement, tampon vide) : le chunk
        # est inspecté tel quel, sans passer par le tampon.
        if not self._buf and chunk.endswith(b"\n"):
            done = chunk
        else:
            self._buf += chunk
            nl = self._buf.rfind(b"\n")
            if nl < 0:
                if len(self._buf) > MAX_LINE_BYTES:
                    self.sse = False
                    self._buf.clear()
                return
            done = bytes(self._buf[:nl + 1])
            del self._buf[:nl + 1]
        for m in _CONTENT_RE.finditer(done):
            self.out_chars += _chars(m.group(1))
        for m in _USAGE_RE.finditer(done):
            usage = _usage_at(done, m.end() - 1)
            if usage is not None:
                self.usage = usage

    def finish(self) -> bytes:
        """Fin du flux : sur une réponse JSON, cherche `usage` dans la
        queue puis la tête conservées — dernière occurrence d'abord,
        c'est celle du niveau racine. Rien à émettre : tout est déjà
        parti par feed()."""
        if self.json:
            for part in (self._buf, self._head):
                if self.usage is not None:
                    break
                buf = bytes(part)
                for m in reversed(list(_USAGE_RE.finditer(buf))):
                    usage = _usage_at(buf, m.end() - 1)
                    if usage is not None:
                        self.usage = usage
                        break
        self._buf.clear()
        self._head.clear()
        return b""

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

    def cached(self) -> int:
        return cached_tokens(self.usage)


def cached_tokens(usage) -> int:
    """Tokens d'entrée servis depuis le cache de préfixe du backend :
    `prompt_tokens_details.cached_tokens` (OpenAI, llama.cpp, vLLM…).
    0 si l'upstream ne le dit pas — jamais une valeur inventée."""
    if not isinstance(usage, dict):
        return 0
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict) and isinstance(details.get("cached_tokens"), int):
        return max(details["cached_tokens"], 0)
    return 0


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
  streamed          INTEGER NOT NULL,
  cached_tokens     INTEGER NOT NULL DEFAULT 0,
  api_key_id        TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS requests_ts       ON requests(ts);
CREATE INDEX IF NOT EXISTS requests_ts_model ON requests(ts, model_key);
"""

INSERT = """
INSERT INTO requests (ts, model_key, backend, model, endpoint, status,
                      latency, prompt_tokens, completion_tokens, exact,
                      streamed, cached_tokens, api_key_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# Une ligne par exécution d'outil hébergé.
#   endpoint   la route par où l'outil a été appelé : /v1/responses,
#              /v1/messages (les boucles du proxy) ou /v1/tools (direct) ;
#   model_key  le modèle PRÉFIXÉ de la conversation ; vide pour l'appel
#              direct, où le proxy ne voit pas de modèle ;
#   outcome    «ok», «error» (le résultat est un texte «Error: …») ou
#              «limit» (refus par limite d'appels : rien n'a été exécuté) ;
#   duration   secondes d'exécution (0 pour un refus) ;
#   result_chars  caractères du résultat rendu au modèle ;
#   api_key_id le client (CLIENT, en tête de module).
# Volontairement AUCUNE colonne de contenu (arguments, requête, URL,
# résultat) : le journal applicatif les montre, les statistiques non.
TOOLS_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_calls (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  ts           REAL    NOT NULL,
  tool         TEXT    NOT NULL,
  endpoint     TEXT    NOT NULL,
  model_key    TEXT    NOT NULL DEFAULT '',
  outcome      TEXT    NOT NULL,
  duration     REAL    NOT NULL,
  result_chars INTEGER NOT NULL,
  api_key_id   TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS tool_calls_ts ON tool_calls(ts);
"""

INSERT_TOOL = """
INSERT INTO tool_calls (ts, tool, endpoint, model_key, outcome, duration,
                        result_chars, api_key_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""

TOOL_OUTCOMES = ("ok", "error", "limit")
# Les tables à lignes horodatées : la purge et reset() les parcourent.
TABLES = ("requests", "tool_calls")

# Colonnes ajoutées après coup : une base existante les reçoit par ALTER
# TABLE au démarrage, avec leur défaut. (table, nom, définition)
ADDED_COLUMNS = (
    ("requests", "cached_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("requests", "api_key_id", "TEXT NOT NULL DEFAULT ''"),
    ("tool_calls", "api_key_id", "TEXT NOT NULL DEFAULT ''"),
)
# Index sur des colonnes ajoutées : créés APRÈS la migration (le schéma
# initial les nommerait avant qu'une vieille base ait la colonne).
ADDED_INDEXES = """
CREATE INDEX IF NOT EXISTS requests_key_ts   ON requests(api_key_id, ts);
CREATE INDEX IF NOT EXISTS tool_calls_key_ts ON tool_calls(api_key_id, ts);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    for table, name, ddl in ADDED_COLUMNS:
        present = {row[1] for row in
                   conn.execute(f"PRAGMA table_info({table})")}
        if name not in present:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            log.info("stats : colonne %s.%s ajoutée à la base existante",
                     table, name)
    conn.executescript(ADDED_INDEXES)
    conn.commit()


def _connect(path: str | None = None) -> sqlite3.Connection:
    # Résolu à l'appel, pas à la définition : un test qui remplace
    # DB_PATH doit ouvrir SA base, jamais celle du déploiement.
    conn = sqlite3.connect(path or DB_PATH, timeout=10.0,
                           check_same_thread=False)
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
    removed = sum(conn.execute(f"DELETE FROM {table} WHERE ts < ?",
                               (cutoff,)).rowcount for table in TABLES)
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
            # Chaque élément de la file est (INSERT de sa table, ligne).
            for sql in (INSERT, INSERT_TOOL):
                rows = [r for s, r in batch if s is sql]
                if rows:
                    conn.executemany(sql, rows)
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
        conn.executescript(TOOLS_SCHEMA)
        conn.commit()
        _migrate(conn)
        _purge(conn)
        _writer = threading.Thread(target=_write_loop, args=(conn,),
                                   name="stats-writer", daemon=True)
        _writer.start()
    total, oldest = 0, None
    with _reader() as r:
        row = r.execute("SELECT COUNT(*), MIN(ts) FROM requests").fetchone()
        total, oldest = row[0], row[1]
        tool_runs = r.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
    log.info(
        "stats : %s | %d requête(s) déjà en base%s | %d exécution(s) "
        "d'outil | rétention %s",
        DB_PATH, total,
        f" depuis {time.strftime('%Y-%m-%d', time.localtime(oldest))}"
        if oldest else "", tool_runs,
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
           completion_tokens: int, exact: bool, streamed: bool,
           cached_tokens: int = 0) -> None:
    """Enregistre UNE requête. `model_key` est le nom préfixé demandé par
    le client. Non bloquant : la ligne part dans la file d'écriture."""
    if not model_key:
        return
    row = (time.time(), model_key, backend, model, "/" + endpoint.strip("/"),
           int(status), max(float(latency), 0.0), max(int(prompt_tokens), 0),
           max(int(completion_tokens), 0), int(bool(exact)),
           int(bool(streamed)), max(int(cached_tokens), 0), CLIENT.get())
    _enqueue(INSERT, row)


def record_tool(tool: str, endpoint: str, model_key: str, outcome: str,
                duration: float, result_chars: int) -> None:
    """Enregistre UNE exécution d'outil hébergé (voir TOOLS_SCHEMA). Non
    bloquant, comme record(). `endpoint` : la route d'appel ; `model_key` :
    le modèle préfixé de la conversation, vide pour l'appel direct.
    Aucun paramètre ne porte de contenu, et ce n'est pas un oubli.

    Sans écrivain (stats.init() pas appelé : un outil exécuté hors de
    l'application) rien n'est déposé — personne ne viderait la file, et
    ses lignes finiraient dans la première base ouverte ensuite."""
    if _writer is None or not tool or outcome not in TOOL_OUTCOMES:
        return
    _enqueue(INSERT_TOOL, (
        time.time(), tool, "/" + endpoint.strip("/"), model_key or "",
        outcome, max(float(duration), 0.0), max(int(result_chars), 0),
        CLIENT.get()))


def _enqueue(sql: str, row: tuple) -> None:
    global _dropped
    try:
        _pending.put_nowait((sql, row))
    except queue.Full:
        _dropped += 1
        if _dropped % 100 == 1:
            log.warning("stats : file saturée, %d ligne(s) perdue(s) "
                        "(le relais n'est pas affecté)", _dropped)


# ── Usage API (forme OpenAI) ────────────────────────────────────────────
#
# Deux écarts au schéma, tous deux additifs (un SDK OpenAI ignore ce
# qu'il ne connaît pas) :
#   * bucket_width accepte « all » : un seul seau couvrant toute la plage ;
#   * chaque `result` porte des champs en plus (erreurs, flux, estimations,
#     latences, première et dernière requête).

BUCKET_WIDTHS = {"1m": 60, "1h": 3600, "1d": 86_400}
# Défauts d'OpenAI : une journée de minutes, une journée d'heures, une
# semaine de jours.
DEFAULT_LIMITS = {"1m": 60, "1h": 24, "1d": 7, "all": 1}
MAX_LIMIT = 180
# Seuls `model` et `api_key_id` portent de l'information. Les autres sont
# acceptés, pour ne pas casser un client, mais rendent une valeur nulle.
GROUP_FIELDS = ("model", "project_id", "user_id", "api_key_id", "batch",
                "service_tier")

# Agrégat d'un couple (seau, modèle). Avec bucket_width=all la largeur
# vaut toute la plage, donc l'indice `b` est 0 partout.
_AGG = """
SELECT CAST((ts - ?) / ? AS INTEGER)      AS b,
       {model}                            AS mk,
       {key}                              AS k,
       SUM(prompt_tokens),
       SUM(completion_tokens),
       COUNT(*),
       SUM(status NOT BETWEEN 200 AND 399),
       SUM(streamed),
       SUM(NOT exact),
       SUM(endpoint = '/v1/messages'),
       SUM(cached_tokens),
       SUM(latency),
       MAX(latency),
       MIN(ts),
       MAX(ts)
FROM requests WHERE ts >= ? AND ts < ?{filter}
GROUP BY b, mk, k
ORDER BY b, mk, k
"""

def parse_group_by(values, fields=GROUP_FIELDS) -> list[str]:
    """`group_by` accepte la forme répétée (?group_by=a&group_by=b) et la
    forme séparée par des virgules. Un champ inconnu est une erreur, comme
    à l'upstream. `fields` : ceux de la route interrogée."""
    out = []
    for value in values or []:
        for field in str(value).split(","):
            field = field.strip()
            if not field:
                continue
            if field not in fields:
                raise ValueError(
                    f"group_by « {field} » inconnu ; attendu : "
                    + ", ".join(fields)
                )
            if field not in out:
                out.append(field)
    return out


def recording_since(keys=None) -> float:
    """Horodatage de la plus ancienne requête connue (0 si base vide) ;
    celles des clés `keys` seulement, si elles sont données."""
    where, tail = _key_filter(keys)
    with _reader() as conn:
        return conn.execute("SELECT MIN(ts) FROM requests WHERE 1" + where,
                            tail).fetchone()[0] or 0.0


def recent(since: float) -> list[tuple]:
    """Les requêtes faites depuis `since`, ce qu'il faut pour remplir les
    fenêtres du partage équitable au démarrage (fairshare.seed) :
    (ts, api_key_id, backend, endpoint, tokens d'entrée + de sortie)."""
    with _reader() as conn:
        return conn.execute(
            "SELECT ts, api_key_id, backend, endpoint, "
            "prompt_tokens + completion_tokens FROM requests "
            "WHERE ts > ? AND api_key_id != ''", (since,)).fetchall()


def default_limit(bucket_width: str) -> int:
    return DEFAULT_LIMITS.get(bucket_width, 7)


def _empty_page() -> dict:
    return {"object": "page", "data": [], "has_more": False,
            "next_page": None}


def _model_filter(models) -> tuple[str, tuple]:
    """Filtre `models` : les ids sont ceux que le client emploie, donc les
    noms PRÉFIXÉS (« albert/openweight-large »)."""
    models = [str(m) for m in models if str(m)]
    if not models:
        return "", ()
    return (" AND model_key IN (%s)" % ", ".join("?" * len(models)),
            tuple(models))


def _key_filter(keys) -> tuple[str, tuple]:
    """Filtre `api_key_ids` : None = toutes les clés ; une liste = celles-là
    seulement («» : les requêtes sans clé, celles d'un proxy ouvert)."""
    if keys is None:
        return "", ()
    keys = tuple(str(k) for k in keys)
    if not keys:
        return " AND 0", ()
    return (" AND api_key_id IN (%s)" % ", ".join("?" * len(keys)), keys)


def _usage_page(start_time: int, end_time: int | None, bucket_width: str,
                limit: int | None, page: str | None, empty: bool,
                results) -> dict:
    """La page d'Usage API, commune à toutes les routes d'usage du proxy :
    fenêtre, découpage en seaux alignés, pagination, forme page → bucket.
    Ce qui diffère d'une route à l'autre lui est passé :
    `results(origin, width, lo, hi)` lit les agrégats de la plage [lo, hi[
    et rend, par ligne, (indice du seau, horodatage de sa première ligne,
    l'objet `result`).

    `empty` : un filtre porte sur une dimension que le proxy ne
    possède pas (projet, utilisateur, clé nommée, lot). Aucune ligne ne
    peut y correspondre — on rend une page vide plutôt que d'ignorer
    silencieusement le filtre et de sur-déclarer l'usage."""
    if empty:
        return _empty_page()
    now = int(time.time())
    start_time = max(int(start_time), 0)
    end_time = int(end_time) if end_time else now
    if end_time <= start_time:
        return _empty_page()

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
        return _empty_page()

    lo = origin + first * width
    hi = min(lo + count * width, end_time) if single else lo + count * width

    grouped: dict[int, list] = {}
    seen_first: dict[int, float] = {}
    for b, first_ts, result in results(origin, width, lo, hi):
        grouped.setdefault(b, []).append(result)
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


def usage_completions(start_time: int, end_time: int | None = None,
                      bucket_width: str = "1d", group_by=(),
                      limit: int | None = None, page: str | None = None,
                      models=(), empty: bool = False, keys=None) -> dict:
    """`/v1/organization/usage/completions` : les requêtes relayées, par
    seau et — avec group_by=model / api_key_id — par modèle, par clé.
    `keys` : les seules clés comptées (None : toutes). Fenêtre, seaux et
    pagination : _usage_page."""
    model_expr = "model_key" if "model" in list(group_by) else "NULL"
    key_expr = "api_key_id" if "api_key_id" in list(group_by) else "NULL"
    where, tail = _model_filter(models)
    kwhere, ktail = _key_filter(keys)
    where, tail = where + kwhere, tail + ktail

    def results(origin, width, lo, hi):
        with _reader() as conn:
            rows = conn.execute(_AGG.format(model=model_expr, key=key_expr,
                                            filter=where),
                                (origin, width, lo, hi) + tail).fetchall()
        for (b, mk, key, prompt, completion, requests, errors, streamed,
             estimated, anthropic, cached, latency_sum, latency_max,
             first_ts, last_ts) in rows:
            yield b, first_ts, {
                "object": "organization.usage.completions.result",
                "input_tokens": prompt or 0,
                "output_tokens": completion or 0,
                # Inclus dans input_tokens, comme chez OpenAI (cached_tokens).
                # L'audio reste inconnu : 0.
                "input_cached_tokens": cached or 0,
                "input_audio_tokens": 0,
                "output_audio_tokens": 0,
                "num_model_requests": requests,
                "project_id": None,
                "user_id": None,
                "api_key_id": key,
                "model": mk,
                "batch": None,
                # ── extensions du proxy (hors schéma OpenAI) ──
                "num_errors": errors or 0,
                "num_streamed_requests": streamed or 0,
                "num_estimated_requests": estimated or 0,
                "num_anthropic_requests": anthropic or 0,
                # Latences recomposables d'un seau à l'autre (somme, maximum) :
                # un percentile ne le serait pas, il n'y en a donc pas.
                "total_latency_seconds": round(latency_sum or 0.0, 3),
                "avg_latency_seconds": round((latency_sum or 0.0) / requests, 3)
                if requests else 0.0,
                "max_latency_seconds": round(latency_max or 0.0, 3),
                "first_request_time": int(first_ts),
                "last_request_time": int(last_ts),
            }

    return _usage_page(start_time, end_time, bucket_width, limit, page,
                       empty, results)


# ── Usage des outils hébergés ───────────────────────────────────────────
#
# Aucune route de l'Usage API d'OpenAI ne couvre les appels d'outils en
# général (`web_search_calls` ne compte que des recherches, sans issue ni
# durée) : `/tools` est une route SŒUR, propre au proxy, à la forme
# commune. `num_requests` y est le nombre d'appels ; `tool` et `endpoint`
# sont des dimensions du proxy. Tout s'additionne ou se maximise.

TOOL_GROUP_FIELDS = ("tool", "endpoint", "model", "project_id", "user_id",
                     "api_key_id")

_TOOL_AGG = """
SELECT CAST((ts - ?) / ? AS INTEGER)      AS b,
       {tool}                             AS t,
       {endpoint}                         AS e,
       {model}                            AS mk,
       {key}                              AS k,
       COUNT(*),
       SUM(outcome = 'error'),
       SUM(outcome = 'limit'),
       SUM(duration),
       MAX(duration),
       SUM(result_chars),
       MIN(ts),
       MAX(ts)
FROM tool_calls WHERE ts >= ? AND ts < ?{filter}
GROUP BY b, t, e, mk, k
ORDER BY b, t, e, mk, k
"""


def usage_tools(start_time: int, end_time: int | None = None,
                bucket_width: str = "1d", group_by=(),
                limit: int | None = None, page: str | None = None,
                models=(), empty: bool = False, keys=None) -> dict:
    """`/v1/organization/usage/tools` : les exécutions d'outils hébergés,
    par seau et — selon group_by — par outil, par route d'appel, par
    modèle, par clé. Mêmes paramètres et même page que usage_completions."""
    group_by = list(group_by)
    exprs = {
        "tool": "tool" if "tool" in group_by else "NULL",
        "endpoint": "endpoint" if "endpoint" in group_by else "NULL",
        # Appel direct (/v1/tools) : pas de modèle, donc null — pas «».
        "model": "NULLIF(model_key, '')" if "model" in group_by else "NULL",
        "key": "api_key_id" if "api_key_id" in group_by else "NULL",
    }
    where, tail = _model_filter(models)
    kwhere, ktail = _key_filter(keys)
    where, tail = where + kwhere, tail + ktail

    def results(origin, width, lo, hi):
        with _reader() as conn:
            rows = conn.execute(_TOOL_AGG.format(filter=where, **exprs),
                                (origin, width, lo, hi) + tail).fetchall()
        for (b, tool, endpoint, mk, key, calls, errors, limited, duration_sum,
             duration_max, chars, first_ts, last_ts) in rows:
            # Un refus par limite n'exécute rien (durée 0) : la moyenne
            # porte sur les appels réellement lancés.
            ran = calls - (limited or 0)
            yield b, first_ts, {
                "object": "organization.usage.tools.result",
                "num_requests": calls,
                "project_id": None,
                "user_id": None,
                "api_key_id": key,
                "model": mk,
                # ── dimensions et mesures du proxy ──
                "tool": tool,
                "endpoint": endpoint,
                # Le résultat rendu au modèle commençait par «Error:».
                "num_errors": errors or 0,
                # Refus par limite d'appels (tools.max_calls, max_uses du
                # client, limite propre à l'outil) : comptés à part, ce
                # ne sont pas des pannes de l'outil.
                "num_limited": limited or 0,
                "total_duration_seconds": round(duration_sum or 0.0, 3),
                "avg_duration_seconds": round((duration_sum or 0.0) / ran, 3)
                if ran else 0.0,
                "max_duration_seconds": round(duration_max or 0.0, 3),
                # Caractères rendus au modèle, tous résultats confondus.
                "result_chars": chars or 0,
                "first_request_time": int(first_ts),
                "last_request_time": int(last_ts),
            }

    return _usage_page(start_time, end_time, bucket_width, limit, page,
                       empty, results)


def reset() -> None:
    """Efface l'historique persistant."""
    with _reader() as conn:
        for table in TABLES:
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
