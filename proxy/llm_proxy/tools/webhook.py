"""
`webhook` : le modèle poste un message sur un WEBHOOK SORTANT du compte
(storage/webhooks.py) — une URL Apprise (Slack, Mattermost, Discord,
Teams, Telegram, ntfy, e-mail… : https://github.com/caronc/apprise),
envoyée en Markdown que chaque service rend à sa façon. Une URL http(s)
qu'Apprise ne reconnaît pas reçoit du JSON compatible Slack
({"text": "<mrkdwn>", "channel"?}). Le compte les définit lui-même
(Configuration → Webhooks) ; le modèle ne choisit jamais une URL, seulement
le NOM d'un webhook de `call.client` EXPOSÉ à l'outil (case du sortant,
décochée par défaut). Il ne peut donc écrire que là où le compte l'a
permis. Les noms disponibles ne lui sont dits que sur erreur (nom inconnu,
ou plusieurs sortants sans nom donné).

Aussi ce qu'en a besoin la réception (ui/webhook_turn.py) : le format
Slack dans les deux sens, et l'envoi.

  post(owner, hook, text)  l'envoi sur un sortant du compte (Apprise, ou
                           JSON), l'issue notée sur lui → «» ou l'erreur
  send(url, text, extra)   POST {"text": mrkdwn, …extra} (net.post : les
                           adresses publiques seulement, sauf
                           [webhooks].allow_private) ; lève ToolError — le
                           sortant JSON, et le `response_url` d'une
                           commande slash
  to_mrkdwn(markdown)      le Markdown du modèle → le mrkdwn de Slack
  from_slack(payload)      le texte d'un message Slack reçu (text, blocks,
                           attachments), en Markdown

Sans liaison de protocole : présentable sur /v1/chat/completions (déclaré
`{"type": "webhook"}`, le chat de la page le propose), exécutable par
/v1/tools et le serveur MCP.
"""

import asyncio
import html
import json
import logging
import re
from urllib.parse import parse_qs, urlsplit

import apprise

from ..core import config
from ..storage import webhooks
from . import net
from .contract import Call, Result, Tool, ToolError, ToolName

ENABLED = config.flag("webhooks.enabled", False)
NAME = ToolName.WEBHOOK
ALLOW_PRIVATE = config.flag("webhooks.allow_private", False)
TIMEOUT = webhooks.TIMEOUT
# Slack coupe un `text` au-delà de 40 000 caractères.
MAX_TEXT = 39_000
USER_AGENT = webhooks.USER_AGENT


# ── le format Slack ─────────────────────────────────────────────────────

_FENCE = re.compile(r"(```.*?```|`[^`\n]+`)", re.S)


def to_mrkdwn(text: str) -> str:
    """Markdown → mrkdwn de Slack : **gras** → *gras*, ~~barré~~ → ~barré~,
    [texte](url) → <url|texte>, titres → lignes en gras, puces « - » → « • ».
    Le code (blocs et en ligne) passe tel quel ; &, < et > sont échappés
    hors des liens, comme Slack le demande."""
    out = []
    for i, part in enumerate(_FENCE.split(text or "")):
        if i % 2:                    # du code : intact
            out.append(re.sub(r"^```[\w+-]*\n", "```\n", part))
            continue
        part = part.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        part = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)", r"<\2|\1>", part)
        part = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r"<\2|\1>", part)
        part = re.sub(r"\*\*(.+?)\*\*|__(.+?)__",
                      lambda m: "*" + (m.group(1) or m.group(2)) + "*", part)
        part = re.sub(r"~~(.+?)~~", r"~\1~", part)
        part = re.sub(r"(?m)^#{1,6}\s+(.+?)\s*#*\s*$", r"*\1*", part)
        part = re.sub(r"(?m)^(\s*)[-*+]\s+", r"\1• ", part)
        out.append(part)
    text = "".join(out).strip()
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "\n…"


def _slack_inline(text: str) -> str:
    """mrkdwn reçu → Markdown : <url|texte> → [texte](url), <url> → url,
    <@U…> et <#C…|nom> lisibles, entités décodées."""
    def link(m):
        target, _, label = m.group(1).partition("|")
        if target.startswith(("@", "#", "!")):
            return label and (("#" if target[0] == "#" else "@") + label) or target
        return f"[{label}]({target})" if label else target
    return html.unescape(re.sub(r"<([^<>\s][^<>]*)>", link, text or ""))


