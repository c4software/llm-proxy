"""
`web_search` : une recherche web, par une instance SearXNG auto-hébergée
(métamoteur libre, API JSON, sans clé — le service `searxng` du
docker-compose). Le schéma et la forme de la sortie reprennent ceux de
l'outil web_search d'oh-my-pi (can1357/oh-my-pi) : `query`, `recency`,
`limit` ; une entrée numérotée par résultat — titre, date, URL, extrait.

Ce que la recherche rend existe sous deux formes qui disent la même
chose : la liste structurée (`entries`) et le texte du modèle (`render`),
qu'on relit dans l'autre sens par `parse`. `run` ne rend que le texte ;
la surface Anthropic en tire les blocs `web_search_result` de son client.

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
# Élément Responses qui rend compte de l'appel au client.
ITEM_TYPE = "web_search_call"
# Types d'outil Responses qui activent cette fonction.
KINDS = ("web_search", "web_search_preview", "web_search_2025_08_26")

# La description dit au modèle de lire une page par `web_fetch` : vrai
# seulement là où `web_fetch` lui est présenté aussi. La surface Anthropic
# ne présente que la recherche (Claude Code lit les pages sur le poste du
# client) — elle prend `definition(fetch=False)`, sans cette phrase, pour
# que le modèle n'appelle pas une fonction qui n'existe pas.
_FETCH_HINT = "Use web_fetch to read a result page. "


def definition(fetch: bool = True) -> dict:
    return {"type": "function", "function": {
        "name": NAME,
        "description": (
            "Search the web. Returns a numbered list of results (title, date, "
            "URL, snippet). The query accepts the usual operators: site:, "
            "-site:, \"exact phrase\", -term, OR. "
            + (_FETCH_HINT if fetch else "")
            + "Prefer primary sources and cite the URLs you rely on."),
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


DEFINITION = definition()


def action(args: dict) -> dict:
    return {"type": "search", "query": str(args.get("query") or "")}


def _limit(value) -> int:
    return min(max(value, 1), MAX_LIMIT) if isinstance(value, int) \
        and not isinstance(value, bool) else LIMIT


def entries(results, limit: int, allowed=None, blocked=None) -> list[dict]:
    """Résultats SearXNG → la liste STRUCTURÉE de ce qui est rendu : un
    dict `title` / `url` / `date` / `snippet` par résultat, `limit` au
    plus. C'est d'elle que sortent le texte du modèle (`render`) et, sur
    la surface Anthropic, les blocs `web_search_result` du client.
    `allowed` / `blocked` : listes de domaines (voir _domain_match),
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
        out.append({"title": " ".join(str(r.get("title") or url).split()),
                    "url": url,
                    "date": str(r.get("publishedDate") or "")[:10],
                    "snippet": snippet})
        if len(out) >= limit:
            break
    return out


def render(query: str, found: list[dict]) -> str:
    """La liste structurée → le texte rendu au modèle : une entrée
    numérotée par résultat — titre et date, URL, extrait (ligne omise
    s'il est vide)."""
    lines = []
    for n, e in enumerate(found, 1):
        lines.append(f"[{n}] {e['title']}"
                     + (f" ({e['date']})" if e["date"] else ""))
        lines.append(f"    {e['url']}")
        lines.append(f"    {e['snippet']}")
    if not lines:
        return f"No results for «{query}»."
    return "\n".join(line for line in lines if line.strip())


_HEAD = re.compile(r"^\[(\d+)\] (.*)$")
_DATED = re.compile(r"^(.*) \((\d{4}-\d{2}-\d{2})\)$")


def parse(text: str) -> list[dict]:
    """L'inverse de `render` : le texte rendu au modèle → la liste
    structurée. La surface Anthropic en a besoin dans ce sens-là : ce
    qu'elle reçoit de l'exécution (tools.Hosted.run) est le TEXTE, borné
    et tel que le modèle le lira ; les blocs du client en sont déduits,
    donc ne peuvent pas dire autre chose que lui. Et `render(parse(t))`
    redonne `t` : un client qui rejoue ses blocs fait retrouver au modèle
    le texte d'origine, sans que le proxy ait rien conservé.
    Un texte d'erreur, «No results», une entrée coupée par la troncature
    (sans URL) : rien."""
    out: list[dict] = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        head = _HEAD.match(lines[i])
        i += 1
        if not head or i >= len(lines) or not lines[i].startswith("    "):
            continue
        title, date = head.group(2), ""
        # Une date est collée au titre, entre parenthèses ; seule la
        # forme AAAA-MM-JJ en est relue (une date d'une autre forme reste
        # dans le titre). Un titre qui finirait de lui-même par une telle
        # date est lu comme daté : `render` recolle les deux à l'identique.
        dated = _DATED.match(title)
        if dated:
            title, date = dated.group(1), dated.group(2)
        entry = {"title": title, "url": lines[i][4:], "date": date,
                 "snippet": ""}
        i += 1
        if i < len(lines) and lines[i].startswith("    "):
            entry["snippet"] = lines[i][4:]
            i += 1
        out.append(entry)
    return out


def format_results(query: str, results: list[dict], limit: int,
                   allowed=None, blocked=None) -> str:
    return render(query, entries(results, limit, allowed, blocked))


async def run(args: dict, transport=None, allowed_domains=None,
              blocked_domains=None) -> str:
    """`allowed_domains` / `blocked_domains` ne viennent jamais du modèle :
    ce sont les listes que le CLIENT pose sur son outil serveur (surface
    Anthropic), passées par tools.Hosted.run."""
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
    # Le cache web (webcache.py) : les résultats BRUTS de SearXNG pour cette
    # requête — limite et listes de domaines s'appliquent après, à chaque
    # appel. Une recherche identique dans les minutes qui suivent ne repart
    # pas chez les moteurs, qui bloquent vite une adresse trop pressante.
    key = ("search", SEARXNG_URL, tuple(sorted(params.items())))
    cached = webcache.CACHE.get(key)
    if cached is not None:
        return format_results(query.strip(), cached, limit, allowed_domains,
                              blocked_domains)
    try:
        # trust_env=False : l'instance est une adresse du réseau du proxy,
        # un HTTP_PROXY d'environnement n'a pas à s'en mêler.
        async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport,
                                     trust_env=False) as c:
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
    # Aucun résultat PARCE QUE les moteurs de SearXNG ne répondent plus
    # (limite de débit, CAPTCHA : ils bloquent l'adresse de la machine) :
    # ce n'est pas « rien trouvé ». Le dire, et dire de ne pas réessayer —
    # un modèle qui lit « No results » reformule et relance, jusqu'à la
    # limite d'appels, ce qui aggrave le blocage.
    down = data.get("unresponsive_engines")
    if not results and isinstance(down, list) and down:
        names = ", ".join(sorted({
            f"{e[0]}: {e[1]}" if isinstance(e, (list, tuple)) and len(e) > 1
            else str(e[0] if isinstance(e, (list, tuple)) and e else e)
            for e in down}))[:300]
        return (f"Error: the search engines are temporarily unavailable "
                f"({names}). Do not retry the search now: answer with what "
                f"you already know, or say that the search failed.")
    if results:
        webcache.CACHE.put(key, results, len(r.content))
    return format_results(query.strip(), results, limit, allowed_domains,
                          blocked_domains)
