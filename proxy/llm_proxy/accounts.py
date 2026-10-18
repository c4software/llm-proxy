"""
Les COMPTES et leurs CLÉS : ce qui existe quand la page se connecte par
OIDC ([oidc], voir oidc.py). Sans [oidc], ce module ne sert pas : les clés
du proxy sont celles du TOML (proxy.api_keys).

UN COMPTE = une identité OIDC (issuer, sub), avec un rôle :
  * admin   — tout ce qu'a un utilisateur, plus les comptes et les clés de
              tous, et toutes les statistiques ;
  * user    — ses clés, ses statistiques, le chat et sa mémoire ;
  * pending — connecté, mais rien d'ouvert tant qu'un admin ne l'a pas
              accepté ([oidc].new_accounts dit si un nouveau compte
              commence là ou directement en user) ;
  * blocked — refusé : ni page, ni clés (ses clés cessent de valoir).
Le PREMIER compte créé est admin : c'est ainsi qu'un déploiement neuf a
son administrateur sans rien écrire ailleurs. Le dernier admin ne peut
pas perdre son rôle.

UNE CLÉ = un uuid4 tiré par le proxy, montré UNE fois à sa création. N'en
est gardé que le condensé (tools.owner, calculé par l'appelant : ce module
ne connaît pas le secret) et un indice (ses 8 premiers caractères) pour
la reconnaître dans la liste. Une clé révoquée reste en base (ses
statistiques gardent un libellé) mais ne vaut plus rien.

LES CLÉS DU TOML (proxy.api_keys) n'ont pas de compte, sauf si un admin
en RATTACHE une à un compte (table `bound`, par condensé) : elle écrit
alors dans la mémoire du compte, ses statistiques rejoignent les siennes,
et elle suit son état (compte en attente ou bloqué = clé refusée). Elle
reste écrite dans le TOML. Les données déjà rangées sous la clé seule
ne sont pas déplacées.

Ce module ne connaît ni FastAPI, ni httpx2, ni Authlib. SQLite, une
connexion par opération sous un verrou, comme memories.py : des
opérations courtes, appelées hors de la boucle d'événements. Les lectures
du chemin chaud (une clé présentée, le compte d'une session) passent par
un petit cache à durée courte, vidé à chaque écriture locale.
"""

import sqlite3
import threading
import time

from . import config

DB_PATH = config.resolve(config.text("oidc.database", "accounts.db"))
ROLES = ("admin", "user", "pending", "blocked")
ACTIVE = frozenset(("admin", "user"))
NEW_ROLE = config.text("oidc.new_accounts", "pending")
if NEW_ROLE not in ("pending", "user"):
    raise SystemExit(f"{config.CONFIG_PATH} : oidc.new_accounts doit être "
                     f"«pending» ou «user» (reçu {NEW_ROLE!r})")
MAX_LABEL = 80
MAX_KEYS = config.integer("oidc.max_keys_per_account", 50)
# Une clé présentée, le compte d'une session : relus au plus toutes les
# CACHE_TTL secondes. Assez court pour qu'une révocation faite par un
# autre processus prenne effet vite ; assez long pour ne pas toucher
# SQLite à chaque requête.
CACHE_TTL = 10.0
# Dernière utilisation d'une clé : écrite au plus une fois par minute.
TOUCH_EVERY = 60.0


class AccountError(Exception):
    """Une opération refusée ; le message est pour la page (en français).
    `status` : celui de la réponse HTTP."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


_lock = threading.Lock()
_cache: dict[tuple, tuple[float, object]] = {}
_touched: dict[str, float] = {}


def _connect() -> sqlite3.Connection:
    # Résolu à l'appel : un test qui remplace DB_PATH ouvre SA base.
    conn = sqlite3.connect(DB_PATH, timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        issuer TEXT NOT NULL, sub TEXT NOT NULL,
        email TEXT NOT NULL DEFAULT '', name TEXT NOT NULL DEFAULT '',
        role TEXT NOT NULL, created REAL NOT NULL, last_login REAL,
        UNIQUE (issuer, sub))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS keys (
        id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id),
        digest TEXT NOT NULL UNIQUE, hint TEXT NOT NULL,
        label TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
        last_used REAL, revoked REAL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS bound (
        digest TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id),
        created REAL NOT NULL)""")
    # Ajouts de colonnes, idempotents : `share`, la part du compte dans le
    # partage équitable (fairshare.py) — NULL = la part par défaut,
    # 0 = illimitée.
    have = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    if "share" not in have:
        conn.execute("ALTER TABLE users ADD COLUMN share INTEGER")
    return conn


