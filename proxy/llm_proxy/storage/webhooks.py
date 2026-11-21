"""
Les WEBHOOKS d'un compte, définis par lui-même (Configuration → Webhooks),
liés au même client que son historique (auth.ui_client) :

  entrants  une adresse secrète, POST /hooks/<jeton>, qui reçoit un
            message au format d'un webhook entrant de Slack ({"text": …}).
            Le proxy le range dans une conversation du chat de la page
            (une nouvelle à chaque fois, ou toujours la même) et y fait
            répondre le modèle du webhook (ui/webhook_turn.py). Le jeton
            n'est montré qu'à la création (ou à sa régénération) : seul
            son condensé est gardé, comme celui d'une clé.
  sortants  une URL compatible Slack (Slack, Mattermost, Rocket.Chat,
            Discord en /slack…) où le proxy POSTe {"text": …} : la
            réponse à un webhook entrant qui la nomme (`reply`), et
            l'outil hébergé `webhook` (tools/webhook.py), que le modèle
            appelle de n'importe quelle conversation du compte.

Un entrant garde aussi QUI l'a créé (son compte, son api_key_id) : ses
requêtes sont comptées à ce client, et refusées si le compte est bloqué
ou la clé retirée. Même base que l'historique (conversations.db,
sauvegardée avec lui, backup.py), ses tables à part.
"""

import json
import secrets
import time
from urllib.parse import urlsplit

from ..core import config, identity
from . import conversations

MAX_PER_USER = config.integer("webhooks.max_per_user", 20)
MAX_NAME = 80
MAX_MODEL = 200
MAX_SYSTEM = 20_000
MAX_URL = 2000
MAX_TOOLS = 40
TARGETS = ("new", "conversation")
# Le préfixe d'un jeton : de quoi le reconnaître dans un journal ou un
# gestionnaire de secrets.
TOKEN_PREFIX = "wh-"

_IN = ("id, name, hint, model, model_override, system, tools, target, "
       "conversation, reply, account, api_key_id, created, last_used, calls, "
       "last_error")
_OUT = "id, name, url, created, last_used, calls, last_error"


class WebhookError(conversations.HistoryError):
    """Une opération refusée ; le message est pour la page."""


