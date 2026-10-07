"""
`web_search` : une recherche web, par une instance SearXNG auto-hébergée
(métamoteur libre, API JSON, sans clé — le service `searxng` du
docker-compose). Le schéma et la forme de la sortie reprennent ceux de
l'outil web_search d'oh-my-pi (can1357/oh-my-pi) : `query`, `recency`,
`limit` ; une entrée numérotée par résultat — titre, date, URL, extrait.

Ce que la recherche rend existe sous deux formes qui disent la même
chose, et `run` rend les deux (contract.Result) : le texte du modèle
(`render`) et les sources (`entries`), d'où sortent les annotations d'un
client chat/completions et les blocs `web_search_result` d'un client
Anthropic. Le texte se refait des sources, à l'octet près : c'est ce qui
permet à un client Anthropic de rejouer ses blocs sans que le proxy ait
rien gardé.

L'instance est une adresse de CONFIGURATION ([tools.web_search].
searxng_url), en général privée : le garde-fou des adresses publiques
(net.py) ne s'y applique pas, il protège des cibles choisies par le
modèle.

SearXNG ne sert le JSON que si `search.formats` le liste (settings.yml) ;
sinon il répond 403, et l'erreur le dit.
"""

import re

import httpx

from .. import config
from . import webcache
from .contract import (Anthropic, Call, Responses, Result, Source, Tool,
                       ToolError)
from .net import domain_match as _domain_match

ENABLED = config.flag("tools.web_search.enabled", False)
SEARXNG_URL = config.text("tools.web_search.searxng_url", "").rstrip("/")
TIMEOUT = config.num("tools.web_search.timeout", 20)
LIMIT = config.integer("tools.web_search.limit", 8)
MAX_LIMIT = 20
LANGUAGE = config.text("tools.web_search.language", "")
CATEGORIES = config.text("tools.web_search.categories", "")
SNIPPET_CHARS = 240

NAME = "web_search"

# La description dit au modèle de lire une page par `web_fetch` : vrai
# seulement là où `web_fetch` lui est présenté aussi (`present` de prompt).
# Un client Anthropic qui ne déclare que la recherche (Claude Code, qui
# lit les pages sur le poste du client) reçoit la description sans cette
# phrase, pour que le modèle n'appelle pas une fonction qui n'existe pas.
_FETCH = "web_fetch"
_FETCH_HINT = f"Use {_FETCH} to read a result page. "


def _limit(value) -> int:
    return min(max(value, 1), MAX_LIMIT) if isinstance(value, int) \
        and not isinstance(value, bool) else LIMIT


_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DATED = re.compile(r"^(.*) \((\d{4}-\d{2}-\d{2})\)$")


def _dated(title: str, date: str) -> tuple[str, str]:
    """(titre, date) tels que le TEXTE les dit. `render` colle la date au
    titre, entre parenthèses, et un client Anthropic rend ses blocs, d'où
    le texte est refait : pour qu'il n'y ait qu'une lecture de «Titre
    (2026-10-03)», seule une date AAAA-MM-JJ est une date. Une date d'une
    autre forme reste dans le titre ; un titre qui finit de lui-même par
    une telle date est lu comme daté — `render` recolle les deux à
    l'identique."""
    if date and not _DATE.fullmatch(date):
        title, date = f"{title} ({date})", ""
    if not date and (dated := _DATED.match(title)):
        title, date = dated.group(1), dated.group(2)
    return title, date


def entries(results, limit: int, allowed=None, blocked=None) -> tuple[Source, ...]:
    """Résultats SearXNG → les SOURCES de ce qui est rendu : `title` /
    `url` / `date` / `snippet` par résultat, `limit` au plus. C'est
    d'elles que sort le texte du modèle (`render`).
    `allowed` / `blocked` : listes de domaines (voir net.domain_match),
    appliquées AVANT `limit` — filtrer après rendrait une liste amputée."""
    out = []
    for r in results:
        url = r.get("url") if isinstance(r, dict) else None
        if not isinstance(url, str) or not url:
            continue
        if allowed and not _domain_match(url, allowed):
            continue
        if blocked and _domain_match(url, blocked):
            continue
        snippet = " ".join(str(r.get("content") or "").split())
        if len(snippet) > SNIPPET_CHARS:
            snippet = snippet[:SNIPPET_CHARS].rstrip() + "…"
        title, date = _dated(" ".join(str(r.get("title") or url).split()),
                             str(r.get("publishedDate") or "")[:10])
        out.append(Source(url, title, date, snippet))
        if len(out) >= limit:
            break
    return tuple(out)


def render(query: str, found) -> str:
    """Les sources → le texte rendu au modèle : une entrée numérotée par
    résultat — titre et date, URL, extrait (ligne omise s'il est vide)."""
    lines = []
    for n, e in enumerate(found, 1):
        lines.append(f"[{n}] {e.title}" + (f" ({e.date})" if e.date else ""))
        lines.append(f"    {e.url}")
        lines.append(f"    {e.snippet}")
    if not lines:
        return f"No results for «{query}»."
    return "\n".join(line for line in lines if line.strip())


