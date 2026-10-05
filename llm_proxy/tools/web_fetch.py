"""
`web_fetch` : lire une page web, pour que le modèle puisse ouvrir un
résultat de `web_search` (c'est l'action `open_page` de la recherche web
d'OpenAI ; chez oh-my-pi, la lecture d'URL de l'outil `read`, sans ses
extracteurs par site).

La cible est choisie par le modèle : elle passe par net.public_target —
adresses publiques seulement, connexion vers l'adresse vérifiée — à
CHAQUE saut de redirection. Le corps est lu jusqu'à `max_bytes`, rendu
en texte (HTML → texte, JSON et texte tels quels) et coupé à `max_chars`.
"""

import re
from urllib.parse import urljoin, urlsplit

import httpx

from .. import config
from . import net
from .html_text import html_to_text

ENABLED = config.flag("tools.web_fetch.enabled", False)
TIMEOUT = config.num("tools.web_fetch.timeout", 20)
MAX_BYTES = config.integer("tools.web_fetch.max_bytes", 2_000_000)
MAX_CHARS = config.integer("tools.web_fetch.max_chars", 20_000)
# Lire aussi les adresses privées : à n'ouvrir que sur un proxy dont tous
# les clients sont de confiance, et jamais derrière un modèle qui lit le web.
ALLOW_PRIVATE = config.flag("tools.web_fetch.allow_private", False)
# Listes de domaines, fixées par celui qui déploie (pas par le modèle) :
# `allowed_domains` non vide = SEULS ces domaines sont lus ;
# `blocked_domains` = jamais lus. Mêmes règles que net.domain_match
# (sous-domaines couverts, chemin facultatif). C'est la seule parade à la
# fuite par l'URL : une page lue qui pousse le modèle à ouvrir
# https://ailleurs/?d=<contenu de la conversation>.
ALLOWED_DOMAINS = config.strings("tools.web_fetch.allowed_domains")
BLOCKED_DOMAINS = config.strings("tools.web_fetch.blocked_domains")
MAX_REDIRECTS = 5
USER_AGENT = "llm-proxy web_fetch (+https://github.com/c4software/llm-proxy)"

NAME = "web_fetch"
ITEM_TYPE = "web_search_call"
# Activée avec la recherche : une recherche sans lecture ne rend que des
# extraits de 240 caractères.
KINDS = ("web_search", "web_search_preview", "web_search_2025_08_26")

DEFINITION = {"type": "function", "function": {
    "name": NAME,
    "description": (
        "Fetch a web page by URL and return its content as text (HTML is "
        "converted to plain text; JSON and text are returned as is). Use it "
        "to read a page found with web_search. Long pages are truncated: "
        "pass `offset` to continue from a given character position."),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string",
                    "description": "The http(s) URL to fetch."},
            "offset": {"type": "integer",
                       "description": "Character position to start from, "
                                      "to continue a truncated page."},
        },
        "required": ["url"],
    },
}}

TEXT_TYPES = ("text/", "application/json", "application/xml",
              "application/xhtml+xml", "application/javascript",
              "application/rss+xml", "application/atom+xml")


# L'en-tête qu'écrit render() pour une page lue par morceaux.
_RANGE = re.compile(r"^Characters: (\d+)-(\d+) of \d+", re.M)


def action(args: dict, result=None) -> dict:
    """Ce que le client affiche de l'appel. Une page longue est lue en
    plusieurs morceaux (`offset`) : sans rien pour les distinguer, le client
    montre trois fois « Opened <même URL> » et l'on croit à une boucle. La
    plage de caractères réellement rendue suit donc l'URL affichée —
    « <url> [20000, 40000] » —, lue dans l'en-tête du résultat ; une page
    rendue d'un seul tenant garde son URL nue. Le modèle, lui, ne voit que
    ses propres arguments."""
    url = str(args.get("url") or "")
    span = _RANGE.search(str(result)[:600]) if url and result is not None else None
    if span:
        url = f"{url} [{span.group(1)}, {span.group(2)}]"
    return {"type": "open_page", "url": url}


def item(args: dict, result) -> dict:
    """Les champs de l'élément terminé : l'action, avec la plage lue."""
    return {"status": "completed", "action": action(args, result)}


