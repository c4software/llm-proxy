"""
`web_fetch` : lire une page web, pour que le modèle puisse ouvrir un
résultat de `web_search` (l'action `open_page` de la recherche web
d'OpenAI).

La cible est choisie par le modèle : elle passe par le téléchargement
gardé (net.download). Le corps est lu jusqu'à `max_bytes`, rendu en texte (HTML →
texte, JSON et texte tels quels) et coupé à `max_chars`.

Un PDF est lu aussi : téléchargé jusqu'à `pdf_max_bytes`, son texte est
extrait par pypdf, hors de la boucle asyncio, puis rendu comme celui d'une
page — mêmes morceaux, même cache. Pas d'OCR : un scan n'a pas de texte.

La lecture (load) et le texte d'une page (page_text) servent aussi à
`web_grep`, qui cherche dans la page au lieu de la rendre : mêmes
garde-fous, même cache.

RENDU (`render: true`, si [tools.web_fetch].render_url est réglé) : la page
est ouverte par un vrai navigateur (render.py, browserless), pour celles qui
ne montrent leur contenu qu'après leur JavaScript ; son HTML rendu suit le
même chemin. `screenshot: true` y ajoute une capture, rendue au client en
fichier (le modèle ne la voit pas).
"""

import asyncio
import io
import logging
import time

from .. import config
from . import net, render as renderer, webcache
from .contract import (Anthropic, Artifact, Call, Responses, Result, Source,
                       Tool, ToolError, ToolName)
from .html_text import html_to_text

ENABLED = config.flag("tools.web_fetch.enabled", False)
TIMEOUT = config.num("tools.web_fetch.timeout", 20)
MAX_BYTES = config.integer("tools.web_fetch.max_bytes", 2_000_000)
MAX_CHARS = config.integer("tools.web_fetch.max_chars", 20_000)
# Un PDF a sa borne de téléchargement : 2 Mo suffisent à une page HTML,
# pas à un article ou à une notice (souvent 1 à 10 Mo, figures comprises),
# et un PDF coupé ne se lit pas — sa table des objets est à la fin.
PDF_MAX_BYTES = config.integer("tools.web_fetch.pdf_max_bytes", 20_000_000)
# Pages d'un PDF dont le texte est extrait (du CPU, quelques dizaines de
# millisecondes par page) ; l'extraction s'arrête aussi passé TIMEOUT.
PDF_MAX_PAGES = 500
PDF_TYPE = "application/pdf"
USER_AGENT = "llm-proxy web_fetch (+https://github.com/c4software/llm-proxy)"
ACCEPT = ("text/html,application/xhtml+xml,application/json,"
          "text/plain;q=0.9,*/*;q=0.5")

NAME = ToolName.WEB_FETCH

# La description renvoie le modèle à `web_search` : seulement là où
# `web_search` lui est présenté aussi (`present` de prompt).
_SEARCH = ToolName.WEB_SEARCH
_SEARCH_HINT = f"Use it to read a page found with {_SEARCH}. "

# Et à `ocr`, de même : pour ce que cet outil-ci ne lit pas.
_OCR = ToolName.OCR
_OCR_HINT = (f"For an image, or a PDF with no extractable text (a scan), "
             f"use {_OCR} with the same URL. ")

TEXT_TYPES = ("text/", "application/json", "application/xml",
              "application/xhtml+xml", "application/javascript",
              "application/rss+xml", "application/atom+xml")


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
        # Importé ici : le proxy démarre sans pypdf.
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


def readable(url: str, content_type: str) -> str:
    """Le type de la page, normalisé ; lève ToolError s'il n'est pas du
    texte (un PDF arrive ici déjà converti)."""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct and ct != PDF_TYPE and not ct.startswith(TEXT_TYPES):
        raise ToolError("unsupported", (
            f"{url} is {ct}, which this tool cannot read "
            f"(text, HTML, JSON and PDF only)."))
    return ct


