"""
Les BACKENDS DES COMPTES : ceux qu'un compte déclare lui-même
(Configuration → Backends), avec [user_backends].enabled. Un nom (le
préfixe de routage, comme celui d'une table [backends.<nom>]), un
protocole (`openai` ou `anthropic`), une adresse, une clé, et deux
réglages d'une table [backends.<nom>] : `images` (vide : selon le
protocole) et `tokenize_path`. Désactivé (`enabled` faux), il garde sa clé
et son nom mais n'est plus routé, ni montré aux autres comptes.

  à soi          (`shared` faux) : seul son compte le voit et s'en sert ;
  partagé        (`shared` vrai) : offert à tous les comptes, AVEC la clé
                 de son créateur — les autres la consomment sans la voir.
                 Un backend à soi du même nom le masque pour son compte.

La clé n'est jamais rendue, ni à la page ni à un autre compte. Le nom d'un
backend du TOML est pris (l'appelant le donne : `reserved`) ; deux backends
partagés ne portent pas le même nom. Rangé dans conversations.db, sauvegardé
avec lui ; règles communes aux magasins par compte : owned.py. Les backends
eux-mêmes (client HTTP, routage) : upstream/backends.py.
"""

import re
import secrets
import time
from urllib.parse import urlsplit

from ..core import config
from ..core.httputil import origin
from . import owned

ENABLED = config.flag("user_backends.enabled", True)
MAX_PER_USER = config.integer("user_backends.max_per_user", 10)
APIS = ("openai", "anthropic")
MAX_KEY = 4000
# Un préfixe de routage : « monllama/qwen3 » vise le backend « monllama ».
NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,39}\Z")
# Un chemin sous l'adresse du backend (llama.cpp : « /tokenize »).
TOKENIZE_PATH = re.compile(r"/[A-Za-z0-9._~/-]{0,199}\Z")
_FIELDS = "id, name, api, url, shared, created, owner, images, tokenize_path, disabled"

BackendError = owned.OwnedError


def _table(c) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS user_backends (
        owner TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL,
        api TEXT NOT NULL, url TEXT NOT NULL, api_key TEXT NOT NULL,
        shared INTEGER NOT NULL, created REAL NOT NULL,
        PRIMARY KEY (owner, id))""")
    owned.add_columns(c, "user_backends", {"images": "INTEGER",
                                           "tokenize_path": "TEXT NOT NULL DEFAULT ''",
                                           "disabled": "INTEGER NOT NULL DEFAULT 0"})


_run = owned.runner(_table)


def _meta(row) -> dict:
    """Une ligne pour la page, sans clé ; `owner` (le client du créateur)
    sert à le nommer, la route le retire."""
    bid, name, api, url, shared, created, owner, images, tokenize_path, disabled = row
    return {"id": bid, "name": name, "api": api, "url": url, "shared": bool(shared),
            "enabled": not disabled,
            "images": None if images is None else bool(images),
            "tokenize_path": tokenize_path, "created": owned.seconds(created),
            "owner": owner}


def _fields(body, reserved) -> dict:
    """Les champs d'un backend, vérifiés ; `api_key` «» = non donnée."""
    owned.body(body)
    name = str(body.get("name") or "").strip().lower()
    if not NAME.match(name):
        raise BackendError("nom : 1 à 40 caractères parmi a-z, 0-9, « . _ - », "
                           "une lettre ou un chiffre en tête (c'est le préfixe des modèles)")
    if name in reserved:
        raise BackendError(f"« {name} » est le nom d'un backend du proxy")
    api = body.get("api") or "openai"
    if api not in APIS:
        raise BackendError("`api` : " + ", ".join(APIS))
    url = owned.http_url(str(body.get("url") or "").strip().rstrip("/"),
                         "l'adresse du backend, une URL")
    if "@" in urlsplit(url).netloc:
        # L'adresse est montrée (à tous pour un backend partagé) : la clé a
        # son propre champ, jamais l'URL.
        raise BackendError("pas d'identifiants dans l'adresse : la clé a son propre champ")
    key = str(body.get("api_key") or "").strip()
    if len(key) > MAX_KEY:
        raise BackendError("clé trop longue")
    images = body.get("images")
    if images is not None and not isinstance(images, bool):
        raise BackendError("`images` : true, false ou absent (selon le protocole)")
    tokenize = str(body.get("tokenize_path") or "").strip()
    if tokenize and (not TOKENIZE_PATH.match(tokenize) or ".." in tokenize
                     or "//" in tokenize):
        raise BackendError("`tokenize_path` : un chemin sous l'adresse, « /tokenize »")
    # Absent : actif à la création, inchangé à la modification.
    enabled = body.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise BackendError("`enabled` : true ou false")
    return {"name": name, "api": api, "url": url, "api_key": key,
            "shared": 1 if body.get("shared") else 0, "disabled": None if enabled is None else int(not enabled),
            "images": None if images is None else int(images), "tokenize_path": tokenize}