def _list(value) -> list:
    """Une liste du corps reçu, ou rien : un tiers peut envoyer n'importe quoi."""
    return value if isinstance(value, list) else []


# Profondeur au plus des blocs imbriqués (rich_text en a trois ou quatre).
MAX_DEPTH = 8


def _block_text(block, depth: int = 0) -> list[str]:
    """Le texte d'un bloc Block Kit (section, header, context, rich_text…)."""
    if not isinstance(block, dict) or depth > MAX_DEPTH:
        return []
    out = []
    t = block.get("text")
    if isinstance(t, dict) and isinstance(t.get("text"), str):
        out.append(t["text"])
    elif isinstance(t, str) and block.get("type") in ("text", "mrkdwn", "plain_text"):
        out.append(t)
    for f in _list(block.get("fields")):
        if isinstance(f, dict) and isinstance(f.get("text"), str):
            out.append(f["text"])
    for e in _list(block.get("elements")):
        if isinstance(e, dict) and e.get("type") in ("text", "link"):
            out.append(str(e.get("text") or e.get("url") or ""))
        else:
            out.extend(_block_text(e, depth + 1))
    return [x for x in out if x]


def from_slack(payload: dict) -> str:
    """Le texte d'un message au format Slack : `text`, sinon celui de ses
    `blocks`, plus ses `attachments` (pretext, titre, texte, champs). En
    Markdown, prêt pour la conversation."""
    parts = []
    if isinstance(payload.get("text"), str) and payload["text"].strip():
        parts.append(payload["text"])
    else:
        for b in _list(payload.get("blocks")):
            parts.extend(_block_text(b))
    for a in _list(payload.get("attachments")):
        if not isinstance(a, dict):
            continue
        lines = [a.get("pretext"), a.get("title") and f"**{a['title']}**", a.get("text")]
        lines += [f"{f.get('title')}: {f.get('value')}" for f in _list(a.get("fields"))
                  if isinstance(f, dict)]
        if not any(lines):
            for b in _list(a.get("blocks")):
                lines.extend(_block_text(b))
        if not any(lines):
            lines = [a.get("fallback")]
        parts.append("\n".join(str(x) for x in lines if x))
    return _slack_inline("\n\n".join(p for p in parts if p)).strip()


async def send(url: str, text: str, extra: dict | None = None,
               allow_private: bool | None = None) -> None:
    """POST d'un message Slack sur `url`. Lève ToolError : adresse
    refusée, injoignable, ou réponse hors 2xx (son début dans le message ;
    une redirection n'est pas suivie, donc un échec). `allow_private` :
    None = [webhooks].allow_private."""
    body = json.dumps({"text": to_mrkdwn(text), **(extra or {})},
                      ensure_ascii=False).encode()
    status, got = await net.post(url, body, "application/json", timeout=TIMEOUT,
                                 user_agent=USER_AGENT,
                                 allow_private=ALLOW_PRIVATE if allow_private is None
                                 else allow_private, limit=2000)
    if 300 <= status < 400:
        # net.post ne suit pas de redirection : rien n'est arrivé.
        raise ToolError("not_accessible", f"the webhook answered HTTP {status}, a "
                        f"redirection, which is not followed: check its URL (https, "
                        f"trailing slash).")
    if status >= 400 or status < 200:
        detail = got.decode("utf-8", "replace").strip()[:200]
        raise ToolError("too_many_requests" if status == 429 else "not_accessible",
                        f"the webhook answered HTTP {status}"
                        + (f" ({detail})" if detail else "") + ".")


