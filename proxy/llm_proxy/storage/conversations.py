"""
L'HISTORIQUE du chat de la page : les conversations et leurs images,
gardées en base et liées au COMPTE connecté (auth.ui_client : le compte
OIDC, sinon le condensé de la clé de la page ; «» pour un proxy ouvert).
Le même compte retrouve donc ses conversations d'un navigateur ou d'un
appareil à l'autre. Une conversation incognito n'est jamais envoyée.

  conversations  une ligne par conversation : son document JSON tel que la
                 page le tient (messages, réglages, artifacts, fichiers
                 produits — tout ce qui est du texte), son titre et ses
                 dates pour la liste
  assets         les IMAGES (jointes, générées, retouchées), à part : le
                 document n'en garde que l'adresse, /ui/assets/<id>. Seules
                 les images matricielles (PNG, JPEG, GIF, WebP, reconnues à
                 leurs octets) sont acceptées : servies sous l'origine de
                 la page, rien qui s'y exécute (ni SVG, ni HTML).

Une image que plus aucun message de sa conversation ne cite (réponse
régénérée, message modifié) est effacée au prochain enregistrement de la
conversation, celle d'une conversation qui n'existe pas à la prochaine
lecture de la liste — passé un délai : celle qui vient d'être envoyée
attend que le document qui la cite arrive.

Bornes par compte (taille d'un document, d'une image, du tout), pour
qu'une page qui boucle ne remplisse pas le disque. SQLite, comme la
mémoire (memories.py) : une connexion par opération sous un verrou,
appelée hors de la boucle d'événements.
"""

import json
import re
import secrets
import sqlite3
import threading
import time
import unicodedata

from ..core import config
from . import files

DB_PATH = config.resolve(config.text("ui.conversations_database", "conversations.db"))
MAX_DOC_BYTES = config.integer("ui.conversation_max_bytes", 20_000_000)
MAX_ASSET_BYTES = config.integer("ui.asset_max_bytes", 20_000_000)
MAX_TOTAL_BYTES = config.integer("ui.history_max_bytes", 2_000_000_000)
ASSET_PATH = "/ui/assets/"
# Une image envoyée et pas encore citée n'est pas effacée avant ce délai.
ORPHAN_GRACE = 600.0
MAX_ID = 64
_ID = re.compile(r"[A-Za-z0-9_-]{1,%d}\Z" % MAX_ID)
_CITED = re.compile(re.escape(ASSET_PATH) + r"([A-Za-z0-9_-]{1,%d})" % MAX_ID)