def _clash(c, f: dict, owner: str, but: str = "") -> None:
    if owned.taken(c, "user_backends", owner, f["name"], but):
        raise BackendError("un de vos backends porte déjà ce nom")
    if f["shared"] and c.execute(
            "SELECT 1 FROM user_backends WHERE shared = 1 AND name = ? "
            "AND NOT (owner = ? AND id = ?)", (f["name"], owner, but)).fetchone():
        raise BackendError("un backend partagé porte déjà ce nom")


def _one(c, owner: str, bid: str):
    return c.execute(f"SELECT {_FIELDS} FROM user_backends WHERE owner = ? AND id = ?",
                     (owner, bid)).fetchone()


def list_own(owner: str) -> list[dict]:
    """Les backends du compte, sans clé : pour la page."""
    rows = _run(lambda c: c.execute(
        f"SELECT {_FIELDS} FROM user_backends WHERE owner = ? ORDER BY created, id",
        (owner,)).fetchall())
    return [_meta(r) for r in rows]


def list_shared(owner: str) -> list[dict]:
    """Les backends partagés par les AUTRES comptes, actifs, sans clé."""
    rows = _run(lambda c: c.execute(
        f"SELECT {_FIELDS} FROM user_backends WHERE shared = 1 AND disabled = 0 "
        "AND owner != ? "
        "ORDER BY name", (owner,)).fetchall())
    return [_meta(r) for r in rows]


def list_all() -> list[dict]:
    """Les backends de tous les comptes, sans clé : pour l'admin."""
    rows = _run(lambda c: c.execute(
        f"SELECT {_FIELDS} FROM user_backends ORDER BY name, created").fetchall())
    return [_meta(r) for r in rows]


def create(owner: str, body, reserved=frozenset()) -> dict:
    f = _fields(body, reserved)
    bid = secrets.token_urlsafe(12)

    def q(c):
        owned.room(c, "user_backends", owner, MAX_PER_USER, "backends")
        _clash(c, f, owner)
        c.execute("INSERT INTO user_backends (owner, id, name, api, url, api_key, shared, "
                  "created, images, tokenize_path, disabled) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (owner, bid, f["name"], f["api"], f["url"], f["api_key"], f["shared"],
                   time.time(), f["images"], f["tokenize_path"], f["disabled"] or 0))
        return _one(c, owner, bid)
    return _meta(_run(q))


def update(owner: str, bid: str, body, reserved=frozenset()) -> dict:
    """La clé absente reste (la page ne la connaît pas) — sauf si l'adresse
    change d'origine : elle n'irait pas, sans être ressaisie, vers un autre
    hôte. `enabled` absent : l'état reste."""
    owned.check_id(bid, "backend")
    f = _fields(body, reserved)

    def q(c):
        old = c.execute("SELECT url, api_key, disabled FROM user_backends "
                        "WHERE owner = ? AND id = ?", (owner, bid)).fetchone()
        if old is None:
            raise BackendError("backend inconnu", 404)
        key = f["api_key"] or ("" if origin(old[0]) != origin(f["url"]) else old[1])
        _clash(c, f, owner, bid)
        c.execute("UPDATE user_backends SET name = ?, api = ?, url = ?, api_key = ?, "
                  "shared = ?, images = ?, tokenize_path = ?, disabled = ? "
                  "WHERE owner = ? AND id = ?",
                  (f["name"], f["api"], f["url"], key, f["shared"], f["images"],
                   f["tokenize_path"], old[2] if f["disabled"] is None else f["disabled"],
                   owner, bid))
        return _one(c, owner, bid)
    return _meta(_run(q))


def delete(owner: str | None, bid: str) -> int:
    """Le backend `bid` du compte `owner`, ou de n'importe qui (None : admin)."""
    owned.check_id(bid, "backend")
    if owner is None:
        return _run(lambda c: c.execute("DELETE FROM user_backends WHERE id = ?",
                                        (bid,)).rowcount)
    return _run(lambda c: c.execute("DELETE FROM user_backends WHERE owner = ? AND id = ?",
                                    (owner, bid)).rowcount)


def rows() -> list[dict]:
    """Les backends actifs des comptes, clés COMPRISES : pour le routage
    (upstream/backends.py) seulement. BLOQUANT."""
    got = _run(lambda c: c.execute(
        "SELECT owner, id, name, api, url, api_key, shared, images, tokenize_path "
        "FROM user_backends WHERE disabled = 0 ORDER BY created, id").fetchall())
    return [{"owner": o, "id": i, "name": n, "api": a, "url": u, "api_key": k,
             "shared": bool(s), "images": None if im is None else bool(im),
             "tokenize_path": t} for o, i, n, a, u, k, s, im, t in got]
