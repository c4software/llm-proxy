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
import time
from urllib.parse import urljoin, urlsplit

import httpx

from .. import config
from . import net, webcache
from .contract import (Anthropic, Call, Responses, Result, Source, Tool,
                       ToolError)
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

# La description renvoie le modèle à `web_search` : vrai seulement là où
# `web_search` lui est présenté aussi (`present` de spec) — un client
# Anthropic qui ne déclare que la lecture la reçoit sans cette phrase.
_SEARCH = "web_search"
_SEARCH_HINT = f"Use it to read a page found with {_SEARCH}. "

TEXT_TYPES = ("text/", "application/json", "application/xml",
              "application/xhtml+xml", "application/javascript",
              "application/rss+xml", "application/atom+xml")


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
        raise net.Blocked(f"hôte introuvable : {host}", "not_accessible")
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
           max_chars: int | None = None) -> Result:
    """`body` en octets : la page telle que téléchargée. En `str` : un texte
    déjà extrait par run() (PDF), rendu tel quel. `max_chars` : une taille
    de morceau plus PETITE que MAX_CHARS (voir run), jamais plus grande.
    Rend le texte du modèle et, dans `meta`, ce que son en-tête dit de la
    page : `url` (celle réellement lue, après redirections), `title`,
    `content_type`, `total` (caractères de la page) et `range` — la plage
    rendue, seulement si ce n'est pas la page entière."""
    ct = (content_type or "").split(";")[0].strip().lower()
    title = ""
    if isinstance(body, str):
        text = body
    else:
        if ct and not ct.startswith(TEXT_TYPES):
            raise ToolError("unsupported", (
                f"{url} is {ct}, which this tool cannot read "
                f"(text, HTML, JSON and PDF only)."))
        try:
            text = body.decode(encoding or "utf-8", "replace")
        except LookupError:  # charset annoncé inconnu de Python
            text = body.decode("utf-8", "replace")
        if "html" in ct or (not ct and "<html" in text[:2000].lower()):
            title, text = html_to_text(text)
    total = len(text)
    offset = min(max(offset, 0), total)
    shown = text[offset:offset + min(max_chars or MAX_CHARS, MAX_CHARS)]
    meta = {"url": url, "title": title, "content_type": ct, "total": total}
    head = [f"URL: {url}"]
    if title:
        head.append(f"Title: {title}")
    if ct:
        head.append(f"Content-Type: {ct}")
    end = offset + len(shown)
    if offset or end < total:
        meta["range"] = (offset, end)
        head.append(f"Characters: {offset}-{end} of {total}"
                    + (f" (truncated: pass offset={end} to continue)"
                       if end < total else ""))
    if isinstance(body, bytes) and len(body) >= MAX_BYTES:
        head.append(f"Note: only the first {MAX_BYTES} bytes were downloaded.")
    return Result("\n".join(head) + "\n\n---\n" + (shown or "(empty page)"),
                  meta=meta)


