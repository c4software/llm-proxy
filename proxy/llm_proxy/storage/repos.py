"""
Les SOURCES DE DÉPÔTS que lit l'outil `repository` (tools/repository.py) :
une forge, son adresse, un jeton.

  par compte   définies par chacun (Configuration → Dépôts), avec ses
               propres jetons, liées au même client que son historique
               (auth.ui_client). Dans conversations.db, sauvegardée avec
               lui ; règles communes aux magasins par compte : owned.py. Le
               jeton n'est jamais rendu à la page ni au modèle.
  partagées    déclarées par l'admin dans [repos.sources.<nom>] (un jeton
               d'organisation en ${VAR}), offertes à tous les comptes. Une
               source d'un compte du même nom (sans casse) la masque pour lui.

Chaque source peut borner les projets permis (`projects` : des motifs à
jokers, « equipe/* ») ; vide = tous ceux que le jeton voit.
"""

import fnmatch
import json
import re
import secrets
import time
from urllib.parse import urlsplit

from ..core import config
from ..core.httputil import origin
from . import owned

FORGES = ("gitlab", "github", "gitea", "git")
MAX_PER_USER = config.integer("repos.max_per_user", 20)
MAX_TOKEN = 1000
MAX_PATTERNS = 50
_FIELDS = "id, name, forge, url, username, projects, created, last_used, last_error"

# Une opération refusée : celle des magasins par compte (owned.py).
RepoError = owned.OwnedError


def _table(c) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS repo_sources (
        owner TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL,
        forge TEXT NOT NULL, url TEXT NOT NULL, username TEXT NOT NULL,
        token TEXT NOT NULL, projects TEXT NOT NULL, created REAL NOT NULL,
        last_used REAL, last_error TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (owner, id))""")


_run = owned.runner(_table)


def _check_id(sid) -> str:
    return owned.check_id(sid, "source")


def _meta(row, token: str | None = None) -> dict:
    sid, name, forge, url, username, projects, created, last_used, last_error = row
    out = {"id": sid, "name": name, "forge": forge, "url": url, "username": username,
           "projects": json.loads(projects), "created": owned.seconds(created),
           "last_used": owned.seconds(last_used), "last_error": last_error,
           "shared": False}
    if token is not None:
        out["token"] = token
    return out


def _fields(body) -> dict:
    """Les champs d'une source, vérifiés ; `token` «» = non donné."""
    owned.body(body)
    forge = body.get("forge") or "gitlab"
    if forge not in FORGES:
        raise RepoError("`forge` : " + ", ".join(FORGES))
    url = str(body.get("url") or "").strip().rstrip("/") or \
        ("https://github.com" if forge == "github" else "")
    url = owned.http_url(url, "l'adresse de la forge, une URL")
    if "@" in urlsplit(url).netloc:
        # L'adresse est montrée (à la page, à tous pour une partagée) : le
        # jeton a son propre champ, jamais l'URL.
        raise RepoError("pas d'identifiants dans l'adresse : le jeton a son propre champ")
    token = str(body.get("token") or "").strip()
    if len(token) > MAX_TOKEN:
        raise RepoError("jeton trop long")
    projects = body.get("projects") or []
    if isinstance(projects, str):
        projects = [p for p in re.split(r"[\s,]+", projects) if p]
    if not isinstance(projects, list) or len(projects) > MAX_PATTERNS \
            or not all(isinstance(p, str) and 0 < len(p) <= 200 for p in projects):
        raise RepoError("`projects` : une liste de motifs (« groupe/* »)")
    return {"name": owned.name(body.get("name")), "forge": forge, "url": url,
            "username": str(body.get("username") or "").strip()[:100],
            "token": token, "projects": json.dumps(projects)}


def _needs_token(forge: str) -> bool:
    """Une forge à API demande un jeton ; un dépôt git peut être public."""
    return forge != "git"


def list_own(owner: str) -> list[dict]:
    rows = _run(lambda c: c.execute(
        f"SELECT {_FIELDS} FROM repo_sources WHERE owner = ? ORDER BY created, id",
        (owner,)).fetchall())
    return [_meta(r) for r in rows]