def _notify(url: str, text: str) -> str:
    """L'envoi par Apprise, en Markdown → «», ou ce qu'Apprise a dit de
    l'échec. Toute URL qu'Apprise sait servir est permise (docs/webhooks.md).
    Un texte trop long pour le service part en plusieurs messages, sauf si
    l'URL dit autrement (`overflow=`). BLOQUANT."""
    service = webhooks.target(url)
    if service is None:
        return "Apprise does not recognize this URL."
    if "overflow" not in parse_qs(urlsplit(url).query):
        service.overflow_mode = apprise.OverflowMode.SPLIT
    # Les délais de connexion et de lecture (cto=, rto= dans l'URL) bornés
    # par [webhooks].timeout.
    service.socket_connect_timeout = min(service.socket_connect_timeout, TIMEOUT)
    service.socket_read_timeout = min(service.socket_read_timeout, TIMEOUT)
    sender = apprise.Apprise(service, asset=webhooks.ASSET)
    said = []
    if sender.notify(body=text[:MAX_TEXT], body_format=apprise.NotifyFormat.MARKDOWN,
                     log_callback=lambda entry, plugin: said.append(entry.message),
                     log_level=logging.WARNING):
        return ""
    return (said[-1] if said else "the notification failed.")[:300]


async def post(owner: str, hook: dict, text: str, extra: dict | None = None) -> str:
    """Envoie sur le sortant `hook` du compte (`list_out`, `get_out`) et note l'issue (son dernier usage, son erreur). → «» si envoyé,
    sinon le message d'erreur. Une URL Apprise : par Apprise (`extra`, le fil
    Slack, n'y a pas cours). Sinon, en JSON, avec son salon et `extra`."""
    if not hook["legacy"]:
        error = await asyncio.to_thread(_notify, hook["url"], text)
    else:
        if hook.get("channel"):
            extra = {"channel": hook["channel"], **(extra or {})}
        try:
            await send(hook["url"], text, extra)
            error = ""
        except ToolError as exc:
            error = exc.message
    await asyncio.to_thread(webhooks.used_out, owner, hook["id"], error)
    return error


# ── l'outil ─────────────────────────────────────────────────────────────

class WebhookTool(Tool):
    name = NAME
    max_calls = 5

    @property
    def enabled(self) -> bool:
        return ENABLED

    def prompt(self, present) -> str:
        return (
            "Post a message to one of the user's outgoing webhooks (a Slack, "
            "Mattermost, Discord, Teams, Telegram, e-mail or other destination "
            "the user set up). Use it ONLY when "
            "the user asks to send, post, notify or share something there; "
            "never on your own initiative. `text` is the message, in "
            "Markdown, self-contained: the readers of the channel do not see "
            "this conversation. `webhook` is the name of the webhook; omit it "
            "when the user has only one. An unknown name returns the list of "
            "available names.")

    def parameters(self, present) -> dict:
        return {"type": "object", "properties": {
            "text": {"type": "string", "description": "The message to post, in Markdown."},
            "webhook": {"type": "string", "description": (
                "The name of the outgoing webhook; optional when the user has "
                "only one.")},
        }, "required": ["text"]}

    def summary(self, args: dict, result: Result | None = None) -> dict:
        return {"type": self.name, "webhook": str(args.get("webhook") or "")}

    async def run(self, args: dict, call: Call) -> Result:
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ToolError("invalid_input", "`text` is required.")
        # Seuls ceux que le compte a exposés à l'outil.
        hooks = [h for h in await asyncio.to_thread(webhooks.list_out, call.client)
                 if h["tool"]]
        if not hooks:
            raise ToolError("unavailable", (
                "the user has no outgoing webhook available to this tool; they can "
                "add or expose one in the dashboard, Configuration → Webhooks."))
        wanted = str(args.get("webhook") or "").strip()
        if wanted:
            hook = next((h for h in hooks if h["name"].casefold() == wanted.casefold()),
                        None)
        else:
            hook = hooks[0] if len(hooks) == 1 else None
        if hook is None:
            names = ", ".join(json.dumps(h["name"], ensure_ascii=False) for h in hooks)
            raise ToolError("invalid_input", (
                f"unknown webhook {wanted!r}; " if wanted else
                "several webhooks exist, name one; ") + f"available: {names}.")
        error = await post(call.client, hook, text)
        if error:
            raise ToolError("not_accessible", error)
        return Result(f"Message posted to the webhook {hook['name']!r}.",
                      meta={"webhook": hook["name"]})


TOOL = WebhookTool()
