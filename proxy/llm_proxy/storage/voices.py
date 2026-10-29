"""
Les VOIX CLONÉES du chat de la page, gardées en base et liées au COMPTE
connecté (auth.ui_client), comme l'historique : le même compte retrouve
ses voix d'un navigateur ou d'un appareil à l'autre. Chacune : sa
référence (un WAV normalisé par la page, quelques centaines de Ko) et sa
transcription mot pour mot. La référence repart de la page avec chaque
requête de synthèse (docs/voix.md) : le proxy ne fait que la garder.

Même base que l'historique (conversations.db : sauvegardée avec lui,
backup.py), sa table à part. L'identifiant vient de la page (un UUID) :
une voix envoyée d'un ancien navigateur garde le sien, et les réglages
ou conversations qui la citent la retrouvent. Bornes par compte (nombre,
taille d'une référence) pour qu'une page qui boucle ne remplisse pas le
disque.
"""

import time

from ..core import config
from . import conversations

MAX_VOICES = config.integer("ui.voices_max", 100)
MAX_WAV_BYTES = config.integer("ui.voice_max_bytes", 10_000_000)
MAX_TEXT = 5000
MAX_NAME = 120
_FIELDS = "id, name, text, duration, source, created, length(wav)"


def _table(c) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS voices (
        owner TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL,
        text TEXT NOT NULL, duration REAL NOT NULL, source TEXT NOT NULL,
        created REAL NOT NULL, wav BLOB NOT NULL, PRIMARY KEY (owner, id))""")


def _run(fn):
    def q(c):
        _table(c)
        return fn(c)
    return conversations._run(q)


def _check_id(vid) -> str:
    if not isinstance(vid, str) or not conversations._ID.match(vid):
        raise conversations.HistoryError("identifiant de voix invalide")
    return vid


def _meta(row) -> dict:
    vid, name, text, duration, source, created, size = row
    # Les dates de la page : des millisecondes.
    return {"id": vid, "name": name, "text": text, "duration": duration,
            "source": source, "created": round(created * 1000), "size": size}


def list_all(owner: str) -> list[dict]:
    """Les voix du compte, sans leur référence, la plus ancienne d'abord."""
    rows = _run(lambda c: c.execute(
        f"SELECT {_FIELDS} FROM voices WHERE owner = ? ORDER BY created, id",
        (owner,)).fetchall())
    return [_meta(r) for r in rows]


def save(owner: str, vid: str, meta, wav: bytes) -> dict:
    """Crée ou remplace la voix `vid` (son WAV et sa transcription)."""
    _check_id(vid)
    if not isinstance(meta, dict):
        raise conversations.HistoryError("corps JSON attendu")
    name = str(meta.get("name") or "").strip()[:MAX_NAME] or "Voix"
    text = str(meta.get("text") or "").strip()
    if not text:
        raise conversations.HistoryError("transcription manquante")
    if len(text) > MAX_TEXT:
        raise conversations.HistoryError(f"transcription trop longue ({MAX_TEXT} caractères au plus)")
    if conversations.files.sniff_audio(wav) != "audio/wav":
        raise conversations.HistoryError("la référence doit être un WAV")
    if len(wav) > MAX_WAV_BYTES:
        raise conversations.HistoryError(f"référence trop grande ({len(wav)} octets, "
                                         f"{MAX_WAV_BYTES} au plus)", 413)
    try:
        duration = max(0.0, float(meta.get("duration") or 0))
    except (TypeError, ValueError):
        duration = 0.0
    source = str(meta.get("source") or "")[:40]
    created = conversations._stamp(meta.get("created"), time.time())

    def q(c):
        known = c.execute("SELECT 1 FROM voices WHERE owner = ? AND id = ?",
                          (owner, vid)).fetchone()
        n, = c.execute("SELECT count(*) FROM voices WHERE owner = ?", (owner,)).fetchone()
        if not known and n >= MAX_VOICES:
            raise conversations.HistoryError(f"{MAX_VOICES} voix au plus : "
                                             "supprimez-en une", 413)
        c.execute("INSERT INTO voices (owner, id, name, text, duration, source, created, wav) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (owner, id) DO UPDATE SET "
                  "name = excluded.name, text = excluded.text, duration = excluded.duration, "
                  "source = excluded.source, wav = excluded.wav",
                  (owner, vid, name, text, duration, source, created, wav))
        return c.execute(f"SELECT {_FIELDS} FROM voices WHERE owner = ? AND id = ?",
                         (owner, vid)).fetchone()
    return _meta(_run(q))


def rename(owner: str, vid: str, name) -> dict | None:
    _check_id(vid)
    name = str(name or "").strip()[:MAX_NAME]
    if not name:
        raise conversations.HistoryError("nom manquant")

    def q(c):
        c.execute("UPDATE voices SET name = ? WHERE owner = ? AND id = ?", (name, owner, vid))
        return c.execute(f"SELECT {_FIELDS} FROM voices WHERE owner = ? AND id = ?",
                         (owner, vid)).fetchone()
    row = _run(q)
    return _meta(row) if row else None


def wav(owner: str, vid: str) -> bytes | None:
    """La référence d'une voix du compte ; None sinon."""
    if not isinstance(vid, str) or not conversations._ID.match(vid):
        return None
    row = _run(lambda c: c.execute("SELECT wav FROM voices WHERE owner = ? AND id = ?",
                                   (owner, vid)).fetchone())
    return row[0] if row else None


def delete(owner: str, vid: str) -> int:
    _check_id(vid)

    def q(c):
        n = c.execute("DELETE FROM voices WHERE owner = ? AND id = ?", (owner, vid)).rowcount
        conversations._reclaim(c)
        return n
    return _run(q)