def page_text(url: str, content_type: str, body: bytes | str,
              encoding: str | None) -> tuple[str, str, str]:
    """La page lue → (type, titre, texte) : HTML converti, JSON et texte tels
    quels, un `body` déjà en `str` (le texte d'un PDF) tel quel. Lève
    ToolError pour un type qui n'est pas du texte."""
    ct = readable(url, content_type)
    if isinstance(body, str):
        return ct, "", body
    try:
        text = body.decode(encoding or "utf-8", "replace")
    except LookupError:  # charset annoncé inconnu de Python
        text = body.decode("utf-8", "replace")
    if "html" in ct or (not ct and "<html" in text[:2000].lower()):
        title, text = html_to_text(text)
        return ct, title, text
    return ct, "", text


def render(url: str, content_type: str, body: bytes | str,
           encoding: str | None, offset: int = 0,
           max_chars: int | None = None) -> Result:
    """`body` en octets : la page telle que téléchargée ; en `str` : le
    texte déjà extrait d'un PDF. `max_chars` : une taille de morceau plus
    PETITE que MAX_CHARS, jamais plus grande. `meta` reprend l'en-tête :
    `url` (réellement lue), `title`, `content_type`, `total` et `range`
    (seulement si ce n'est pas la page entière)."""
    ct, title, text = page_text(url, content_type, body, encoding)
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
    family = "web"
    # Activée avec la recherche, et rendue par le même élément : une
    # recherche sans lecture ne rend que des extraits de 240 caractères.
    responses = Responses(
        kinds=("web_search", "web_search_preview", "web_search_2025_08_26"),
        item="web_search_call")
    anthropic = Anthropic(prefix="web_fetch", block="web_fetch_tool_result")

    @property
    def enabled(self) -> bool:
        return ENABLED

    def prompt(self, present) -> str:
        return (
            "Fetch a web page by URL and return its content as text (HTML is "
            "converted to plain text; JSON and text are returned as is; the "
            "text of a PDF is extracted). "
            + (_SEARCH_HINT if _SEARCH in present else "")
            + (_OCR_HINT if _OCR in present else "")
            + "Long pages are truncated: "
            "pass `offset` to continue from a given character position."
            + (_RENDER_HINT if renderer.available() else ""))

    def parameters(self, present) -> dict:
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string",
                        "description": "The http(s) URL to fetch."},
                "offset": {"type": "integer",
                           "description": "Character position to start from, "
                                          "to continue a truncated page."},
                **(RENDER_PARAMS if renderer.available() else {}),
            },
            "required": ["url"],
        }

    def summary(self, args: dict, result: Result | None = None) -> dict:
        """Une page longue est lue en plusieurs morceaux (`offset`) : sans
        rien pour les distinguer, le client montrerait trois fois « Opened
        <même URL> ». La plage réellement rendue suit donc l'URL affichée —
        « <url> [20000, 40000] » ; une page d'un seul tenant garde son URL
        nue."""
        url = str(args.get("url") or "")
        span = result.meta.get("range") if url and result is not None else None
        if span:
            url = f"{url} [{span[0]}, {span[1]}]"
        return {"type": "open_page", "url": url}

    async def run(self, args: dict, call: Call, transport=None) -> Result:
        """`call.settings` — `allowed_domains`, `blocked_domains`,
        `max_chars` — ne vient jamais du modèle : ce sont les réglages que
        le CLIENT pose sur son outil serveur. Ses listes s'AJOUTENT à celles
        de la configuration (net.check)."""
        asked = args.get("url")
        url = target(asked)
        offset = args.get("offset")
        offset = offset if isinstance(offset, int) \
            and not isinstance(offset, bool) else 0
        max_chars = call.settings.get("max_chars")

        rendered = args.get("render") is True
        shot = rendered and args.get("screenshot") is True
        if args.get("screenshot") is True and not rendered:
            raise ToolError("invalid_input", "`screenshot` needs `render: true`.")
        files = ()
        if shot:
            # Une capture : un rendu à part, non gardé en cache.
            page, image = await load_rendered(url, call.settings, screenshot=True)
            if image:
                files = (Artifact("capture.jpg", "image/jpeg", image),)
        else:
            page = await load(url, call.settings, transport, rendered=rendered)
        out = render(*page, offset, max_chars)
        text = out.text
        if files:
            text += "\n\n[A screenshot of the rendered page (capture.jpg) was given to the user.]"
        # La source : l'URL telle que le modèle l'a écrite — c'est elle
        # qu'il citera dans sa réponse.
        return Result(text, sources=(Source(asked, asked),), meta=out.meta, files=files)


