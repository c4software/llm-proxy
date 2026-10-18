"""
`web_grep` : chercher dans une page web au lieu de la lire en entier.
C'est l'action `find_in_page` de la recherche web d'OpenAI : le modèle
donne une URL et un motif, il reçoit les lignes qui y répondent, avec
quelques lignes autour — sur une documentation de 200 000 caractères,
deux mille au lieu de dix appels à `web_fetch`.

La page est lue par web_fetch.load : mêmes garde-fous, même conversion
en texte, même cache web — chercher, puis lire par `web_fetch` à l'offset
d'une correspondance, ne retélécharge pas la page.

Le motif est une expression régulière, écrite par le modèle : elle passe
par la bibliothèque `regex` et non `re`, parce que `regex` a un délai
(`timeout`). Une expression à retour arrière catastrophique — `(a+)+$` —
est coupée au bout de REGEX_TIMEOUT au lieu de bloquer le proxy. La
recherche tourne dans un fil (`concurrent=True` rend le GIL) : la boucle
asyncio ne l'attend pas.
"""

import asyncio
import bisect

from .. import config
from . import web_fetch
from .contract import Call, Responses, Result, Source, Tool, ToolError, ToolName

ENABLED = config.flag("tools.web_grep.enabled", False)
# Lignes qui répondent rendues quand le modèle ne précise pas
# `max_matches` ; il peut en demander jusqu'à MAX_MATCHES_LIMIT.
MAX_MATCHES = config.integer("tools.web_grep.max_matches", 20)
MAX_MATCHES_LIMIT = 50
# Lignes (non vides) montrées de chaque côté d'une correspondance quand le
# modèle ne précise pas `context` ; au plus MAX_CONTEXT.
CONTEXT = config.integer("tools.web_grep.context", 1)
MAX_CONTEXT = 5
# Délai (s) de la recherche du motif dans la page, lecture non comprise.
REGEX_TIMEOUT = config.num("tools.web_grep.regex_timeout", 1.0)
# Une ligne de texte peut être un paragraphe entier : coupée autour de la
# correspondance (ou à son début, pour une ligne de contexte).
LINE_CHARS = 300
MAX_PATTERN = 500

NAME = ToolName.WEB_GREP

# La description renvoie à `web_fetch` pour lire la suite d'une
# correspondance : vrai seulement là où il est présenté aussi.
_FETCH = ToolName.WEB_FETCH
_FETCH_HINT = (f" Pass a line's offset to {_FETCH} as `offset` to read the "
               f"page from there.")


