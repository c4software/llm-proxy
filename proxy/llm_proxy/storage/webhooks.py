"""
Les WEBHOOKS d'un compte, définis par lui-même (Configuration → Webhooks),
liés au même client que son historique (auth.ui_client) :

  entrants  une adresse secrète, POST /hooks/<jeton>, qui reçoit un
            message au format d'un webhook entrant de Slack ({"text": …}).
            Le proxy le range dans une conversation du chat de la page
            (une nouvelle à chaque fois, ou toujours la même) et y fait
            répondre le modèle du webhook (ui/webhook_turn.py) — ou, en
            passe plat (`target` « none »), ne range rien et n'envoie la
            réponse qu'au sortant. Le jeton est gardé EN CLAIR (la page
            réaffiche l'adresse : bouton Adresse) et retrouvé par son
            condensé ; une copie de la base donne donc des adresses
            utilisables, comme les URL des sortants.
  sortants  une URL Apprise (slack://, mmost://, discord://, mailto://…),
            ou une URL http(s) qu'Apprise ne reconnaît pas, où le proxy
            POSTe {"text": …} (et `channel`, son salon) : la réponse à un
            webhook entrant qui la nomme (`reply`) ; et, s'il est EXPOSÉ
            à l'outil (`tool`, décoché par défaut), ce que le modèle y
            poste par l'outil hébergé `webhook` (tools/webhook.py), de
            n'importe quelle conversation du compte.

Un entrant garde aussi QUI l'a créé (son compte, son api_key_id) : ses
requêtes sont comptées à ce client, et refusées si le compte est bloqué
ou la clé retirée. Même base que l'historique (conversations.db,
sauvegardée avec lui, backup.py), ses tables à part.
"""

import json
import logging
import re
import secrets
import time
from urllib.parse import quote, unquote, urlsplit

import apprise

from ..core import config, identity
from . import conversations, owned

MAX_PER_USER = config.integer("webhooks.max_per_user", 20)
# Délai (s) d'un envoi sortant (Apprise, ou POST JSON).
TIMEOUT = config.num("webhooks.timeout", 10.0)
MAX_MODEL = 200
MAX_SYSTEM = 20_000
MAX_TOOLS = 40
# « none » : le passe plat — rien n'est rangé, la réponse ne va qu'au sortant.
TARGETS = ("new", "conversation", "none")
# Le préfixe d'un jeton : de quoi le reconnaître dans un journal ou un
# gestionnaire de secrets.
TOKEN_PREFIX = "wh-"

_IN = ("id, name, hint, model, model_override, system, tools, target, "
       "conversation, reply, account, api_key_id, created, last_used, calls, "
       "last_error, token != ''")
_OUT = "id, name, url, channel, tool, created, last_used, calls, last_error"


# Une opération refusée : celle des magasins par compte (owned.py).
WebhookError = owned.OwnedError


