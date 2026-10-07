"""
`web_fetch` : lire une page web, pour que le modèle puisse ouvrir un
résultat de `web_search` (c'est l'action `open_page` de la recherche web
d'OpenAI ; chez oh-my-pi, la lecture d'URL de l'outil `read`, sans ses
extracteurs par site).

La cible est choisie par le modèle : elle passe par net.public_target —
adresses publiques seulement, connexion vers l'adresse vérifiée — à
CHAQUE saut de redirection. Le corps est lu jusqu'à `max_bytes`, rendu
en texte (HTML → texte, JSON et texte tels quels) et coupé à `max_chars`.

Un PDF est lu aussi : téléchargé jusqu'à `pdf_max_bytes`, son texte est
extrait par pypdf, hors de la boucle asyncio, puis rendu comme celui d'une
page — mêmes morceaux, même cache. Pas d'OCR : un scan n'a pas de texte.
"""

import asyncio
import io
import logging
import re
import time
from urllib.parse import urljoin, urlsplit

import httpx

from .. import config
from . import net, webcache
from .html_text import html_to_text

ENABLED = config.flag("tools.web_fetch.enabled", False)
TIMEOUT = config.num("tools.web_fetch.timeout", 20)
MAX_BYTES = config.integer("tools.web_fetch.max_bytes", 2_000_000)
MAX_CHARS = config.integer("tools.web_fetch.max_chars", 20_000)
# Un PDF a sa borne de téléchargement : 2 Mo suffisent à une page HTML,
# pas à un article ou à une notice (souvent 1 à 10 Mo, figures comprises),
# et un PDF coupé ne se lit pas — sa table des objets est à la fin.
PDF_MAX_BYTES = config.integer("tools.web_fetch.pdf_max_bytes", 20_000_000)
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
# Pages d'un PDF dont le texte est extrait (du CPU, quelques dizaines de
# millisecondes par page) ; l'extraction s'arrête aussi passé TIMEOUT.
PDF_MAX_PAGES = 500
PDF_TYPE = "application/pdf"
USER_AGENT = "llm-proxy web_fetch (+https://github.com/c4software/llm-proxy)"

NAME = "web_fetch"
ITEM_TYPE = "web_search_call"
# Activée avec la recherche : une recherche sans lecture ne rend que des
# extraits de 240 caractères.
KINDS = ("web_search", "web_search_preview", "web_search_2025_08_26")

# La description renvoie le modèle à `web_search` : vrai seulement là où
# `web_search` lui est présenté aussi. La surface Anthropic ne présente
# que les outils serveur que son client déclare — pour celui qui ne
# déclare que la lecture, elle prend DEFINITION_ALONE, sans cette phrase
# (le pendant de web_search.definition(fetch=False) ; pas de fonction
# `definition` ici : les autres surfaces l'appelleraient avec `fetch`).
_SEARCH_HINT = "Use it to read a page found with web_search. "