def _run(fn, write: bool = False):
    """fn(conn) sous le verrou, dans une transaction. Une écriture vide le
    cache : ce processus voit tout de suite ce qu'il vient de changer."""
    with _lock:
        conn = _connect()
        try:
            with conn:
                result = fn(conn)
        finally:
            conn.close()
        if write:
            _cache.clear()
        return result


def _cached(key: tuple, load):
    hit = _cache.get(key)
    now = time.monotonic()
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    value = load()
    _cache[key] = (now, value)
    return value


def _user(row) -> dict | None:
    if row is None:
        return None
    return {"id": row["id"], "email": row["email"], "name": row["name"],
            "role": row["role"], "created": row["created"],
            "last_login": row["last_login"], "share": row["share"]}


# ── Comptes ─────────────────────────────────────────────────────────────

def login(issuer: str, sub: str, email: str = "", name: str = "") -> dict:
    """Le retour d'une connexion OIDC : le compte de (issuer, sub), créé
    s'il n'existait pas — admin si c'est le premier, NEW_ROLE sinon. Son
    e-mail et son nom suivent ce que dit le fournisseur."""
    if not issuer or not sub:
        raise AccountError("identité OIDC incomplète (iss, sub)")
    now = time.time()

    def go(conn):
        row = conn.execute("SELECT * FROM users WHERE issuer = ? AND sub = ?",
                           (issuer, sub)).fetchone()
        if row is not None:
            conn.execute("UPDATE users SET email = ?, name = ?, last_login = ? "
                         "WHERE id = ?", (email, name, now, row["id"]))
            uid = row["id"]
        else:
            first = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
            uid = conn.execute(
                "INSERT INTO users (issuer, sub, email, name, role, created, "
                "last_login) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (issuer, sub, email, name, "admin" if first else NEW_ROLE,
                 now, now)).lastrowid
        return _user(conn.execute("SELECT * FROM users WHERE id = ?",
                                  (uid,)).fetchone())
    return _run(go, write=True)


def user(uid: int) -> dict | None:
    """Le compte `uid`, ou None — relu au plus toutes les CACHE_TTL s."""
    return _cached(("user", uid), lambda: _run(lambda conn: _user(
        conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone())))


def users() -> list[dict]:
    """Tous les comptes (admin), avec leur nombre de clés actives."""
    def go(conn):
        rows = conn.execute(
            "SELECT u.*, (SELECT COUNT(*) FROM keys k WHERE k.user_id = u.id "
            "AND k.revoked IS NULL) AS n_keys FROM users u ORDER BY u.id"
        ).fetchall()
        return [_user(r) | {"keys": r["n_keys"]} for r in rows]
    return _run(go)


def set_role(uid: int, role: str) -> dict:
    """Le rôle d'un compte (admin). Le dernier admin garde le sien."""
    if role not in ROLES:
        raise AccountError(f"rôle inconnu : {role!r} ({', '.join(ROLES)})")

    def go(conn):
        row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        if row is None:
            raise AccountError("compte introuvable", 404)
        if row["role"] == "admin" and role != "admin":
            admins = conn.execute("SELECT COUNT(*) FROM users "
                                  "WHERE role = 'admin'").fetchone()[0]
            if admins <= 1:
                raise AccountError("le dernier admin ne peut pas perdre son "
                                   "rôle", 409)
        conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, uid))
        return _user(conn.execute("SELECT * FROM users WHERE id = ?",
                                  (uid,)).fetchone())
    return _run(go, write=True)