def _tables(c) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS webhooks_in (
        owner TEXT NOT NULL, id TEXT NOT NULL, digest TEXT NOT NULL UNIQUE,
        hint TEXT NOT NULL, name TEXT NOT NULL, model TEXT NOT NULL,
        model_override INTEGER NOT NULL, system TEXT NOT NULL,
        tools TEXT NOT NULL, target TEXT NOT NULL, conversation TEXT NOT NULL,
        reply TEXT NOT NULL, account INTEGER, api_key_id TEXT NOT NULL,
        created REAL NOT NULL, last_used REAL, calls INTEGER NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '', PRIMARY KEY (owner, id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS webhooks_out (
        owner TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL,
        url TEXT NOT NULL, created REAL NOT NULL, last_used REAL,
        calls INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (owner, id))""")


def _run(fn):
    def q(c):
        _tables(c)
        return fn(c)
    return conversations._run(q)


def _ms(ts):
    """Une date pour la page : des secondes (lib/format.js, stamp), comme
    les clés."""
    return round(ts) if ts else None


def _check_id(wid) -> str:
    if not isinstance(wid, str) or not conversations._ID.match(wid):
        raise WebhookError("identifiant de webhook invalide")
    return wid


def _name(value) -> str:
    name = " ".join(str(value or "").split())[:MAX_NAME]
    if not name:
        raise WebhookError("un nom est attendu")
    return name


def _digest(token: str) -> str:
    return identity.owner("\0webhook:" + token)


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(24)


# ── entrants ────────────────────────────────────────────────────────────

def _in_meta(row) -> dict:
    (wid, name, hint, model, override, system, tools, target, conv, reply,
     account, api_key_id, created, last_used, calls, last_error) = row
    return {"id": wid, "name": name, "hint": hint, "model": model,
            "model_override": bool(override), "system": system,
            "tools": json.loads(tools), "target": target,
            "conversation": conv, "reply": reply, "account": account,
            "api_key_id": api_key_id, "created": _ms(created),
            "last_used": _ms(last_used), "calls": calls,
            "last_error": last_error}


def _in_fields(body, c, owner: str) -> dict:
    """Les champs réglables d'un entrant, vérifiés."""
    if not isinstance(body, dict):
        raise WebhookError("corps JSON attendu")
    model = str(body.get("model") or "").strip()[:MAX_MODEL]
    system = str(body.get("system") or "")
    if len(system) > MAX_SYSTEM:
        raise WebhookError(f"prompt système trop long ({MAX_SYSTEM} caractères au plus)")
    tools = body.get("tools") or []
    if not isinstance(tools, list) or len(tools) > MAX_TOOLS \
            or not all(isinstance(t, str) and 0 < len(t) <= 100 for t in tools):
        raise WebhookError("`tools` : une liste de types d'outils hébergés")
    target = body.get("target") or "new"
    if target not in TARGETS:
        raise WebhookError("`target` : « new » (une conversation par message) "
                           "ou « conversation » (toujours la même)")
    conv = str(body.get("conversation") or "") if target == "conversation" else ""
    if conv:
        conversations._check_id(conv)
    reply = str(body.get("reply") or "")
    if reply and not c.execute("SELECT 1 FROM webhooks_out WHERE owner = ? AND id = ?",
                               (owner, reply)).fetchone():
        raise WebhookError("webhook sortant de réponse inconnu")
    return {"name": _name(body.get("name")), "model": model,
            "model_override": int(bool(body.get("model_override"))),
            "system": system, "tools": json.dumps(list(dict.fromkeys(tools))),
            "target": target, "conversation": conv, "reply": reply}


def list_in(owner: str) -> list[dict]:
    rows = _run(lambda c: c.execute(
        f"SELECT {_IN} FROM webhooks_in WHERE owner = ? ORDER BY created, id",
        (owner,)).fetchall())
    return [_in_meta(r) for r in rows]


def create_in(owner: str, body, account: int | None, api_key_id: str) -> tuple[dict, str]:
    """Un nouvel entrant → (sa description, son jeton en clair : montré une
    seule fois)."""
    token = new_token()
    wid = secrets.token_urlsafe(12)

    def q(c):
        f = _in_fields(body, c, owner)
        n, = c.execute("SELECT count(*) FROM webhooks_in WHERE owner = ?",
                       (owner,)).fetchone()
        if n >= MAX_PER_USER:
            raise WebhookError(f"{MAX_PER_USER} webhooks entrants au plus", 413)
        c.execute("INSERT INTO webhooks_in (owner, id, digest, hint, name, model, "
                  "model_override, system, tools, target, conversation, reply, "
                  "account, api_key_id, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, "
                  "?, ?, ?, ?, ?, ?)",
                  (owner, wid, _digest(token), token[len(TOKEN_PREFIX):][:6],
                   f["name"], f["model"], f["model_override"], f["system"],
                   f["tools"], f["target"], f["conversation"], f["reply"],
                   account, api_key_id, time.time()))
        return c.execute(f"SELECT {_IN} FROM webhooks_in WHERE owner = ? AND id = ?",
                         (owner, wid)).fetchone()
    return _in_meta(_run(q)), token


def update_in(owner: str, wid: str, body) -> dict:
    _check_id(wid)

    def q(c):
        f = _in_fields(body, c, owner)
        n = c.execute("UPDATE webhooks_in SET name = ?, model = ?, model_override = ?, "
                      "system = ?, tools = ?, target = ?, conversation = ?, reply = ? "
                      "WHERE owner = ? AND id = ?",
                      (f["name"], f["model"], f["model_override"], f["system"],
                       f["tools"], f["target"], f["conversation"], f["reply"],
                       owner, wid)).rowcount
        if not n:
            raise WebhookError("webhook inconnu", 404)
        return c.execute(f"SELECT {_IN} FROM webhooks_in WHERE owner = ? AND id = ?",
                         (owner, wid)).fetchone()
    return _in_meta(_run(q))


def rotate_in(owner: str, wid: str) -> tuple[dict, str]:
    """Un nouveau jeton : l'ancienne adresse cesse de répondre."""
    _check_id(wid)
    token = new_token()

    def q(c):
        n = c.execute("UPDATE webhooks_in SET digest = ?, hint = ? WHERE owner = ? "
                      "AND id = ?", (_digest(token), token[len(TOKEN_PREFIX):][:6],
                                     owner, wid)).rowcount
        if not n:
            raise WebhookError("webhook inconnu", 404)
        return c.execute(f"SELECT {_IN} FROM webhooks_in WHERE owner = ? AND id = ?",
                         (owner, wid)).fetchone()
    return _in_meta(_run(q)), token


def delete_in(owner: str, wid: str) -> int:
    _check_id(wid)
    return _run(lambda c: c.execute("DELETE FROM webhooks_in WHERE owner = ? AND id = ?",
                                    (owner, wid)).rowcount)


def by_token(token: str) -> tuple[str, dict] | None:
    """(client, description) de l'entrant dont c'est le jeton ; None sinon."""
    if not isinstance(token, str) or not token.startswith(TOKEN_PREFIX) \
            or len(token) > 100:
        return None
    row = _run(lambda c: c.execute(
        f"SELECT owner, {_IN} FROM webhooks_in WHERE digest = ?",
        (_digest(token),)).fetchone())
    return (row[0], _in_meta(row[1:])) if row else None


def get_in(owner: str, wid: str) -> dict | None:
    _check_id(wid)
    row = _run(lambda c: c.execute(
        f"SELECT {_IN} FROM webhooks_in WHERE owner = ? AND id = ?",
        (owner, wid)).fetchone())
    return _in_meta(row) if row else None


def used_in(owner: str, wid: str, error: str = "") -> None:
    """Un message reçu et traité : compté, son erreur (ou «») gardée."""
    _run(lambda c: c.execute(
        "UPDATE webhooks_in SET last_used = ?, calls = calls + 1, last_error = ? "
        "WHERE owner = ? AND id = ?", (time.time(), error[:500], owner, wid)))


def set_conversation(owner: str, wid: str, cid: str) -> None:
    """La conversation dédiée qu'un entrant « toujours la même » vient de
    créer (s'il n'en a pas reçu une autre entre-temps)."""
    _run(lambda c: c.execute(
        "UPDATE webhooks_in SET conversation = ? WHERE owner = ? AND id = ? "
        "AND target = 'conversation' AND conversation = ''", (cid, owner, wid)))


# ── sortants ────────────────────────────────────────────────────────────

def mask(url: str) -> str:
    """L'URL d'un sortant telle que la page la montre : son hôte et la fin
    de son chemin — l'URL entière vaut droit d'écrire."""
    parts = urlsplit(url)
    tail = parts.path[-4:] if len(parts.path) > 8 else ""
    return f"{parts.scheme}://{parts.hostname or ''}/…{tail}"


def _out_meta(row, full: bool = False) -> dict:
    wid, name, url, created, last_used, calls, last_error = row
    return {"id": wid, "name": name, "url": url if full else mask(url),
            "created": _ms(created), "last_used": _ms(last_used),
            "calls": calls, "last_error": last_error}


def _url(value) -> str:
    url = str(value or "").strip()
    if len(url) > MAX_URL or not url.lower().startswith(("https://", "http://")):
        raise WebhookError("une URL http(s) est attendue")
    if urlsplit(url).path.startswith("/hooks/" + TOKEN_PREFIX):
        # Un sortant vers un entrant d'un proxy : chaque réponse en ferait
        # un nouveau message, sans fin.
        raise WebhookError("un webhook sortant ne peut pas viser un webhook entrant du proxy")
    return url


def list_out(owner: str, full: bool = False) -> list[dict]:
    rows = _run(lambda c: c.execute(
        f"SELECT {_OUT} FROM webhooks_out WHERE owner = ? ORDER BY created, id",
        (owner,)).fetchall())
    return [_out_meta(r, full) for r in rows]


def create_out(owner: str, body) -> dict:
    if not isinstance(body, dict):
        raise WebhookError("corps JSON attendu")
    name, url = _name(body.get("name")), _url(body.get("url"))
    wid = secrets.token_urlsafe(12)

    def q(c):
        n, = c.execute("SELECT count(*) FROM webhooks_out WHERE owner = ?",
                       (owner,)).fetchone()
        if n >= MAX_PER_USER:
            raise WebhookError(f"{MAX_PER_USER} webhooks sortants au plus", 413)
        if c.execute("SELECT 1 FROM webhooks_out WHERE owner = ? AND name = ?",
                     (owner, name)).fetchone():
            raise WebhookError("un webhook sortant porte déjà ce nom")
        c.execute("INSERT INTO webhooks_out (owner, id, name, url, created) "
                  "VALUES (?, ?, ?, ?, ?)", (owner, wid, name, url, time.time()))
        return c.execute(f"SELECT {_OUT} FROM webhooks_out WHERE owner = ? AND id = ?",
                         (owner, wid)).fetchone()
    return _out_meta(_run(q))


def update_out(owner: str, wid: str, body) -> dict:
    """Le nom, et l'URL si elle est donnée (la page ne la connaît pas :
    absente, elle reste)."""
    _check_id(wid)
    if not isinstance(body, dict):
        raise WebhookError("corps JSON attendu")
    name = _name(body.get("name"))
    url = _url(body["url"]) if body.get("url") else None

    def q(c):
        if c.execute("SELECT 1 FROM webhooks_out WHERE owner = ? AND name = ? AND id != ?",
                     (owner, name, wid)).fetchone():
            raise WebhookError("un webhook sortant porte déjà ce nom")
        n = c.execute("UPDATE webhooks_out SET name = ?, url = coalesce(?, url) "
                      "WHERE owner = ? AND id = ?", (name, url, owner, wid)).rowcount
        if not n:
            raise WebhookError("webhook inconnu", 404)
        return c.execute(f"SELECT {_OUT} FROM webhooks_out WHERE owner = ? AND id = ?",
                         (owner, wid)).fetchone()
    return _out_meta(_run(q))


def delete_out(owner: str, wid: str) -> int:
    """Supprime un sortant ; les entrants qui y répondaient n'y répondent plus."""
    _check_id(wid)

    def q(c):
        c.execute("UPDATE webhooks_in SET reply = '' WHERE owner = ? AND reply = ?",
                  (owner, wid))
        return c.execute("DELETE FROM webhooks_out WHERE owner = ? AND id = ?",
                         (owner, wid)).rowcount
    return _run(q)


def get_out(owner: str, wid: str) -> dict | None:
    """Un sortant, URL comprise (pour l'envoi) ; None si inconnu."""
    row = _run(lambda c: c.execute(
        f"SELECT {_OUT} FROM webhooks_out WHERE owner = ? AND id = ?",
        (owner, wid)).fetchone())
    return _out_meta(row, full=True) if row else None


def used_out(owner: str, wid: str, error: str = "") -> None:
    _run(lambda c: c.execute(
        "UPDATE webhooks_out SET last_used = ?, calls = calls + 1, last_error = ? "
        "WHERE owner = ? AND id = ?", (time.time(), error[:500], owner, wid)))
