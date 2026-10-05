"""
`web_search` : une recherche web, par une instance SearXNG auto-hébergée
(métamoteur libre, API JSON, sans clé — le service `searxng` du
docker-compose). Le schéma et la forme de la sortie reprennent ceux de
l'outil web_search d'oh-my-pi (can1357/oh-my-pi) : `query`, `recency`,
`limit` ; une entrée numérotée par résultat — titre, date, URL, extrait.

L'instance est une adresse de CONFIGURATION ([tools.web_search].
searxng_url), en général privée : le garde-fou des adresses publiques
(net.py) ne s'y applique pas, il protège des cibles choisies par le
modèle.

SearXNG ne sert le JSON que si `search.formats` le liste (settings.yml) ;
sinon il répond 403, et l'erreur le dit.
"""

import httpx

from .. import config

ENABLED = config.flag("tools.web_search.enabled", False)
SEARXNG_URL = config.text("tools.web_search.searxng_url", "").rstrip("/")
TIMEOUT = config.num("tools.web_search.timeout", 20)
LIMIT = config.integer("tools.web_search.limit", 8)
MAX_LIMIT = 20
LANGUAGE = config.text("tools.web_search.language", "")
CATEGORIES = config.text("tools.web_search.categories", "")
SNIPPET_CHARS = 240

NAME = "web_search"
# Élément Responses qui rend compte de l'appel au client.
ITEM_TYPE = "web_search_call"
# Types d'outil Responses qui activent cette fonction.
KINDS = ("web_search", "web_search_preview", "web_search_2025_08_26")

DEFINITION = {"type": "function", "function": {
    "name": NAME,
    "description": (
        "Search the web. Returns a numbered list of results (title, date, "
        "URL, snippet). The query accepts the usual operators: site:, "
        "-site:, \"exact phrase\", -term, OR. Use web_fetch to read a "
        "result page. Prefer primary sources and cite the URLs you rely on."),
    "parameters": {
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
    },
}}


def action(args: dict) -> dict:
    return {"type": "search", "query": str(args.get("query") or "")}


def _limit(value) -> int:
    return min(max(value, 1), MAX_LIMIT) if isinstance(value, int) \
        and not isinstance(value, bool) else LIMIT


def format_results(query: str, results: list[dict], limit: int) -> str:
    lines = []
    for r in results:
        url = r.get("url") if isinstance(r, dict) else None
        if not isinstance(url, str) or not url:
            continue
        title = " ".join(str(r.get("title") or url).split())
        date = str(r.get("publishedDate") or "")[:10]
        lines.append(f"[{len(lines) // 3 + 1}] {title}"
                     + (f" ({date})" if date else ""))
        lines.append(f"    {url}")
        snippet = " ".join(str(r.get("content") or "").split())
        if len(snippet) > SNIPPET_CHARS:
            snippet = snippet[:SNIPPET_CHARS].rstrip() + "…"
        lines.append(f"    {snippet}")
        if len(lines) // 3 >= limit:
            break
    if not lines:
        return f"No results for «{query}»."
    return "\n".join(line for line in lines if line.strip())


async def run(args: dict, transport=None) -> str:
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return "Error: `query` is required."
    if not SEARXNG_URL:
        return "Error: web search is not configured on this proxy."
    limit = _limit(args.get("limit"))
    params = {"q": query.strip(), "format": "json", "pageno": 1}
    if args.get("recency") in ("day", "week", "month", "year"):
        params["time_range"] = args["recency"]
    if LANGUAGE:
        params["language"] = LANGUAGE
    if CATEGORIES:
        params["categories"] = CATEGORIES
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport) as c:
            r = await c.get(f"{SEARXNG_URL}/search", params=params)
    except httpx.HTTPError as exc:
        return f"Error: search engine unreachable ({type(exc).__name__})."
    if r.status_code == 403:
        return ("Error: the search engine refused the JSON format (SearXNG: "
                "add `json` to search.formats in settings.yml).")
    if r.status_code != 200:
        return f"Error: search engine returned HTTP {r.status_code}."
    try:
        data = r.json()
    except ValueError:
        return "Error: unreadable search engine response."
    # JSON valide mais d'une autre forme (liste, `results` qui n'en est
    # pas une) : illisible aussi, plutôt qu'une exception.
    results = data.get("results") if isinstance(data, dict) else None
    if results is None:
        results = []
    if not isinstance(data, dict) or not isinstance(results, list):
        return "Error: unreadable search engine response."
    return format_results(query.strip(), results, limit)
