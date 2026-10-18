"""
La BASE des statistiques (SQLite, data/stats.db) : schéma, migrations,
écriture en file par un fil dédié, purge, et ce que l'on y range — une
ligne par requête, une par exécution d'outil hébergé.
"""

import contextvars
import logging
import os
import queue
import sqlite3
import threading
import time
from ...core import config


log = logging.getLogger("albert-proxy")


# Le client de la requête en cours : condensé de sa clé (tools.owner), «»
# pour un proxy ouvert. Posé par l'application à l'entrée de chaque requête.
CLIENT: contextvars.ContextVar[str] = contextvars.ContextVar(
    "stats_client", default="")


DB_PATH = config.resolve(config.text("stats.database", "stats.db"))


# Purge des lignes plus vieilles que N jours (0 = conservation illimitée).
RETENTION_DAYS = config.num("stats.retention_days", 90)


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


def reset() -> None:
    """Efface l'historique persistant."""
    with _reader() as conn:
        for table in TABLES:
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