class WebFetch(Tool):
    name = NAME
    # Activée avec la recherche, et rendue par le même élément : une
    # recherche sans lecture ne rend que des extraits de 240 caractères.
    responses = Responses(
        kinds=("web_search", "web_search_preview", "web_search_2025_08_26"),
        item="web_search_call")
    anthropic = Anthropic(prefix="web_fetch", block="web_fetch_tool_result")

    @property
    def enabled(self) -> bool:
        return ENABLED

    def spec(self, present) -> dict:
        return {"type": "function", "function": {
            "name": NAME,
            "description": (
                "Fetch a web page by URL and return its content as text (HTML is "
                "converted to plain text; JSON and text are returned as is; the "
                "text of a PDF is extracted). "
                + (_SEARCH_HINT if _SEARCH in present else "")
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

    def summary(self, args: dict, result: Result | None = None) -> dict:
        """Une page longue est lue en plusieurs morceaux (`offset`) : sans
        rien pour les distinguer, le client montre trois fois « Opened
        <même URL> » et l'on croit à une boucle. La plage de caractères
        réellement rendue suit donc l'URL affichée — « <url> [20000,
        40000] » —, prise dans `meta` du résultat ; une page rendue d'un
        seul tenant garde son URL nue. Le modèle, lui, ne voit que ses
        propres arguments."""
        url = str(args.get("url") or "")
        span = result.meta.get("range") if url and result is not None else None
        if span:
            url = f"{url} [{span[0]}, {span[1]}]"
        return {"type": "open_page", "url": url}

    async def run(self, args: dict, call: Call, transport=None) -> Result:
        """`call.settings` — `allowed_domains`, `blocked_domains`,
        `max_chars` — ne vient jamais du modèle : ce sont les réglages que
        le CLIENT pose sur son outil serveur (surface Anthropic —
        `max_chars` y est tiré de `max_content_tokens`). Ses listes
        s'AJOUTENT à celles de la configuration, elles n'en lèvent rien."""
        asked = args.get("url")
        if not isinstance(asked, str) or not asked.strip():
            raise ToolError("invalid_input", "`url` is required.")
        url = asked.strip()
        if url.startswith("www."):
            url = "https://" + url
        offset = args.get("offset")
        offset = offset if isinstance(offset, int) \
            and not isinstance(offset, bool) else 0
        allowed_domains = call.settings.get("allowed_domains")
        blocked_domains = call.settings.get("blocked_domains")
        max_chars = call.settings.get("max_chars")

        def check(u: str) -> None:
            if any(not net.domain_match(u, allowed)
                   for allowed in (ALLOWED_DOMAINS, allowed_domains) if allowed) \
                    or net.domain_match(u, BLOCKED_DOMAINS) \
                    or net.domain_match(u, blocked_domains or ()):
                raise ToolError("not_allowed", (
                    f"{urlsplit(u).hostname or u} is not a "
                    f"domain this proxy is allowed to read."))

        def read(page) -> Result:
            """Le morceau demandé, et sa source : l'URL telle que le modèle
            l'a écrite — c'est elle qu'il citera dans sa réponse."""
            out = render(*page, offset, max_chars)
            return Result(out.text, sources=(Source(asked, asked),),
                          meta=out.meta)

        # Le cache web (webcache.py) : la page telle qu'elle a été
        # téléchargée, d'où chaque morceau (`offset`) est rendu sans la
        # redemander. Les listes de domaines passent AVANT ; le contrôle
        # d'adresse et celui de chaque redirection ont été faits au
        # téléchargement.
        key = ("fetch", url.split("#", 1)[0])
        check(url)
        hit = webcache.CACHE.get(key)
        if hit is not None:
            return read(hit)
        try:
            for _ in range(MAX_REDIRECTS + 1):
                # À chaque saut, comme le contrôle d'adresse : une
                # redirection ne sort pas des listes.
                check(url)
                r, body = await _get(url, transport)
                if r.status_code in (301, 302, 303, 307, 308) \
                        and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    continue
                break
            else:
                raise ToolError("not_accessible", "too many redirects.")
        except net.Blocked as exc:
            raise ToolError(exc.code, f"{exc}.")
        # InvalidURL n'est PAS une HTTPError : caractère de contrôle dans le
        # chemin, URL trop longue — y compris dans un Location de redirection.
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise ToolError("not_accessible", f"could not fetch {url} "
                                              f"({type(exc).__name__}).")
        if r.status_code >= 400:
            raise ToolError(
                "too_many_requests" if r.status_code == 429 else "not_accessible",
                f"{url} returned HTTP {r.status_code}.")
        content_type = r.headers.get("content-type", "")
        page = (url, content_type, body, r.charset_encoding)
        if is_pdf(content_type, body):
            if len(body) >= PDF_MAX_BYTES:
                raise ToolError("unsupported", (
                    f"{url} is a PDF larger than {PDF_MAX_BYTES} "
                    f"bytes, which this tool does not download."))
            # Dans un fil : l'extraction est du CPU, la boucle asyncio (les
            # autres requêtes du proxy) ne l'attend pas.
            text, why = await asyncio.to_thread(pdf_text, body)
            if why:
                raise ToolError("unsupported", f"{url} {why}.")
            # C'est le TEXTE qui est rendu et gardé en cache : chaque
            # morceau (`offset`) en sort sans refaire l'extraction.
            page = (url, PDF_TYPE, text, None)
        out = read(page)
        # Seule une page lue avec succès est gardée (un type non lisible
        # lève avant : rien à garder).
        if r.status_code == 200:
            webcache.CACHE.put(key, page, len(page[2]))
        return out


TOOL = WebFetch()