def create(owner: str, body) -> dict:
    f = _fields(body)
    if not f["token"] and _needs_token(f["forge"]):
        raise RepoError("un jeton est attendu")
    sid = secrets.token_urlsafe(12)

    def q(c):
        owned.room(c, "repo_sources", owner, MAX_PER_USER, "sources")
        if owned.taken(c, "repo_sources", owner, f["name"]):
            raise RepoError("une source porte déjà ce nom")
        c.execute("INSERT INTO repo_sources (owner, id, name, forge, url, username, token, "
                  "projects, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (owner, sid, f["name"], f["forge"], f["url"], f["username"], f["token"],
                   f["projects"], time.time()))
        return c.execute(f"SELECT {_FIELDS} FROM repo_sources WHERE owner = ? AND id = ?",
                         (owner, sid)).fetchone()
    return _meta(_run(q))


def update(owner: str, sid: str, body) -> dict:
    """Le jeton absent reste (la page ne le connaît pas) — sauf si la source
    change d'origine ou de forge : il n'irait pas, sans être ressaisi, vers
    un autre hôte."""
    _check_id(sid)
    f = _fields(body)

    def q(c):
        old = c.execute("SELECT url, forge, token FROM repo_sources WHERE owner = ? AND id = ?",
                        (owner, sid)).fetchone()
        if old is None:
            raise RepoError("source inconnue", 404)
        moved = origin(old[0]) != origin(f["url"]) or old[1] != f["forge"]
        token = f["token"] or ("" if moved else old[2])
        if not token and _needs_token(f["forge"]):
            raise RepoError("un jeton est attendu (la source change d'adresse ou de forge)"
                            if moved else "un jeton est attendu")
        if owned.taken(c, "repo_sources", owner, f["name"], sid):
            raise RepoError("une source porte déjà ce nom")
        c.execute("UPDATE repo_sources SET name = ?, forge = ?, url = ?, username = ?, "
                  "projects = ?, token = ? WHERE owner = ? AND id = ?",
                  (f["name"], f["forge"], f["url"], f["username"], f["projects"], token,
                   owner, sid))
        return c.execute(f"SELECT {_FIELDS} FROM repo_sources WHERE owner = ? AND id = ?",
                         (owner, sid)).fetchone()
    return _meta(_run(q))


def delete(owner: str, sid: str) -> int:
    _check_id(sid)
    return _run(lambda c: c.execute("DELETE FROM repo_sources WHERE owner = ? AND id = ?",
                                    (owner, sid)).rowcount)


def downloaded(owner: str, sid: str, error: str = "") -> None:
    """Un téléchargement de la source, réussi («») ou non (son erreur)."""
    _run(lambda c: c.execute(
        "UPDATE repo_sources SET last_used = ?, last_error = ? WHERE owner = ? AND id = ?",
        (time.time(), error[:500], owner, sid)))


# ── les sources partagées ([repos.sources.<nom>]) ───────────────────────

def _shared_from(table: dict) -> list[dict]:
    out = []
    for name, t in (table or {}).items():
        if not isinstance(t, dict) or t.get("enabled") is False:
            continue
        try:
            f = _fields({**t, "name": name})
        except RepoError as exc:
            raise SystemExit(f"[repos.sources.{name}] : {exc}")
        out.append({"id": "shared:" + f["name"], "name": f["name"], "forge": f["forge"],
                    "url": f["url"], "username": f["username"], "token": f["token"],
                    "projects": json.loads(f["projects"]), "shared": True,
                    "created": None, "last_used": None, "last_error": ""})
    return out


# Lues seulement dépôts actifs : une source mal écrite n'arrête pas un proxy
# qui ne s'en sert pas.
SHARED = _shared_from(config.get("repos.sources", {})) \
    if config.flag("repos.enabled", False) else []


def shared() -> list[dict]:
    """Les sources partagées, SANS jeton : pour la page."""
    return [{k: v for k, v in s.items() if k != "token"} for s in SHARED]


def available(owner: str) -> list[dict]:
    """Les sources du compte, puis les partagées qu'aucune des siennes ne
    masque ; jetons COMPRIS — pour l'outil seulement. BLOQUANT."""
    rows = _run(lambda c: c.execute(
        f"SELECT {_FIELDS}, token FROM repo_sources WHERE owner = ? ORDER BY created, id",
        (owner,)).fetchall())
    own = [_meta(r[:-1], r[-1]) for r in rows]
    names = {s["name"].casefold() for s in own}
    return own + [s for s in SHARED if s["name"].casefold() not in names]


def permits(source: dict, project: str) -> bool:
    """Le projet est-il permis par les motifs de la source (casse ignorée) ?"""
    patterns = source.get("projects") or []
    return not patterns or any(fnmatch.fnmatch(project.lower(), p.lower()) for p in patterns)
