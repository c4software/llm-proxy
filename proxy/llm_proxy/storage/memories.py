"""
La MÉMOIRE persistante des clients : des fichiers texte sous `/memories`,
rangés par client, que le modèle lit et écrit par l'outil `memory`
(tools/memory.py) et que l'utilisateur voit, corrige ou efface dans le
tableau de bord (/ui, section Mémoire).

CLOISONNÉE PAR CLÉ : chaque fichier appartient au client dont la clé du
proxy l'a écrit, et ne se lit qu'avec elle. Un client = tools.owner(clé),
le condensé que reçoit tout outil dans `call.client` : stable d'un
démarrage à l'autre (son secret est gardé dans data/client.secret), et qui
ne dit rien de la clé. La clé elle-même n'est jamais écrite. Proxy ouvert
(pas de clé) : un seul client, «».
Changer une clé, c'est donc changer de mémoire : l'ancienne reste en base,
inaccessible, jusqu'à ce qu'on la supprime.
Avec les comptes OIDC (accounts.py), le client d'une clé gérée est son
COMPTE (auth.account_owner) : toutes ses clés, et la page connectée,
partagent une mémoire.

Un fichier = (client, chemin, contenu, dates, échéance). L'échéance
(`expires`, horodatage) est facultative : sans elle, le fichier est
PERSISTANT ; avec, il disparaît une fois passée — invisible aussitôt,
effacé à l'opération suivante. C'est le modèle qui la choisit (l'outil
`memory`, `expires_in_days`), ou l'utilisateur dans le tableau de bord.

Les répertoires n'existent
pas en tant que tels : ce sont les préfixes des chemins, comme dans un
magasin d'objets. Bornes par client (nombre de fichiers, taille d'un
fichier, taille totale), pour qu'un modèle qui boucle ne remplisse pas le
disque.

SQLite, une connexion par opération sous un verrou : des opérations
courtes, appelées hors de la boucle d'événements (asyncio.to_thread).
"""

import posixpath
import sqlite3
import threading
import time

from ..core import config

DB_PATH = config.resolve(config.text("tools.memory.database", "memory.db"))
MAX_FILES = config.integer("tools.memory.max_files", 500)
MAX_FILE_CHARS = config.integer("tools.memory.max_file_chars", 100_000)
MAX_TOTAL_CHARS = config.integer("tools.memory.max_total_chars", 2_000_000)
ROOT = "/memories"
MAX_PATH = 256


class MemoryError_(Exception):
    """Une opération refusée : le message est pour le modèle (ou l'UI),
    en anglais ; `code` : celui du contrat des outils (tools.ERRORS) —
    `invalid_input` (chemin, argument), `not_accessible` (rien à ce
    chemin), `failed` (bornes de la mémoire atteintes)."""

    def __init__(self, message: str, code: str = "invalid_input"):
        super().__init__(message)
        self.code = code