def set_share(uid: int, share) -> dict:
    """La part d'un compte dans le partage équitable (admin) : None = la
    part par défaut ([fair_share].tokens_per_hour), 0 = illimitée, sinon
    des tokens par heure glissante."""
    if share is not None and (not isinstance(share, int)
                              or isinstance(share, bool) or share < 0):
        raise AccountError("share : null (défaut), 0 (illimitée) ou un "
                           "nombre de tokens par heure")

    def go(conn):
        if conn.execute("UPDATE users SET share = ? WHERE id = ?",
                        (share, uid)).rowcount == 0:
            raise AccountError("compte introuvable", 404)
        return _user(conn.execute("SELECT * FROM users WHERE id = ?",
                                  (uid,)).fetchone())
    return _run(go, write=True)


def owners() -> dict[str, int]:
    """condensé → compte, pour toutes les clés qui en ont un (clés gérées,
    révoquées comprises, et clés du TOML rattachées) : de quoi relire
    la dernière heure des statistiques au démarrage (fairshare.seed)."""
    return _run(lambda conn: {r[0]: r[1] for r in conn.execute(
        "SELECT digest, user_id FROM keys UNION "
        "SELECT digest, user_id FROM bound").fetchall()})


def user_ids() -> list[int]:
    return _run(lambda conn: [r[0] for r in conn.execute(
        "SELECT id FROM users").fetchall()])


# ── Clés ────────────────────────────────────────────────────────────────

def _key(row) -> dict:
    out = {"id": row["id"], "hint": row["hint"], "label": row["label"],
           "created": row["created"], "last_used": row["last_used"],
           "revoked": row["revoked"], "user_id": row["user_id"],
           # Le condensé : ce que les statistiques appellent api_key_id.
           "api_key_id": row["digest"]}
    if "email" in row.keys():
        out["user"] = row["email"] or row["name"] or f"#{row['user_id']}"
    return out


def create_key(uid: int, key: str, digest: str, label: str = "") -> dict:
    """Range la clé `key` (tirée par l'appelant) pour le compte `uid`,
    sous son condensé. Le compte doit être actif."""
    label = " ".join(str(label or "").split())[:MAX_LABEL]
    now = time.time()

    def go(conn):
        row = conn.execute("SELECT role FROM users WHERE id = ?",
                           (uid,)).fetchone()
        if row is None or row["role"] not in ACTIVE:
            raise AccountError("compte inactif : pas de nouvelle clé", 403)
        live = conn.execute("SELECT COUNT(*) FROM keys WHERE user_id = ? "
                            "AND revoked IS NULL", (uid,)).fetchone()[0]
        if live >= MAX_KEYS:
            raise AccountError(f"{MAX_KEYS} clés actives au plus par compte : "
                               "révoquez-en une d'abord", 409)
        kid = digest[:12]
        conn.execute("INSERT INTO keys (id, user_id, digest, hint, label, "
                     "created) VALUES (?, ?, ?, ?, ?, ?)",
                     (kid, uid, digest, key[:8], label, now))
        return _key(conn.execute("SELECT * FROM keys WHERE id = ?",
                                 (kid,)).fetchone())
    return _run(go, write=True)


def keys(uid: int | None = None) -> list[dict]:
    """Les clés d'un compte, ou de tous (None, admin), récentes d'abord,
    révoquées comprises."""
    def go(conn):
        sql = ("SELECT k.*, u.email, u.name FROM keys k "
               "JOIN users u ON u.id = k.user_id")
        args = ()
        if uid is not None:
            sql += " WHERE k.user_id = ?"
            args = (uid,)
        return [_key(r) for r in conn.execute(
            sql + " ORDER BY k.created DESC", args).fetchall()]
    return _run(go)


def digests(uid: int) -> list[str]:
    """Les condensés de toutes les clés du compte, révoquées comprises :
    ses statistiques."""
    return _run(lambda conn: [r[0] for r in conn.execute(
        "SELECT digest FROM keys WHERE user_id = ? UNION "
        "SELECT digest FROM bound WHERE user_id = ?", (uid, uid)).fetchall()])


# ── Clés du TOML rattachées à un compte ─────────────────────────────────