async def _get(url: str, transport) -> tuple[httpx.Response, bytes]:
    """Un saut : résolution contrôlée, connexion vers l'adresse vérifiée
    (Host et SNI portent le nom), corps lu jusqu'à MAX_BYTES."""
    scheme, ip, port = await net.public_target(url, ALLOW_PRIVATE)
    parts = urlsplit(url)
    host = parts.hostname or ""
    # Le nom tel qu'il part sur le fil : en ASCII (un nom accentué passe
    # en punycode, comme getaddrinfo l'a résolu — un en-tête non ASCII
    # ferait lever httpx), une IPv6 littérale entre crochets dans Host.
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise net.Blocked(f"hôte introuvable : {host}")
    host_header = f"[{host}]" if ":" in host else host
    literal = f"[{ip}]" if ":" in ip else ip
    target = f"{scheme}://{literal}:{port}{parts.path or '/'}"
    if parts.query:
        target += "?" + parts.query
    default_port = 443 if scheme == "https" else 80
    headers = {
        "Host": host_header if port == default_port
                else f"{host_header}:{port}",
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/json,"
                  "text/plain;q=0.9,*/*;q=0.5",
        # Pas de compression : MAX_BYTES se compte après décompression,
        # bloc par bloc — un seul bloc gzip peut en rendre mille fois plus.
        "Accept-Encoding": "identity",
    }
    # trust_env=False : sans lui httpx lirait HTTP_PROXY / ALL_PROXY, et
    # « la connexion part vers l'adresse vérifiée » deviendrait « un proxy
    # s'y connecte pour nous ».
    async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport,
                                 follow_redirects=False,
                                 trust_env=False) as c:
        req = c.build_request("GET", target, headers=headers,
                              extensions={"sni_hostname": host})
        r = await c.send(req, stream=True)
        body = bytearray()
        try:
            async for chunk in r.aiter_bytes():
                body += chunk
                if len(body) >= MAX_BYTES:
                    break
        finally:
            await r.aclose()
    return r, bytes(body[:MAX_BYTES])


def render(url: str, content_type: str, body: bytes, encoding: str | None,
           offset: int = 0) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct and not ct.startswith(TEXT_TYPES):
        return (f"Error: {url} is {ct}, which this tool cannot read "
                f"(text, HTML and JSON only).")
    try:
        text = body.decode(encoding or "utf-8", "replace")
    except LookupError:  # charset annoncé inconnu de Python
        text = body.decode("utf-8", "replace")
    title = ""
    if "html" in ct or (not ct and "<html" in text[:2000].lower()):
        title, text = html_to_text(text)
    total = len(text)
    offset = min(max(offset, 0), total)
    shown = text[offset:offset + MAX_CHARS]
    head = [f"URL: {url}"]
    if title:
        head.append(f"Title: {title}")
    if ct:
        head.append(f"Content-Type: {ct}")
    end = offset + len(shown)
    if offset or end < total:
        head.append(f"Characters: {offset}-{end} of {total}"
                    + (f" (truncated: pass offset={end} to continue)"
                       if end < total else ""))
    if len(body) >= MAX_BYTES:
        head.append(f"Note: only the first {MAX_BYTES} bytes were downloaded.")
    return "\n".join(head) + "\n\n---\n" + (shown or "(empty page)")


async def run(args: dict, transport=None) -> str:
    url = args.get("url")
    if not isinstance(url, str) or not url.strip():
        return "Error: `url` is required."
    url = url.strip()
    if url.startswith("www."):
        url = "https://" + url
    offset = args.get("offset")
    offset = offset if isinstance(offset, int) and not isinstance(offset, bool) else 0
    try:
        for _ in range(MAX_REDIRECTS + 1):
            # À chaque saut, comme le contrôle d'adresse : une redirection
            # ne sort pas des listes.
            if (ALLOWED_DOMAINS and not net.domain_match(url, ALLOWED_DOMAINS)) \
                    or net.domain_match(url, BLOCKED_DOMAINS):
                return (f"Error: {urlsplit(url).hostname or url} is not a "
                        f"domain this proxy is allowed to read.")
            r, body = await _get(url, transport)
            if r.status_code in (301, 302, 303, 307, 308) \
                    and r.headers.get("location"):
                url = urljoin(url, r.headers["location"])
                continue
            break
        else:
            return "Error: too many redirects."
    except net.Blocked as exc:
        return f"Error: {exc}."
    # InvalidURL n'est PAS une HTTPError : caractère de contrôle dans le
    # chemin, URL trop longue — y compris dans un Location de redirection.
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        return f"Error: could not fetch {url} ({type(exc).__name__})."
    if r.status_code >= 400:
        return f"Error: {url} returned HTTP {r.status_code}."
    return render(url, r.headers.get("content-type", ""), body,
                  r.charset_encoding, offset)
