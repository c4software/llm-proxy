"""
La PROVENANCE d'une URL : un outil qui lit une URL choisie par le modèle
(web_fetch, web_grep, ocr, transcribe) ne lit que celles d'un SITE déjà
apparu dans la conversation — dans un message de l'utilisateur, le prompt
système, une réponse précédente, ou le résultat (réussi) d'un outil, une
recherche web comprise. Une adresse que le modèle invente sur un site jamais
vu est refusée.

C'est la règle du web_fetch d'Anthropic (qui exige l'URL EXACTE), élargie au
SITE : le modèle peut construire une URL sur un site rencontré (une recherche
interne, une page voisine) et suivre les liens d'une page lue, mais pas
ouvrir un hôte sorti de sa mémoire — les URL de stockage qu'un modèle Qwen
appelle de mémoire, ou un domaine où il ferait fuiter la conversation.

  [tools.net]
  provenance = "site"      # "off" : pas de contrôle

Le contrôle n'a lieu que là où le proxy VOIT la conversation (les trois
surfaces, le chat de la page) : `call.hosts` en porte les hôtes connus ;
None (appel direct /v1/tools, serveur /mcp) = pas de contexte, pas de
contrôle. Les redirections ne sont pas jugées ici : c'est le site lu qui
les choisit (net.download les passe par ses autres garde-fous).
"""

import re
from urllib.parse import urlsplit

from ..core import config
from .contract import Call, ToolError

MODE = config.text("tools.net.provenance", "site").strip().lower()

# Une URL, et un nom de domaine écrit nu (« regarde frerestoque.fr »).
_URL = re.compile(r"https?://[^\s<>\"'`)\]}]+", re.IGNORECASE)
_DOMAIN = re.compile(r"(?<![\w@./-])((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24})(?![\w-])",
                     re.IGNORECASE)
# Les suffixes à deux niveaux les plus courants : le site est alors le
# troisième niveau (exemple.co.uk, ville.gouv.fr).
_SECOND = {"co", "com", "net", "org", "gov", "gouv", "ac", "edu", "asso", "ne", "or", "go"}
# Ce qui ressemble à un domaine sans en être un (noms de fichiers, versions).
_NOT_TLD = {"js", "py", "md", "txt", "json", "html", "htm", "css", "png", "jpg", "jpeg",
            "gif", "svg", "pdf", "csv", "xml", "yml", "yaml", "toml", "ts", "sh", "zip",
            "tar", "gz", "log", "ini", "cfg", "env", "lock", "rs", "go", "rb", "php", "java"}


def site(host: str) -> str:
    """Le SITE d'un hôte : son domaine enregistrable, approché (les deux
    derniers niveaux, trois sous un suffixe comme co.uk ou gouv.fr)."""
    host = (host or "").lower().strip(".")
    labels = host.split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def hosts_in_text(text: str) -> set[str]:
    """Les sites cités dans un texte : ceux de ses URL, et ses noms de
    domaine écrits nus."""
    out = set()
    if not text:
        return out
    for m in _URL.finditer(text):
        try:
            host = urlsplit(m.group(0)).hostname
        except ValueError:
            continue
        if host:
            out.add(site(host))
    for m in _DOMAIN.finditer(text):
        name = m.group(1).lower()
        if name.rsplit(".", 1)[-1] not in _NOT_TLD:
            out.add(site(name))
    return out


def _texts(content) -> list[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [p.get("text", "") for p in content if isinstance(p, dict) and isinstance(p.get("text"), str)]
    return []


def hosts_in_messages(messages) -> set[str]:
    """Les sites connus d'une conversation chat/completions : tout le texte
    des messages, SAUF les arguments des appels d'outils (ce que le modèle
    demande n'est pas une source) et les résultats en échec (une URL
    inventée qui a échoué n'en devient pas connue)."""
    out = set()
    for msg in messages if isinstance(messages, list) else ():
        if not isinstance(msg, dict):
            continue
        texts = _texts(msg.get("content"))
        if msg.get("role") == "tool" and any(t.lstrip().startswith("Error:") for t in texts):
            continue
        for t in texts:
            out |= hosts_in_text(t)
    return out


def check(url: str, call: Call) -> None:
    """Lève ToolError `not_allowed` si le site de `url` n'est pas connu de
    la conversation (`call.hosts`) ; rien sans contexte ou règle coupée."""
    if MODE == "off" or call.hosts is None:
        return
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return                      # l'outil dira lui-même l'URL invalide
    if not host or site(host) in call.hosts:
        return
    raise ToolError("not_allowed", (
        f"{host} has not appeared in this conversation (not in the user's "
        f"messages, nor in a tool or search result), so this proxy does not "
        f"open it: do not guess URLs. Find the page with a web search, or ask "
        f"the user for the address."))