def target(asked) -> str:
    """L'URL écrite par le modèle, nettoyée. Lève ToolError si elle manque."""
    if not isinstance(asked, str) or not asked.strip():
        raise ToolError("invalid_input", "`url` is required.")
    url = asked.strip()
    return "https://" + url if url.startswith("www.") else url


_RENDER_HINT = (" If a page comes back empty or incomplete because it needs "
                "JavaScript (a single-page app, content loaded by scripts), "
                "fetch it again with `render: true`: it is opened in a real "
                "browser (slower, use it only then).")
RENDER_PARAMS = {
    "render": {"type": "boolean",
               "description": "Open the page in a real browser and read it "
                              "once its JavaScript has run. Only for pages "
                              "that come back empty or incomplete."},
    "screenshot": {"type": "boolean",
                   "description": "With render: also give the user a "
                                  "screenshot of the page (you do not see it)."},
}


async def load_rendered(url: str, settings, *, screenshot: bool = False) -> tuple:
    """La page à `url`, RENDUE par un navigateur (render.py) → (page,
    capture) : la page au format de load(), la capture en octets ou None.
    Sans capture, la page est gardée en cache (clé à part de la page
    téléchargée)."""
    key = ("render", url.split("#", 1)[0])
    net.check(url, settings)
    if not screenshot and (hit := webcache.CACHE.get(key)) is not None:
        return hit, None
    r = await renderer.render(url, settings, screenshot=screenshot)
    page = (r.url, "text/html; charset=utf-8", r.html.encode("utf-8"), "utf-8")
    webcache.CACHE.put(key, page, len(page[2]))
    return page, r.image


async def load(url: str, settings, transport=None, rendered: bool = False) -> tuple:
    """La page à `url` (passée par target()) → (url lue, type, corps,
    encodage), depuis le cache web ou téléchargée sous les garde-fous ;
    le corps d'un PDF est déjà son TEXTE. Lève ToolError. `settings` :
    les réglages du client (listes de domaines), voir run. `rendered` :
    ouverte par un navigateur (load_rendered)."""
    if rendered:
        return (await load_rendered(url, settings))[0]
    # Le cache web : chaque morceau (`offset`) est rendu sans redemander la
    # page. Les listes de domaines passent AVANT ; le contrôle d'adresse a
    # été fait au téléchargement.
    key = ("fetch", url.split("#", 1)[0])
    net.check(url, settings)
    hit = webcache.CACHE.get(key)
    if hit is not None:
        return hit

    def limit(r, body: bytes) -> int:
        # Connu aux premiers octets : un PDF a sa propre borne.
        return PDF_MAX_BYTES if is_pdf(
            r.headers.get("content-type", ""), body) else MAX_BYTES

    url, r, body = await net.download(
        url, settings, timeout=TIMEOUT, limit=limit,
        user_agent=USER_AGENT, accept=ACCEPT, transport=transport)
    content_type = r.headers.get("content-type", "")
    page = (url, content_type, body, r.charset_encoding)
    if is_pdf(content_type, body):
        if len(body) >= PDF_MAX_BYTES:
            raise ToolError("unsupported", (
                f"{url} is a PDF larger than {PDF_MAX_BYTES} "
                f"bytes, which this tool does not download."))
        text, why = await asyncio.to_thread(pdf_text, body)
        if why:
            raise ToolError("unsupported", f"{url} {why}.")
        # C'est le TEXTE qui est rendu et gardé en cache : chaque
        # morceau (`offset`) en sort sans refaire l'extraction.
        page = (url, PDF_TYPE, text, None)
    else:
        # Un type non lisible lève ici, avant le cache : rien à garder.
        readable(url, content_type)
    # Seule une page lue avec succès est gardée.
    if r.status_code == 200:
        webcache.CACHE.put(key, page, len(page[2]))
    return page


TOOL = WebFetch()