def bind(digest: str, uid: int | None) -> None:
    """Rattache la clé du TOML de condensé `digest` au compte `uid`, ou
    l'en détache (None). L'appelant vérifie que c'est bien une clé du
    TOML."""
    def go(conn):
        if uid is None:
            conn.execute("DELETE FROM bound WHERE digest = ?", (digest,))
            return
        if conn.execute("SELECT 1 FROM users WHERE id = ?",
                        (uid,)).fetchone() is None:
            raise AccountError("compte introuvable", 404)
        conn.execute("INSERT INTO bound (digest, user_id, created) "
                     "VALUES (?, ?, ?) ON CONFLICT (digest) DO UPDATE "
                     "SET user_id = excluded.user_id, created = excluded.created",
                     (digest, uid, time.time()))
    _run(go, write=True)


def bindings() -> dict[str, dict]:
    """condensé → {"user_id", "user"} : les clés du TOML rattachées."""
    return _run(lambda conn: {r["digest"]: {
        "user_id": r["user_id"],
        "user": r["email"] or r["name"] or f"#{r['user_id']}"}
        for r in conn.execute("SELECT b.digest, b.user_id, u.email, u.name "
                              "FROM bound b JOIN users u ON u.id = b.user_id")})


def bound(digest: str) -> dict | None:
    """Le compte d'une clé du TOML rattachée : {"user_id", "active"} ;
    None si elle ne l'est pas — relu au plus toutes les CACHE_TTL s."""
    def load():
        row = _run(lambda conn: conn.execute(
            "SELECT b.user_id, u.role FROM bound b JOIN users u "
            "ON u.id = b.user_id WHERE b.digest = ?", (digest,)).fetchone())
        return {"user_id": row["user_id"], "active": row["role"] in ACTIVE} \
            if row else None
    return _cached(("bound", digest), load)


def revoke_key(kid: str, uid: int | None = None) -> dict:
    """Révoque la clé `kid` — du compte `uid`, ou de n'importe qui (None,
    admin). Révoquer deux fois ne change rien."""
    def go(conn):
        row = conn.execute("SELECT * FROM keys WHERE id = ?", (kid,)).fetchone()
        if row is None or (uid is not None and row["user_id"] != uid):
            raise AccountError("clé introuvable", 404)
        if row["revoked"] is None:
            conn.execute("UPDATE keys SET revoked = ? WHERE id = ?",
                         (time.time(), kid))
        return _key(conn.execute("SELECT * FROM keys WHERE id = ?",
                                 (kid,)).fetchone())
    return _run(go, write=True)


def resolve(digest: str) -> dict | None:
    """La clé de condensé `digest`, si elle vaut encore : non révoquée, et
    son compte actif → {"id", "user_id"} ; None sinon."""
    def load():
        def go(conn):
            row = conn.execute(
                "SELECT k.id, k.user_id FROM keys k JOIN users u "
                "ON u.id = k.user_id WHERE k.digest = ? AND k.revoked IS NULL "
                "AND u.role IN ('admin', 'user')", (digest,)).fetchone()
            return dict(row) if row else None
        return _run(go)
    return _cached(("key", digest), load)


def key_name(digest: str) -> str:
    """Le nom d'une clé gérée (son libellé, sinon ses premiers
    caractères) : ce que l'extension de navigateur montre de qui
    l'appelle. «» pour une clé inconnue."""
    def load():
        def go(conn):
            row = conn.execute("SELECT hint, label FROM keys WHERE digest = ?",
                               (digest,)).fetchone()
            return (row["label"] or row["hint"] + "…") if row else ""
        return _run(go)
    return _cached(("name", digest), load)


def touch_due(kid: str) -> bool:
    """Faut-il noter l'usage de la clé `kid` ? Au plus une fois par
    minute ; vrai = c'est fait pour la minute qui vient (appeler touch)."""
    now = time.monotonic()
    if now - _touched.get(kid, -TOUCH_EVERY) < TOUCH_EVERY:
        return False
    _touched[kid] = now
    return True


def touch(kid: str) -> None:
    """Note l'usage de la clé `kid` (sa dernière utilisation)."""
    _run(lambda conn: conn.execute("UPDATE keys SET last_used = ? WHERE id = ?",
                                   (time.time(), kid)))