def _tables(c) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS webhooks_in (
        owner TEXT NOT NULL, id TEXT NOT NULL, digest TEXT NOT NULL UNIQUE,
        hint TEXT NOT NULL, name TEXT NOT NULL, model TEXT NOT NULL,
        model_override INTEGER NOT NULL, system TEXT NOT NULL,
        tools TEXT NOT NULL, target TEXT NOT NULL, conversation TEXT NOT NULL,
        reply TEXT NOT NULL, account INTEGER, api_key_id TEXT NOT NULL,
        created REAL NOT NULL, last_used REAL, calls INTEGER NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '', token TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (owner, id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS webhooks_out (
        owner TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL,
        url TEXT NOT NULL, created REAL NOT NULL, last_used REAL,
        calls INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
        channel TEXT NOT NULL DEFAULT '', tool INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (owner, id))""")
    # Une base d'avant : `channel` absent en v2026.11.21, `token` et `tool`
    # absents jusqu'à v2026.11.21.1. Les sortants de ces bases restent
    # exposés à l'outil (DEFAULT 1), comme ils l'étaient alors ; un sortant
    # créé ne l'est que si on le coche (create_out donne toujours `tool`).
    owned.add_columns(c, "webhooks_out", {"channel": "TEXT NOT NULL DEFAULT ''",
                                          "tool": "INTEGER NOT NULL DEFAULT 1"})
    owned.add_columns(c, "webhooks_in", {"token": "TEXT NOT NULL DEFAULT ''"})


_run = owned.runner(_tables)


def _check_id(wid) -> str:
    return owned.check_id(wid, "webhook")


def _digest(token: str) -> str:
    return identity.owner("\0webhook:" + token)


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(24)


# ── entrants ────────────────────────────────────────────────────────────

def _in_meta(row) -> dict:
    (wid, name, hint, model, override, system, tools, target, conv, reply,
     account, api_key_id, created, last_used, calls, last_error, copyable) = row
    return {"id": wid, "name": name, "hint": hint, "model": model,
            "model_override": bool(override), "system": system,
            "tools": json.loads(tools), "target": target,
            "conversation": conv, "reply": reply, "account": account,
            "api_key_id": api_key_id, "created": owned.seconds(created),
            "last_used": owned.seconds(last_used), "calls": calls,
            "last_error": last_error,
            # Le lien se relit (token_in) ; faux pour un entrant d'avant
            # v2026.11.21.2, dont seul le condensé est gardé : à régénérer.
            "copyable": bool(copyable)}


def _in_fields(body, c, owner: str) -> dict:
    """Les champs réglables d'un entrant, vérifiés."""
    owned.body(body)
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
        raise WebhookError("`target` : « new » (une conversation par message), "
                           "« conversation » (toujours la même) ou « none » (passe plat)")
    conv = str(body.get("conversation") or "") if target == "conversation" else ""
    if conv:
        conversations.check_id(conv)
    reply = str(body.get("reply") or "")
    if target == "none" and not reply:
        raise WebhookError("le passe plat (aucune conversation) exige un webhook "
                           "sortant de réponse")
    if reply and not c.execute("SELECT 1 FROM webhooks_out WHERE owner = ? AND id = ?",
                               (owner, reply)).fetchone():
        raise WebhookError("webhook sortant de réponse inconnu")
    return {"name": owned.name(body.get("name")), "model": model,
            "model_override": int(bool(body.get("model_override"))),
            "system": system, "tools": json.dumps(list(dict.fromkeys(tools))),
            "target": target, "conversation": conv, "reply": reply}


def list_in(owner: str) -> list[dict]:
    rows = _run(lambda c: c.execute(
        f"SELECT {_IN} FROM webhooks_in WHERE owner = ? ORDER BY created, id",
        (owner,)).fetchall())
    return [_in_meta(r) for r in rows]


def create_in(owner: str, body, account: int | None, api_key_id: str) -> tuple[dict, str]:
    """Un nouvel entrant → (sa description, son jeton)."""
    token = new_token()
    wid = secrets.token_urlsafe(12)

    def q(c):
        f = _in_fields(body, c, owner)
        owned.room(c, "webhooks_in", owner, MAX_PER_USER, "webhooks entrants")
        c.execute("INSERT INTO webhooks_in (owner, id, digest, token, hint, name, model, "
                  "model_override, system, tools, target, conversation, reply, "
                  "account, api_key_id, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                  "?, ?, ?, ?, ?, ?)",
                  (owner, wid, _digest(token), token, token[len(TOKEN_PREFIX):][:6],
                   f["name"], f["model"], f["model_override"], f["system"],
                   f["tools"], f["target"], f["conversation"], f["reply"],
                   account, api_key_id, time.time()))
        return c.execute(f"SELECT {_IN} FROM webhooks_in WHERE owner = ? AND id = ?",
                         (owner, wid)).fetchone()
    return _in_meta(_run(q)), token


def update_in(owner: str, wid: str, body) -> dict:
    """Remplace TOUS les réglages de l'entrant (la page les envoie tous) :
    un champ absent reprend sa valeur par défaut."""
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
        n = c.execute("UPDATE webhooks_in SET digest = ?, token = ?, hint = ? WHERE "
                      "owner = ? AND id = ?", (_digest(token), token,
                                               token[len(TOKEN_PREFIX):][:6],
                                               owner, wid)).rowcount
        if not n:
            raise WebhookError("webhook inconnu", 404)
        return c.execute(f"SELECT {_IN} FROM webhooks_in WHERE owner = ? AND id = ?",
                         (owner, wid)).fetchone()
    return _in_meta(_run(q)), token


def token_in(owner: str, wid: str) -> str | None:
    """Le jeton d'un entrant du compte (le lien à copier) ; «» pour un
    entrant d'avant qu'il soit gardé, None si inconnu."""
    _check_id(wid)
    row = _run(lambda c: c.execute(
        "SELECT token FROM webhooks_in WHERE owner = ? AND id = ?", (owner, wid)).fetchone())
    return row[0] if row else None


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
    créer : la première, ou une nouvelle quand la sienne a été supprimée.
    Ses tours passent l'un après l'autre (webhook_turn._locks)."""
    _run(lambda c: c.execute(
        "UPDATE webhooks_in SET conversation = ? WHERE owner = ? AND id = ? "
        "AND target = 'conversation'", (cid, owner, wid)))


# ── sortants ────────────────────────────────────────────────────────────

# L'agent des envois sortants, Apprise ou JSON : un entrant le refuse
# (un sortant pointé sur un entrant bouclerait).
USER_AGENT = "llm-proxy-webhook"
# Apprise : stockage en MÉMOIRE (le conteneur est en lecture seule, rien n'a
# à survivre), le délai d'un envoi, et ni nom ni image d'Apprise sur les
# messages (pied Slack, avatar Discord, nom Mattermost). `app_id` est aussi
# son User-Agent.
ASSET = apprise.AppriseAsset(
    storage_mode=apprise.PersistentStoreMode.MEMORY, service_timeout=TIMEOUT,
    app_id=USER_AGENT, app_desc="llm-proxy", app_url="https://github.com/c4software/llm-proxy",
    image_url_mask=None, image_url_logo=None)
# Les journaux d'Apprise citent les URL, donc leurs secrets : coupés du
# journal du proxy. L'erreur d'un envoi est lue par son log_callback.
logging.getLogger("apprise").propagate = False


def target(url: str):
    """Le service Apprise de cette URL (un objet d'Apprise), None si Apprise
    ne la reconnaît pas : une URL http(s) quelconque, un sortant JSON."""
    return apprise.Apprise.instantiate(url, asset=ASSET)


def describe(url: str) -> dict:
    """Le service d'un sortant, et `legacy` pour un envoi JSON hors Apprise,
    à ressaisir en URL Apprise."""
    found = target(url)
    return {"service": found.service_name if found is not None else "JSON compatible Slack",
            "legacy": found is None}


def _out_meta(row) -> dict:
    wid, name, url, channel, tool, created, last_used, calls, last_error = row
    seen = describe(url)
    # L'URL entière : elle n'est rendue qu'à son compte, qui la réaffiche et
    # la copie (comme l'adresse d'un entrant).
    return {"id": wid, "name": name, "url": url,
            "service": seen["service"], "legacy": seen["legacy"],
            # Le salon d'un sortant JSON, envoyé en `channel` ; avec Apprise,
            # il est dans l'URL (slack://…/#alertes).
            "channel": channel if seen["legacy"] else "",
            # Offert à l'outil `webhook` du modèle ; sinon, réponses des entrants seulement.
            "tool": bool(tool),
            "created": owned.seconds(created), "last_used": owned.seconds(last_used),
            "calls": calls, "last_error": last_error}


def _url(value) -> str:
    """Une URL Apprise (slack://, mmost://, discord://, tgram://, mailto://…,
    ou l'URL d'un webhook qu'Apprise reconnaît), ou une URL http(s) : un
    envoi JSON compatible Slack."""
    url = str(value or "").strip()
    if len(url) > owned.MAX_URL:
        raise WebhookError("URL trop longue")
    found = target(url)
    if found is None:
        url = owned.http_url(url, "une URL Apprise (slack://, mmost://, discord://…) ou une URL")
    if re.search(r"(^|[/=])" + TOKEN_PREFIX, unquote(url).casefold()):
        # Un sortant vers un entrant d'un proxy (quel que soit le schéma :
        # mmost://hôte/wh-…, json://hôte/%68ooks/wh-…) : chaque réponse en
        # ferait un nouveau message, sans fin.
        raise WebhookError("un webhook sortant ne peut pas viser un webhook entrant du proxy")
    # Les paramètres lus comme Apprise : « + » est un caractère, pas un espace.
    query = [tuple(unquote(x) for x in (part.split("=", 1) + [""])[:2])
             for part in urlsplit(url).query.split("&") if part]
    if any(k.casefold() == "+user-agent" for k, _ in query):
        # Le User-Agent est ce par quoi un entrant reconnaît un proxy.
        raise WebhookError("l'en-tête User-Agent d'un sortant ne se change pas")
    if any(v.startswith(("/", "~", "file:", ".")) for _, v in query):
        # keyfile=/etc/… : Apprise lirait un fichier du proxy.
        raise WebhookError("une URL de sortant ne désigne pas de fichier local")
    return url


def _flag(value) -> bool:
    """`tool` : un booléen JSON, rien d'autre (1, "true" refusés)."""
    if not isinstance(value, bool):
        raise WebhookError("`tool` : true ou false")
    return value


def list_out(owner: str) -> list[dict]:
    rows = _run(lambda c: c.execute(
        f"SELECT {_OUT} FROM webhooks_out WHERE owner = ? ORDER BY created, id",
        (owner,)).fetchall())
    return [_out_meta(r) for r in rows]


def create_out(owner: str, body) -> dict:
    owned.body(body)
    name, url = owned.name(body.get("name")), _url(body.get("url"))
    # Pas exposé à l'outil sauf demande : un salon n'est pas offert au modèle
    # sans qu'on l'ait choisi.
    tool = int(_flag(body.get("tool", False)))
    wid = secrets.token_urlsafe(12)

    def q(c):
        owned.room(c, "webhooks_out", owner, MAX_PER_USER, "webhooks sortants")
        if owned.taken(c, "webhooks_out", owner, name):
            raise WebhookError("un webhook sortant porte déjà ce nom")
        c.execute("INSERT INTO webhooks_out (owner, id, name, url, channel, tool, created) "
                  "VALUES (?, ?, ?, ?, '', ?, ?)", (owner, wid, name, url, tool, time.time()))
        return c.execute(f"SELECT {_OUT} FROM webhooks_out WHERE owner = ? AND id = ?",
                         (owner, wid)).fetchone()
    return _out_meta(_run(q))


def update_out(owner: str, wid: str, body) -> dict:
    """Le nom, l'exposition à l'outil, et l'URL si elle est donnée (la page
    ne la connaît pas : absente, elle reste). Une URL redonnée efface le
    salon d'un sortant JSON d'avant : avec Apprise, il est dans l'URL."""
    _check_id(wid)
    owned.body(body)
    name = owned.name(body.get("name"))
    # Absent (un ancien onglet, un script) : inchangé, comme l'URL.
    tool = None if "tool" not in body else int(_flag(body["tool"]))
    url = _url(body["url"]) if body.get("url") else None

    def q(c):
        if owned.taken(c, "webhooks_out", owner, name, wid):
            raise WebhookError("un webhook sortant porte déjà ce nom")
        n = c.execute("UPDATE webhooks_out SET name = ?, tool = coalesce(?, tool), "
                      "url = coalesce(?, url), channel = CASE WHEN ? IS NULL THEN channel "
                      "ELSE '' END WHERE owner = ? AND id = ?",
                      (name, tool, url, url, owner, wid)).rowcount
        if not n:
            raise WebhookError("webhook inconnu", 404)
        return c.execute(f"SELECT {_OUT} FROM webhooks_out WHERE owner = ? AND id = ?",
                         (owner, wid)).fetchone()
    return _out_meta(_run(q))


def delete_out(owner: str, wid: str) -> int:
    """Supprime un sortant ; les entrants qui y répondaient n'y répondent
    plus. Refusé (409) s'il est le sortant d'un passe plat, qui n'aurait
    plus où répondre."""
    _check_id(wid)

    def q(c):
        used = [n for n, in c.execute("SELECT name FROM webhooks_in WHERE owner = ? AND "
                                      "reply = ? AND target = 'none'", (owner, wid))]
        if used:
            raise WebhookError("sortant de réponse du passe plat « " + " », « ".join(used)
                               + " » : changez d'abord ce webhook entrant", 409)
        c.execute("UPDATE webhooks_in SET reply = '' WHERE owner = ? AND reply = ?",
                  (owner, wid))
        return c.execute("DELETE FROM webhooks_out WHERE owner = ? AND id = ?",
                         (owner, wid)).rowcount
    return _run(q)


def get_out(owner: str, wid: str) -> dict | None:
    """Un sortant, URL comprise (pour l'envoi) ; None si inconnu."""
    _check_id(wid)
    row = _run(lambda c: c.execute(
        f"SELECT {_OUT} FROM webhooks_out WHERE owner = ? AND id = ?",
        (owner, wid)).fetchone())
    return _out_meta(row) if row else None


def used_out(owner: str, wid: str, error: str = "") -> None:
    _run(lambda c: c.execute(
        "UPDATE webhooks_out SET last_used = ?, calls = calls + 1, last_error = ? "
        "WHERE owner = ? AND id = ?", (time.time(), error[:500], owner, wid)))


def clear_inherited_prompts() -> int:
    """Une seule fois par base : le prompt système d'un webhook entrant que
    des conversations ont pris dans leurs réglages (créées par lui, ou
    ouvertes depuis l'une d'elles dans la page, jusqu'à v2026.11.21.5) en
    est retiré — même texte, même compte. Il ne sert qu'aux tours du
    webhook. → le nombre de conversations nettoyées. BLOQUANT."""
    def q(c):
        if not owned.once(c, "webhook-prompts-out-of-conversations"):
            return 0
        prompts: dict[str, set[str]] = {}
        for owner, system in c.execute("SELECT owner, system FROM webhooks_in WHERE system != ''"):
            prompts.setdefault(owner, set()).add(system.strip())
        cleaned = 0
        for owner, texts in prompts.items():
            for cid, raw, rev in c.execute("SELECT id, doc, rev FROM conversations WHERE owner = ?",
                                           (owner,)).fetchall():
                try:
                    doc = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(doc, dict) or str(doc.get("system") or "").strip() not in texts:
                    continue
                doc["system"] = ""
                c.execute("UPDATE conversations SET doc = ?, rev = ? WHERE owner = ? AND id = ?",
                          (json.dumps(doc, ensure_ascii=False, separators=(",", ":")), rev + 1,
                           owner, cid))
                cleaned += 1
        return cleaned
    return _run(q)


_SLACK_HOOK = re.compile(r"https://hooks\.slack\.com/services/([^/]+)/([^/]+)/([^/?#]+)/?\Z")


def move_channels_to_urls() -> int:
    """Une seule fois par base : le salon d'un sortant hooks.slack.com
    (champ à part) passe dans son URL Apprise (slack://A/B/C/#salon). → le
    nombre de sortants convertis. Une URL produite qu'Apprise refuserait
    n'est pas écrite : le sortant reste en JSON, avec son salon. BLOQUANT."""
    def q(c):
        if not owned.once(c, "webhook-channels-into-apprise-urls"):
            return 0
        moved = 0
        for owner, wid, url, channel in c.execute(
                "SELECT owner, id, url, channel FROM webhooks_out WHERE channel != ''").fetchall():
            m = _SLACK_HOOK.match(url)
            if not m:
                continue
            new = f"slack://{m[1]}/{m[2]}/{m[3]}/{quote(channel, safe='')}"
            if target(new) is None:
                continue
            c.execute("UPDATE webhooks_out SET url = ?, channel = '' WHERE owner = ? AND id = ?",
                      (new, owner, wid))
            moved += 1
        return moved
    return _run(q)