class HistoryError(Exception):
    """Une opération refusée ; le message est pour la page. `status` : le
    code HTTP à rendre (400 requête, 404 inconnu, 413 bornes atteintes)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    # Résolu à l'appel : un test qui remplace DB_PATH ouvre SA base.
    conn = sqlite3.connect(DB_PATH, timeout=10.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("""CREATE TABLE IF NOT EXISTS conversations (
        owner TEXT NOT NULL, id TEXT NOT NULL, title TEXT NOT NULL,
        created REAL NOT NULL, updated REAL NOT NULL, doc TEXT NOT NULL,
        PRIMARY KEY (owner, id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS assets (
        owner TEXT NOT NULL, id TEXT NOT NULL, conversation TEXT NOT NULL,
        type TEXT NOT NULL, name TEXT NOT NULL, data BLOB NOT NULL,
        created REAL NOT NULL, PRIMARY KEY (owner, id))""")
    conn.execute("CREATE INDEX IF NOT EXISTS assets_conv "
                 "ON assets (owner, conversation)")
    conn.execute("CREATE INDEX IF NOT EXISTS conversations_recent "
                 "ON conversations (owner, updated DESC)")
    return conn


def _run(fn):
    """fn(conn) sous le verrou, dans une transaction."""
    with _lock:
        conn = _connect()
        try:
            with conn:
                return fn(conn)
        finally:
            conn.close()


def _check_id(cid) -> str:
    if not isinstance(cid, str) or not _ID.match(cid):
        raise HistoryError("identifiant de conversation invalide")
    return cid


def _used(conn, owner: str) -> int:
    docs, = conn.execute("SELECT coalesce(sum(length(doc)), 0) FROM conversations "
                         "WHERE owner = ?", (owner,)).fetchone()
    blobs, = conn.execute("SELECT coalesce(sum(length(data)), 0) FROM assets "
                          "WHERE owner = ?", (owner,)).fetchone()
    return docs + blobs


def list_all(owner: str) -> list[dict]:
    """Les conversations du compte, documents complets, la plus récente
    d'abord. Au passage, les images d'une conversation qui n'existe pas
    (jamais enregistrée, ou supprimée pendant leur envoi) sont effacées,
    passé ORPHAN_GRACE."""
    def q(c):
        c.execute("DELETE FROM assets WHERE owner = ? AND created < ? AND NOT EXISTS "
                  "(SELECT 1 FROM conversations v WHERE v.owner = assets.owner "
                  "AND v.id = assets.conversation)", (owner, time.time() - ORPHAN_GRACE))
        return c.execute("SELECT doc FROM conversations WHERE owner = ? "
                         "ORDER BY updated DESC", (owner,)).fetchall()
    rows = _run(q)
    out = []
    for doc, in rows:
        try:
            out.append(json.loads(doc))
        except ValueError:
            continue
    return out


# Le résumé d'une conversation pour le menu : sans ses messages.
_SUMMARY = ("SELECT id, title, created, updated, "
            "coalesce(json_array_length(doc, '$.messages'), 0) FROM conversations ")


def _summary(row) -> dict:
    cid, title, created, updated, count = row
    # Les dates de la page : des millisecondes.
    return {"id": cid, "title": title, "created": round(created * 1000),
            "updated": round(updated * 1000), "count": count}


def summaries(owner: str, limit: int, offset: int = 0) -> list[dict]:
    """Une page de la liste du menu, la plus récente d'abord."""
    rows = _run(lambda c: c.execute(
        _SUMMARY + "WHERE owner = ? ORDER BY updated DESC, id LIMIT ? OFFSET ?",
        (owner, limit, offset)).fetchall())
    return [_summary(r) for r in rows]


def get(owner: str, cid: str) -> dict | None:
    """Le document complet d'une conversation, None si inconnue."""
    _check_id(cid)
    row = _run(lambda c: c.execute(
        "SELECT doc FROM conversations WHERE owner = ? AND id = ?",
        (owner, cid)).fetchone())
    try:
        return json.loads(row[0]) if row else None
    except ValueError:
        return None


def fold(text) -> str:
    """Sans casse ni accents : la recherche du menu (chat/store.js)."""
    return "".join(ch for ch in unicodedata.normalize("NFD", str(text or ""))
                   if not unicodedata.combining(ch)).lower()


def search(owner: str, query: str, limit: int) -> list[dict]:
    """Les résumés des conversations dont le titre ou le texte d'un message
    contient chaque mot de `query`, la plus récente d'abord."""
    words = fold(query).split()
    if not words:
        return []
    rows = _run(lambda c: c.execute(
        "SELECT id, title, created, updated, doc FROM conversations "
        "WHERE owner = ? ORDER BY updated DESC, id", (owner,)).fetchall())
    out = []
    for cid, title, created, updated, doc in rows:
        try:
            messages = json.loads(doc).get("messages") or []
        except (ValueError, AttributeError):
            continue
        text = fold(title + "\n" + "\n".join(
            m["content"] for m in messages
            if isinstance(m, dict) and isinstance(m.get("content"), str)))
        if all(w in text for w in words):
            out.append(_summary((cid, title, created, updated, len(messages))))
            if len(out) >= limit:
                break
    return out


def usage(owner: str) -> dict:
    def q(c):
        n, = c.execute("SELECT count(*) FROM conversations WHERE owner = ?",
                       (owner,)).fetchone()
        return {"conversations": n, "bytes": _used(c, owner),
                "max_bytes": MAX_TOTAL_BYTES}
    return _run(q)


def save(owner: str, cid: str, doc) -> None:
    """Crée ou remplace la conversation `cid` ; efface ses images que le
    document ne cite plus (passé ORPHAN_GRACE)."""
    _check_id(cid)
    if not isinstance(doc, dict) or doc.get("id") != cid:
        raise HistoryError("document attendu, d'`id` égal à celui de l'adresse")
    if doc.get("incognito"):
        raise HistoryError("une conversation incognito n'est pas gardée")
    text = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
    size = len(text.encode("utf-8"))
    if size > MAX_DOC_BYTES:
        raise HistoryError(f"conversation trop grande ({size} octets, "
                           f"{MAX_DOC_BYTES} au plus)", 413)
    title = str(doc.get("title") or "")[:200]
    now = time.time()
    created = _stamp(doc.get("created"), now)
    updated = _stamp(doc.get("updated"), now)
    cited = set(_CITED.findall(text))

    def q(c):
        old = c.execute("SELECT length(doc) FROM conversations WHERE owner = ? "
                        "AND id = ?", (owner, cid)).fetchone()
        if _used(c, owner) - (old[0] if old else 0) + size > MAX_TOTAL_BYTES:
            raise HistoryError("historique plein : supprimez d'anciennes "
                               "conversations", 413)
        c.execute("INSERT INTO conversations (owner, id, title, created, updated, doc) "
                  "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (owner, id) DO UPDATE SET "
                  "title = excluded.title, updated = excluded.updated, doc = excluded.doc",
                  (owner, cid, title, created, updated, text))
        stale = [aid for aid, in c.execute(
            "SELECT id FROM assets WHERE owner = ? AND conversation = ? AND created < ?",
            (owner, cid, now - ORPHAN_GRACE)) if aid not in cited]
        c.executemany("DELETE FROM assets WHERE owner = ? AND id = ?",
                      [(owner, aid) for aid in stale])
    _run(q)


def _stamp(value, default: float) -> float:
    """Une date de la page (millisecondes) → secondes."""
    try:
        return float(value) / 1000.0
    except (TypeError, ValueError):
        return default


def delete(owner: str, cid: str) -> int:
    """Supprime la conversation et ses images ; 0 si elle n'existait pas."""
    _check_id(cid)

    def q(c):
        n = c.execute("DELETE FROM conversations WHERE owner = ? AND id = ?",
                      (owner, cid)).rowcount
        c.execute("DELETE FROM assets WHERE owner = ? AND conversation = ?",
                  (owner, cid))
        return n
    return _run(q)


def add_asset(owner: str, cid: str, name: str, data: bytes) -> dict:
    """Garde une image de la conversation `cid` (qui peut ne pas encore
    exister : le document suit) → {id, url, type, size}."""
    _check_id(cid)
    kind = files.sniff(data)
    if kind is None:
        raise HistoryError("seules les images PNG, JPEG, GIF et WebP sont gardées")
    if len(data) > MAX_ASSET_BYTES:
        raise HistoryError(f"image trop grande ({len(data)} octets, "
                           f"{MAX_ASSET_BYTES} au plus)", 413)
    aid = secrets.token_urlsafe(18)

    def q(c):
        if _used(c, owner) + len(data) > MAX_TOTAL_BYTES:
            raise HistoryError("historique plein : supprimez d'anciennes "
                               "conversations", 413)
        c.execute("INSERT INTO assets (owner, id, conversation, type, name, data, created) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (owner, aid, cid, kind, files.safe_name(name or "image"), data, time.time()))
    _run(q)
    return {"id": aid, "url": ASSET_PATH + aid, "type": kind, "size": len(data)}


def asset(owner: str, aid: str) -> tuple[str, str, bytes] | None:
    """(type, nom, octets) d'une image du compte ; None sinon."""
    if not isinstance(aid, str) or not _ID.match(aid):
        return None
    return _run(lambda c: c.execute(
        "SELECT type, name, data FROM assets WHERE owner = ? AND id = ?",
        (owner, aid)).fetchone())