def _bounded(value, default: int, low: int, high: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        return default
    return min(max(value, low), high)


def _clip(line: str, start: int = 0, end: int = 0) -> str:
    """La ligne coupée à LINE_CHARS, autour de [start, end) s'il est
    donné (la correspondance), depuis son début sinon."""
    if len(line) <= LINE_CHARS:
        return line
    if end <= start:
        return line[:LINE_CHARS] + "…"
    margin = max((LINE_CHARS - (end - start)) // 2, 0)
    a = max(start - margin, 0)
    b = min(a + LINE_CHARS, len(line))
    a = max(b - LINE_CHARS, 0)
    return ("…" if a else "") + line[a:b] + ("…" if b < len(line) else "")


def find(text: str, pattern, max_matches: int, context: int) -> tuple[list[str], int]:
    """Les lignes de `text` qui répondent au motif compilé, en blocs avec
    leur contexte → (lignes rendues, nombre total de lignes qui répondent).
    Chaque ligne rendue : «> offset  texte» pour une correspondance,
    «  offset  texte» pour le contexte, «--» entre deux blocs. Lève
    TimeoutError si le motif dépasse REGEX_TIMEOUT."""
    lines = text.split("\n")
    starts, pos = [], 0
    for line in lines:
        starts.append(pos)
        pos += len(line) + 1
    # Le contexte se compte en lignes NON VIDES : le texte d'une page
    # sépare ses paragraphes par des lignes blanches.
    full = [i for i, line in enumerate(lines) if line.strip()]
    hits: dict[int, tuple[int, int]] = {}      # ligne → (début, fin) dans la ligne
    for m in pattern.finditer(text, timeout=REGEX_TIMEOUT, concurrent=True):
        i = bisect.bisect_right(starts, m.start()) - 1
        if i in hits or not lines[i].strip():
            continue
        hits[i] = (m.start() - starts[i],
                   min(m.end(), starts[i] + len(lines[i])) - starts[i])
    total = len(hits)
    shown = set(sorted(hits)[:max_matches])
    keep: set[int] = set()
    for i in shown:
        k = bisect.bisect_left(full, i)
        keep.update(full[max(k - context, 0):k + context + 1])
    out, last = [], None
    for i in sorted(keep):
        if last is not None and bisect.bisect_left(full, i) \
                != bisect.bisect_left(full, last) + 1:
            out.append("--")
        if i in shown:
            out.append(f"> {starts[i]}  {_clip(lines[i], *hits[i])}")
        else:
            out.append(f"  {starts[i]}  {_clip(lines[i])}")
        last = i
    return out, total


class WebGrep(Tool):
    name = NAME
    family = "web"
    # Activée avec la recherche, comme web_fetch, et rendue par le même
    # élément, d'action `find_in_page`.
    responses = Responses(
        kinds=("web_search", "web_search_preview", "web_search_2025_08_26"),
        item="web_search_call")

    @property
    def enabled(self) -> bool:
        return ENABLED

    def prompt(self, present) -> str:
        return (
            "Search a web page for a regular expression and return only the "
            "matching lines, with a few lines of context (HTML and PDF are "
            "converted to plain text first). Prefer it to reading a long "
            "page in full: documentation, changelogs, specifications, "
            "reference tables. Each line comes with its character offset in "
            "the page." + (_FETCH_HINT if _FETCH in present else ""))

    def parameters(self, present) -> dict:
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string",
                        "description": "The http(s) URL of the page."},
                "pattern": {"type": "string",
                            "description": "Regular expression to look for "
                                           "(a plain word works too). Use "
                                           "`|` for alternatives."},
                "case_sensitive": {"type": "boolean",
                                   "description": "Match case exactly "
                                                  "(default false)."},
                "context": {"type": "integer",
                            "description": f"Lines shown before and after "
                                           f"each match (default {CONTEXT}, "
                                           f"at most {MAX_CONTEXT})."},
                "max_matches": {"type": "integer",
                                "description": f"Maximum number of matching "
                                               f"lines (default {MAX_MATCHES}, "
                                               f"at most {MAX_MATCHES_LIMIT})."},
                **({"render": web_fetch.RENDER_PARAMS["render"]}
                   if web_fetch.renderer.available() else {}),
            },
            "required": ["url", "pattern"],
        }

    def summary(self, args: dict, result: Result | None = None) -> dict:
        return {"type": "find_in_page", "url": str(args.get("url") or ""),
                "pattern": str(args.get("pattern") or "")}

    async def run(self, args: dict, call: Call, transport=None) -> Result:
        # Importé ici : le proxy démarre sans `regex`.
        import regex

        asked = args.get("url")
        url = web_fetch.target(asked)
        source = args.get("pattern")
        if not isinstance(source, str) or not source.strip():
            raise ToolError("invalid_input", "`pattern` is required.")
        if len(source) > MAX_PATTERN:
            raise ToolError("invalid_input",
                            f"`pattern` is longer than {MAX_PATTERN} characters.")
        flags = regex.MULTILINE | regex.V0
        if args.get("case_sensitive") is not True:
            flags |= regex.IGNORECASE
        try:
            pattern = regex.compile(source, flags)
        except regex.error as exc:
            raise ToolError("invalid_input", f"invalid regular expression ({exc}).")
        max_matches = _bounded(args.get("max_matches"), MAX_MATCHES, 1,
                               MAX_MATCHES_LIMIT)
        context = _bounded(args.get("context"), CONTEXT, 0, MAX_CONTEXT)
        page = await web_fetch.load(url, call.settings, transport,
                                    rendered=args.get("render") is True)
        ct, title, text = web_fetch.page_text(*page)
        try:
            lines, total = await asyncio.to_thread(
                find, text, pattern, max_matches, context)
        except TimeoutError:
            raise ToolError("failed", (
                f"the pattern took more than {REGEX_TIMEOUT:g} s on this "
                f"page; use a simpler expression."))
        head = [f"URL: {page[0]}"]
        if title:
            head.append(f"Title: {title}")
        if ct:
            head.append(f"Content-Type: {ct}")
        head.append(f"Characters: {len(text)}")
        meta = {"url": page[0], "title": title, "content_type": ct,
                "total": len(text), "matches": total}
        sources = (Source(asked, asked),)
        if not total:
            head.append(f"Matches: none for /{source}/.")
            return Result("\n".join(head), sources=sources, meta=meta)
        head.append(f"Matches: {total} line(s) for /{source}/"
                    + (f", first {max_matches} shown" if total > max_matches else "")
                    + ". Matching lines start with `>`; every line starts with "
                      "its character offset in the page.")
        return Result("\n".join(head) + "\n\n---\n" + "\n".join(lines),
                      sources=sources, meta=meta)


TOOL = WebGrep()