def _definition(search: bool = True) -> dict:
    return {"type": "function", "function": {
        "name": NAME,
        "description": (
            "Fetch a web page by URL and return its content as text (HTML is "
            "converted to plain text; JSON and text are returned as is; the "
            "text of a PDF is extracted). "
            + (_SEARCH_HINT if search else "")
            + "Long pages are truncated: "
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


DEFINITION = _definition()
DEFINITION_ALONE = _definition(search=False)

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


# ── pour la surface Anthropic (outil serveur `web_fetch_…`) ──
# Son client reçoit le TEXTE rendu au modèle, entier, dans un bloc
# `web_fetch_result`, et le renvoie tel quel au tour suivant : rien n'est
# à relire dans l'autre sens (pas de `parse` comme pour web_search). Il
# ne reste à tirer de ce texte que ce que le bloc dit À CÔTÉ de lui.

def page(result: str) -> dict:
    """Ce que l'en-tête écrit par render() dit de la page : `url` (celle
    réellement lue, après redirections) et `title` — «» s'il manque."""
    out = {"url": "", "title": ""}
    for line in result.split("\n\n---\n", 1)[0].split("\n"):
        key, _, value = line.partition(": ")
        if key in ("URL", "Title") and not out[key.lower()]:
            out[key.lower()] = value
    return out


# Fragments des textes d'erreur de run() (et de net.Blocked) → `error_code`
# de l'outil serveur d'Anthropic, dans l'ordre où ils sont cherchés. Le
# modèle lit le texte ; le client Anthropic n'en reçoit que ce code.
_ERROR_CODES = (
    ("is not a domain this proxy is allowed to read", "url_not_allowed"),
    ("adresse privée ou locale", "url_not_allowed"),
    ("URL invalide", "invalid_tool_input"),
    ("seules les URL http(s) sont lues", "invalid_tool_input"),
    ("which this tool cannot read", "unsupported_content_type"),
    ("PDF", "unsupported_content_type"),
    ("returned HTTP 429.", "too_many_requests"),
    ("returned HTTP ", "url_not_accessible"),
    ("hôte introuvable", "url_not_accessible"),
    ("too many redirects", "url_not_accessible"),
    ("could not fetch ", "url_not_accessible"),
)


def error_code(result: str) -> str:
    """Un texte «Error: …» rendu par cet outil → le code d'erreur de
    l'outil serveur web fetch d'Anthropic. `unavailable` pour ce qui n'est
    pas dans la table : délai, exception (tools.Hosted.run), texte
    inconnu. L'URL, que le modèle choisit et que le texte cite, est
    retirée avant la recherche : elle ne décide pas du code."""
    for word in result.split():
        if "://" in word:
            result = result.replace(word.rstrip("."), "")
    return next((code for mark, code in _ERROR_CODES if mark in result),
                "unavailable")


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
    limit = MAX_BYTES
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
                # Connu aux premiers octets : un PDF a sa propre borne.
                if is_pdf(r.headers.get("content-type", ""), body):
                    limit = PDF_MAX_BYTES
                if len(body) >= limit:
                    break
        finally:
            await r.aclose()
    return r, bytes(body[:limit])


def is_pdf(content_type: str, body: bytes) -> bool:
    """Un PDF : annoncé comme tel, ou reconnu à ses premiers octets — bien
    des serveurs le servent en `application/octet-stream`, voire en texte."""
    return (content_type or "").split(";")[0].strip().lower() == PDF_TYPE \
        or body.startswith(b"%PDF-")


def pdf_text(body: bytes) -> tuple[str, str]:
    """(texte, «»), ou («», pourquoi il n'y en a pas : la suite d'une phrase
    « Error: <url> … »). Ne lève pas : un PDF tordu fait lever n'importe
    quoi à pypdf. BLOQUANT (du CPU) : à appeler dans un fil, voir run().
    Borné en pages et en durée — passé le délai de l'outil, wait_for
    abandonne l'attente mais n'arrête pas un fil : c'est ici qu'il s'arrête,
    entre deux pages. Un texte incomplet le dit, à la fin."""
    try:
        # Importé ici : le proxy démarre sans pypdf, seule la lecture d'un
        # PDF le demande.
        from pypdf import PdfReader
    except ImportError:
        return "", ("is a PDF, which this proxy cannot read (pypdf is not "
                    "installed)")
    # pypdf signale chaque défaut d'un fichier par un avertissement.
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    deadline = time.monotonic() + TIMEOUT
    try:
        reader = PdfReader(io.BytesIO(body))
        if reader.is_encrypted:
            # Souvent chiffré SANS mot de passe (seules l'impression ou la
            # copie sont restreintes) : celui-là s'ouvre. Un algorithme que
            # pypdf ne déchiffre pas seul (AES) lève : même réponse.
            try:
                opened = reader.decrypt("")
            except Exception:
                opened = 0
            if not opened:
                return "", ("is an encrypted PDF (a password is required), "
                            "which this tool cannot read")
        total = len(reader.pages)
    except Exception:
        return "", "is not a readable PDF (damaged or truncated file)"
    parts, read = [], 0
    for n in range(min(total, PDF_MAX_PAGES)):
        if n and time.monotonic() > deadline:
            break
        try:
            text = (reader.pages[n].extract_text() or "").strip()
        except Exception:
            text = ""  # page illisible : les autres restent lues
        read += 1
        if text:
            parts.append(text)
    if not parts:
        return "", (f"is a PDF with no extractable text ({read} of {total} "
                    f"pages read, probably scanned images): this tool does "
                    f"no OCR")
    if read < total:
        parts.append(f"[only the first {read} of {total} pages were extracted]")
    return "\n\n".join(parts), ""


def render(url: str, content_type: str, body: bytes | str,
           encoding: str | None, offset: int = 0,
           max_chars: int | None = None) -> str:
    """`body` en octets : la page telle que téléchargée. En `str` : un texte
    déjà extrait par run() (PDF), rendu tel quel. `max_chars` : une taille
    de morceau plus PETITE que MAX_CHARS (voir run), jamais plus grande."""
    ct = (content_type or "").split(";")[0].strip().lower()
    title = ""
    if isinstance(body, str):
        text = body
    else:
        if ct and not ct.startswith(TEXT_TYPES):
            return (f"Error: {url} is {ct}, which this tool cannot read "
                    f"(text, HTML, JSON and PDF only).")
        try:
            text = body.decode(encoding or "utf-8", "replace")
        except LookupError:  # charset annoncé inconnu de Python
            text = body.decode("utf-8", "replace")
        if "html" in ct or (not ct and "<html" in text[:2000].lower()):
            title, text = html_to_text(text)
    total = len(text)
    offset = min(max(offset, 0), total)
    shown = text[offset:offset + min(max_chars or MAX_CHARS, MAX_CHARS)]
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
    if isinstance(body, bytes) and len(body) >= MAX_BYTES:
        head.append(f"Note: only the first {MAX_BYTES} bytes were downloaded.")
    return "\n".join(head) + "\n\n---\n" + (shown or "(empty page)")


async def run(args: dict, transport=None, allowed_domains=None,
              blocked_domains=None, max_chars=None) -> str:
    """`allowed_domains` / `blocked_domains` / `max_chars` ne viennent
    jamais du modèle : ce sont les réglages que le CLIENT pose sur son
    outil serveur (surface Anthropic — `max_chars` y est tiré de
    `max_content_tokens`), passés par tools.Hosted.run. Ses listes
    s'AJOUTENT à celles de la configuration, elles n'en lèvent rien."""
    url = args.get("url")
    if not isinstance(url, str) or not url.strip():
        return "Error: `url` is required."
    url = url.strip()
    if url.startswith("www."):
        url = "https://" + url
    offset = args.get("offset")
    offset = offset if isinstance(offset, int) and not isinstance(offset, bool) else 0

    def refused(u: str) -> str | None:
        if any(not net.domain_match(u, allowed)
               for allowed in (ALLOWED_DOMAINS, allowed_domains) if allowed) \
                or net.domain_match(u, BLOCKED_DOMAINS) \
                or net.domain_match(u, blocked_domains or ()):
            return (f"Error: {urlsplit(u).hostname or u} is not a "
                    f"domain this proxy is allowed to read.")
        return None

    # Le cache web (webcache.py) : la page telle qu'elle a été téléchargée,
    # d'où chaque morceau (`offset`) est rendu sans la redemander. Les
    # listes de domaines passent AVANT ; le contrôle d'adresse et celui de
    # chaque redirection ont été faits au téléchargement.
    key = ("fetch", url.split("#", 1)[0])
    if (no := refused(url)) is not None:
        return no
    hit = webcache.CACHE.get(key)
    if hit is not None:
        return render(*hit, offset, max_chars)
    try:
        for _ in range(MAX_REDIRECTS + 1):
            # À chaque saut, comme le contrôle d'adresse : une redirection
            # ne sort pas des listes.
            if (no := refused(url)) is not None:
                return no
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
    content_type = r.headers.get("content-type", "")
    page = (url, content_type, body, r.charset_encoding)
    if is_pdf(content_type, body):
        if len(body) >= PDF_MAX_BYTES:
            return (f"Error: {url} is a PDF larger than {PDF_MAX_BYTES} "
                    f"bytes, which this tool does not download.")
        # Dans un fil : l'extraction est du CPU, la boucle asyncio (les
        # autres requêtes du proxy) ne l'attend pas.
        text, why = await asyncio.to_thread(pdf_text, body)
        if why:
            return f"Error: {url} {why}."
        # C'est le TEXTE qui est rendu et gardé en cache : chaque morceau
        # (`offset`) en sort sans refaire l'extraction.
        page = (url, PDF_TYPE, text, None)
    out = render(*page, offset, max_chars)
    # Seule une page lue avec succès est gardée (un type non lisible rend
    # une erreur : rien à garder).
    if r.status_code == 200 and not out.startswith("Error:"):
        webcache.CACHE.put(key, page, len(page[2]))
    return out