_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    # Résolu à l'appel : un test qui remplace DB_PATH ouvre SA base.
    conn = sqlite3.connect(DB_PATH, timeout=10.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("""CREATE TABLE IF NOT EXISTS files (
        owner TEXT NOT NULL, path TEXT NOT NULL, content TEXT NOT NULL,
        created REAL NOT NULL, updated REAL NOT NULL, expires REAL,
        PRIMARY KEY (owner, path))""")
    # Une base d'avant l'échéance : la colonne, vide (tout persistant).
    if "expires" not in {r[1] for r in conn.execute("PRAGMA table_info(files)")}:
        conn.execute("ALTER TABLE files ADD COLUMN expires REAL")
    return conn


def _run(fn):
    """fn(conn) sous le verrou, dans une transaction, après avoir effacé
    les fichiers échus (de tous les clients) : ce qui suit ne les voit
    plus."""
    with _lock:
        conn = _connect()
        try:
            with conn:
                conn.execute("DELETE FROM files WHERE expires IS NOT NULL "
                             "AND expires <= ?", (time.time(),))
                return fn(conn)
        finally:
            conn.close()


def normalize(path, directory: bool = False) -> str:
    """Le chemin écrit par le modèle ou l'UI → sa forme rangée, sous
    /memories. Refuse ce qui en sortirait (`..`), les caractères de
    contrôle et les chemins trop longs. `directory` : /memories lui-même
    est permis (un répertoire)."""
    if not isinstance(path, str) or not path.strip():
        raise MemoryError_("a path is required")
    raw = path.strip()
    if any(ord(c) < 32 or ord(c) == 127 for c in raw) or "\\" in raw:
        raise MemoryError_(f"invalid path {raw!r}")
    if not raw.startswith("/"):
        raw = ROOT + "/" + raw
    if any(part == ".." for part in raw.split("/")):
        raise MemoryError_(f"the path {raw} leaves {ROOT}")
    norm = posixpath.normpath(raw)
    if norm != ROOT and not norm.startswith(ROOT + "/"):
        raise MemoryError_(f"paths must be under {ROOT} (got {raw})")
    if norm == ROOT and not directory:
        raise MemoryError_(f"{ROOT} is a directory, not a file")
    if len(norm) > MAX_PATH:
        raise MemoryError_(f"the path is longer than {MAX_PATH} characters")
    return norm


def _under(directory: str) -> tuple[str, str]:
    """Bornes LIKE-free d'un préfixe de répertoire : [dir/, dir0)."""
    prefix = directory.rstrip("/") + "/"
    return prefix, prefix[:-1] + "0"      # «0» suit «/» dans l'ordre ASCII


def list_files(owner: str, directory: str = ROOT) -> list[dict]:
    """Les fichiers sous `directory`, triés par chemin."""
    directory = normalize(directory, directory=True)
    low, high = _under(directory)
    rows = _run(lambda c: c.execute(
        "SELECT path, length(content), created, updated, expires FROM files "
        "WHERE owner = ? AND path >= ? AND path < ? ORDER BY path",
        (owner, low, high)).fetchall())
    return [{"path": p, "chars": n, "created": c, "updated": u, "expires": e}
            for p, n, c, u, e in rows]


def is_dir(owner: str, path: str) -> bool:
    if path == ROOT:
        return True
    low, high = _under(path)
    return bool(_run(lambda c: c.execute(
        "SELECT 1 FROM files WHERE owner = ? AND path >= ? AND path < ? LIMIT 1",
        (owner, low, high)).fetchone()))


def read(owner: str, path: str) -> str | None:
    entry = stat(owner, path)
    return entry["content"] if entry else None


def stat(owner: str, path: str) -> dict | None:
    """Le fichier et son échéance : {content, created, updated, expires},
    ou None."""
    path = normalize(path)
    row = _run(lambda c: c.execute(
        "SELECT content, created, updated, expires FROM files "
        "WHERE owner = ? AND path = ?", (owner, path)).fetchone())
    if row is None:
        return None
    return dict(zip(("content", "created", "updated", "expires"), row))


# `write(..., expires=KEEP)` : l'échéance du fichier ne change pas (un
# nouveau fichier est persistant).
KEEP = object()


def write(owner: str, path: str, content: str, expires=KEEP) -> bool:
    """Crée ou remplace le fichier. → True s'il existait déjà. `expires` :
    l'horodatage où il disparaît, None = persistant, KEEP = inchangée.
    Refusé : contenu trop long, échéance passée, chemin qui est un
    répertoire, ou bornes du client dépassées."""
    path = normalize(path)
    if not isinstance(content, str):
        raise MemoryError_("the content must be text")
    if expires is not KEEP and expires is not None:
        if (not isinstance(expires, (int, float)) or isinstance(expires, bool)
                or expires != expires):
            raise MemoryError_("the expiry must be a timestamp, or none")
        if expires <= time.time():
            raise MemoryError_("the expiry is already past")
    if len(content) > MAX_FILE_CHARS:
        raise MemoryError_(f"a memory file holds at most {MAX_FILE_CHARS} "
                           f"characters (got {len(content)})", "failed")

    def go(c):
        low, high = _under(path)
        if c.execute("SELECT 1 FROM files WHERE owner = ? AND path >= ? "
                     "AND path < ? LIMIT 1", (owner, low, high)).fetchone():
            raise MemoryError_(f"{path} is a directory")
        # Un ancêtre qui serait un fichier : /memories/a existe, on écrit
        # /memories/a/b.
        parent = posixpath.dirname(path)
        while parent != ROOT:
            if c.execute("SELECT 1 FROM files WHERE owner = ? AND path = ?",
                         (owner, parent)).fetchone():
                raise MemoryError_(f"{parent} is a file, not a directory")
            parent = posixpath.dirname(parent)
        old = c.execute("SELECT length(content) FROM files WHERE owner = ? "
                        "AND path = ?", (owner, path)).fetchone()
        count, total = c.execute("SELECT count(*), coalesce(sum(length(content)), 0) "
                                 "FROM files WHERE owner = ?", (owner,)).fetchone()
        if old is None and count >= MAX_FILES:
            raise MemoryError_(f"the memory holds at most {MAX_FILES} files; "
                               f"delete some first", "failed")
        if total - (old[0] if old else 0) + len(content) > MAX_TOTAL_CHARS:
            raise MemoryError_(f"the memory holds at most {MAX_TOTAL_CHARS} "
                               f"characters in all; delete or shorten files first",
                               "failed")
        now = time.time()
        if old is None:
            c.execute("INSERT INTO files VALUES (?, ?, ?, ?, ?, ?)",
                      (owner, path, content, now, now,
                       None if expires is KEEP else expires))
        elif expires is KEEP:
            c.execute("UPDATE files SET content = ?, updated = ? WHERE owner = ? "
                      "AND path = ?", (content, now, owner, path))
        else:
            c.execute("UPDATE files SET content = ?, updated = ?, expires = ? "
                      "WHERE owner = ? AND path = ?",
                      (content, now, expires, owner, path))
        return old is not None
    return _run(go)


def delete(owner: str, path: str) -> int:
    """Supprime le fichier, ou tout ce que contient le répertoire. → le
    nombre de fichiers supprimés."""
    path = normalize(path, directory=True)
    low, high = _under(path)
    return _run(lambda c: c.execute(
        "DELETE FROM files WHERE owner = ? AND (path = ? OR (path >= ? AND path < ?))",
        (owner, path, low, high)).rowcount)


def rename(owner: str, old: str, new: str) -> int:
    """Renomme un fichier, ou déplace un répertoire entier. → le nombre de
    fichiers déplacés. Refusé si la destination existe."""
    old = normalize(old)
    new = normalize(new)
    if new == old or new.startswith(old + "/"):
        raise MemoryError_(f"cannot move {old} into itself")

    def go(c):
        lo, hi = _under(old)
        moved = c.execute("SELECT path FROM files WHERE owner = ? AND (path = ? "
                          "OR (path >= ? AND path < ?))", (owner, old, lo, hi)).fetchall()
        if not moved:
            raise MemoryError_(f"{old} does not exist", "not_accessible")
        nlo, nhi = _under(new)
        if c.execute("SELECT 1 FROM files WHERE owner = ? AND (path = ? OR "
                     "(path >= ? AND path < ?)) LIMIT 1", (owner, new, nlo, nhi)).fetchone():
            raise MemoryError_(f"{new} already exists")
        now = time.time()
        for (p,) in moved:
            target = new + p[len(old):]
            if len(target) > MAX_PATH:
                raise MemoryError_(f"the path {target} would be longer than "
                                   f"{MAX_PATH} characters")
            c.execute("UPDATE files SET path = ?, updated = ? WHERE owner = ? "
                      "AND path = ?", (target, now, owner, p))
        return len(moved)
    return _run(go)


def usage(owner: str) -> dict:
    count, total = _run(lambda c: c.execute(
        "SELECT count(*), coalesce(sum(length(content)), 0) FROM files "
        "WHERE owner = ?", (owner,)).fetchone())
    return {"files": count, "chars": total, "max_files": MAX_FILES,
            "max_chars": MAX_TOTAL_CHARS, "max_file_chars": MAX_FILE_CHARS}
