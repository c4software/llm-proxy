"""
`web_grep` : chercher dans une page web au lieu de la lire en entier.
C'est l'action `find_in_page` de la recherche web d'OpenAI : le modèle
donne une URL et un motif, il reçoit les lignes qui y répondent, avec
quelques lignes autour — sur une documentation de 200 000 caractères,
deux mille au lieu de dix appels à `web_fetch`.

La page est lue par web_fetch.load : mêmes garde-fous, même conversion
en texte, même cache web — chercher, puis lire par `web_fetch` à l'offset
d'une correspondance, ne retélécharge pas la page.

Le motif est une expression régulière, écrite par le modèle : compilée et
bornée par tools/pattern.py (délai, longueur), comme celle de
`repository`.
"""

import asyncio
import bisect

from .. import prompts
from ..core import config
from . import pattern as patterns
from . import provenance, web_fetch
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

NAME = ToolName.WEB_GREP

# La description renvoie à `web_fetch` pour lire la suite d'une
# correspondance : vrai seulement là où il est présenté aussi.
_FETCH = ToolName.WEB_FETCH


def _bounded(value, default: int, low: int, high: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        return default
    return min(max(value, low), high)


def find(text: str, pattern, max_matches: int, context: int) -> tuple[list[str], int]:
    """Les lignes de `text` qui répondent au motif compilé, en blocs avec
    leur contexte → (lignes rendues, nombre total de lignes qui répondent).
    Chaque ligne rendue : «> offset  texte» pour une correspondance,
    «  offset  texte» pour le contexte, «--» entre deux blocs. Lève
    TimeoutError si le motif dépasse patterns.TIMEOUT."""
    lines = text.split("\n")
    starts, pos = [], 0
    for line in lines:
        starts.append(pos)
        pos += len(line) + 1
    # Le contexte se compte en lignes NON VIDES : le texte d'une page
    # sépare ses paragraphes par des lignes blanches.
    full = [i for i, line in enumerate(lines) if line.strip()]
    hits: dict[int, tuple[int, int]] = {}      # ligne → (début, fin) dans la ligne
    for m in pattern.finditer(text, timeout=patterns.TIMEOUT, concurrent=True):
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
            out.append(f"> {starts[i]}  {patterns.clip(lines[i], *hits[i])}")
        else:
            out.append(f"  {starts[i]}  {patterns.clip(lines[i])}")
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
        return prompts.text(
            "web_grep.description",
            fetch_hint=prompts.text("web_grep.fetch_hint", web_fetch=_FETCH)
            if _FETCH in present else "")

    def parameters(self, present) -> dict:
        return prompts.resolve({
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": prompts.ref("web_grep.param.url")},
                "pattern": {"type": "string", "description": prompts.ref("web_grep.param.pattern")},
                "case_sensitive": {"type": "boolean",
                                   "description": prompts.ref("web_grep.param.case_sensitive")},
                "context": {"type": "integer", "description": prompts.ref("web_grep.param.context")},
                "max_matches": {"type": "integer",
                                "description": prompts.ref("web_grep.param.max_matches")},
            },
            "required": ["url", "pattern"],
        }, context=CONTEXT, max_context=MAX_CONTEXT, max_matches=MAX_MATCHES,
            max_matches_limit=MAX_MATCHES_LIMIT)

    def summary(self, args: dict, result: Result | None = None) -> dict:
        return {"type": "find_in_page", "url": str(args.get("url") or ""),
                "pattern": str(args.get("pattern") or "")}

    async def run(self, args: dict, call: Call, transport=None) -> Result:
        asked = args.get("url")
        url = web_fetch.target(asked)
        provenance.check(url, call)
        source = args.get("pattern")
        pattern = patterns.compile(source,
                                   ignore_case=args.get("case_sensitive") is not True)
        max_matches = _bounded(args.get("max_matches"), MAX_MATCHES, 1,
                               MAX_MATCHES_LIMIT)
        context = _bounded(args.get("context"), CONTEXT, 0, MAX_CONTEXT)
        page = await web_fetch.load(url, call.settings, transport)
        ct, title, text = web_fetch.page_text(*page)
        try:
            lines, total = await asyncio.to_thread(
                find, text, pattern, max_matches, context)
        except TimeoutError:
            raise ToolError("failed", (
                f"the pattern took more than {patterns.TIMEOUT:g} s on this "
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