def found(query: str, sources) -> Result:
    """Le résultat d'une recherche : le texte, et les sources qu'il dit."""
    sources = tuple(sources)
    return Result(render(query, sources), sources=sources)


class WebSearch(Tool):
    name = NAME
    family = "web"
    responses = Responses(
        kinds=("web_search", "web_search_preview", "web_search_2025_08_26"),
        item="web_search_call")
    anthropic = Anthropic(prefix="web_search", block="web_search_tool_result")

    @property
    def enabled(self) -> bool:
        return ENABLED

    def prompt(self, present) -> str:
        return (
            "Search the web. Returns a numbered list of results (title, date, "
            "URL, snippet). The query accepts the usual operators: site:, "
            "-site:, \"exact phrase\", -term, OR. "
            + (_FETCH_HINT if _FETCH in present else "")
            + "Prefer primary sources and cite the URLs you rely on.")

    def parameters(self, present) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query."},
                "recency": {"type": "string",
                            "enum": ["day", "week", "month", "year"],
                            "description": "Only results from the last day, "
                                           "week, month or year."},
                "limit": {"type": "integer",
                          "description": f"Maximum number of results "
                                         f"(default {LIMIT}, at most {MAX_LIMIT})."},
            },
            "required": ["query"],
        }

    def summary(self, args: dict, result: Result | None = None) -> dict:
        return {"type": "search", "query": str(args.get("query") or "")}

    def render(self, args: dict, sources) -> str:
        return render(str(args.get("query") or "").strip(), sources)

    async def run(self, args: dict, call: Call, transport=None) -> Result:
        """Les listes de domaines de `call.settings` (`allowed_domains`,
        `blocked_domains`) sont celles que le CLIENT pose sur son outil
        serveur (surface Anthropic) : jamais le modèle."""
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ToolError("invalid_input", "`query` is required.")
        if not SEARXNG_URL:
            raise ToolError("unavailable",
                            "web search is not configured on this proxy.")
        limit = _limit(args.get("limit"))
        lists = (call.settings.get("allowed_domains"),
                 call.settings.get("blocked_domains"))
        params = {"q": query.strip(), "format": "json", "pageno": 1}
        if args.get("recency") in ("day", "week", "month", "year"):
            params["time_range"] = args["recency"]
        if LANGUAGE:
            params["language"] = LANGUAGE
        if CATEGORIES:
            params["categories"] = CATEGORIES
        # Le cache web (webcache.py) : les résultats BRUTS de SearXNG pour
        # cette requête — limite et listes de domaines s'appliquent après,
        # à chaque appel. Une recherche identique dans les minutes qui
        # suivent ne repart pas chez les moteurs, qui bloquent vite une
        # adresse trop pressante.
        key = ("search", SEARXNG_URL, tuple(sorted(params.items())))
        cached = webcache.CACHE.get(key)
        if cached is not None:
            return found(query.strip(), entries(cached, limit, *lists))
        try:
            # trust_env=False : l'instance est une adresse du réseau du
            # proxy, un HTTP_PROXY d'environnement n'a pas à s'en mêler.
            async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport,
                                         trust_env=False) as c:
                r = await c.get(f"{SEARXNG_URL}/search", params=params)
        except httpx.HTTPError as exc:
            raise ToolError("unavailable", f"search engine unreachable "
                                           f"({type(exc).__name__}).")
        if r.status_code == 403:
            raise ToolError("unavailable", (
                "the search engine refused the JSON format (SearXNG: "
                "add `json` to search.formats in settings.yml)."))
        if r.status_code != 200:
            raise ToolError("unavailable",
                            f"search engine returned HTTP {r.status_code}.")
        try:
            data = r.json()
        except ValueError:
            raise ToolError("unavailable", "unreadable search engine response.")
        # JSON valide mais d'une autre forme (liste, `results` qui n'en est
        # pas une) : illisible aussi, plutôt qu'une exception.
        results = data.get("results") if isinstance(data, dict) else None
        if results is None:
            results = []
        if not isinstance(data, dict) or not isinstance(results, list):
            raise ToolError("unavailable", "unreadable search engine response.")
        # Aucun résultat PARCE QUE les moteurs de SearXNG ne répondent plus
        # (limite de débit, CAPTCHA : ils bloquent l'adresse de la machine) :
        # ce n'est pas « rien trouvé ». Le dire, et dire de ne pas réessayer
        # — un modèle qui lit « No results » reformule et relance, jusqu'à
        # la limite d'appels, ce qui aggrave le blocage.
        down = data.get("unresponsive_engines")
        if not results and isinstance(down, list) and down:
            names = ", ".join(sorted({
                f"{e[0]}: {e[1]}" if isinstance(e, (list, tuple)) and len(e) > 1
                else str(e[0] if isinstance(e, (list, tuple)) and e else e)
                for e in down}))[:300]
            raise ToolError("unavailable", (
                f"the search engines are temporarily unavailable "
                f"({names}). Do not retry the search now: answer with what "
                f"you already know, or say that the search failed."))
        if results:
            webcache.CACHE.put(key, results, len(r.content))
        return found(query.strip(), entries(results, limit, *lists))


TOOL = WebSearch()
