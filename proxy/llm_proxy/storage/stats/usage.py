"""
La LECTURE des statistiques : l'Usage API à la forme d'OpenAI (tokens
par modèle, par clé, par tranche de temps), et l'usage des outils
hébergés.
"""

import time

from .db import _reader


# ── Usage API (forme OpenAI) ────────────────────────────────────────────
#
# Deux écarts au schéma, tous deux additifs (un SDK OpenAI ignore ce
# qu'il ne connaît pas) :
#   * bucket_width accepte « all » : un seul seau couvrant toute la plage ;
#   * chaque `result` porte des champs en plus (erreurs, flux, estimations,
#     latences, première et dernière requête).

BUCKET_WIDTHS = {"1m": 60, "1h": 3600, "1d": 86_400}


# Défauts d'OpenAI : une journée de minutes, une journée d'heures, une
# semaine de jours.
DEFAULT_LIMITS = {"1m": 60, "1h": 24, "1d": 7, "all": 1}


MAX_LIMIT = 180


# Seuls `model` et `api_key_id` portent de l'information. Les autres sont
# acceptés, pour ne pas casser un client, mais rendent une valeur nulle.
GROUP_FIELDS = ("model", "project_id", "user_id", "api_key_id", "batch",
                "service_tier")


# Agrégat d'un couple (seau, modèle). Avec bucket_width=all la largeur
# vaut toute la plage, donc l'indice `b` est 0 partout.
_AGG = """
SELECT CAST((ts - ?) / ? AS INTEGER)      AS b,
       {model}                            AS mk,
       {key}                              AS k,
       SUM(prompt_tokens),
       SUM(completion_tokens),
       COUNT(*),
       SUM(status NOT BETWEEN 200 AND 399),
       SUM(streamed),
       SUM(NOT exact),
       SUM(endpoint = '/v1/messages'),
       SUM(cached_tokens),
       SUM(latency),
       MAX(latency),
       MIN(ts),
       MAX(ts)
FROM requests WHERE ts >= ? AND ts < ?{filter}
GROUP BY b, mk, k
ORDER BY b, mk, k
"""


def parse_group_by(values, fields=GROUP_FIELDS) -> list[str]:
    """`group_by` accepte la forme répétée (?group_by=a&group_by=b) et la
    forme séparée par des virgules. Un champ inconnu est une erreur, comme
    à l'upstream. `fields` : ceux de la route interrogée."""
    out = []
    for value in values or []:
        for field in str(value).split(","):
            field = field.strip()
            if not field:
                continue
            if field not in fields:
                raise ValueError(
                    f"group_by « {field} » inconnu ; attendu : "
                    + ", ".join(fields)
                )
            if field not in out:
                out.append(field)
    return out


def recording_since(keys=None) -> float:
    """Horodatage de la plus ancienne requête connue (0 si base vide) ;
    celles des clés `keys` seulement, si elles sont données."""
    where, tail = _key_filter(keys)
    with _reader() as conn:
        return conn.execute("SELECT MIN(ts) FROM requests WHERE 1" + where,
                            tail).fetchone()[0] or 0.0


def recent(since: float) -> list[tuple]:
    """Les requêtes faites depuis `since`, ce qu'il faut pour remplir les
    fenêtres du partage équitable au démarrage (fairshare.seed) :
    (ts, api_key_id, backend, endpoint, tokens d'entrée + de sortie)."""
    with _reader() as conn:
        return conn.execute(
            "SELECT ts, api_key_id, backend, endpoint, "
            "prompt_tokens + completion_tokens FROM requests "
            "WHERE ts > ? AND api_key_id != ''", (since,)).fetchall()


def default_limit(bucket_width: str) -> int:
    return DEFAULT_LIMITS.get(bucket_width, 7)


def _empty_page() -> dict:
    return {"object": "page", "data": [], "has_more": False,
            "next_page": None}


def _model_filter(models) -> tuple[str, tuple]:
    """Filtre `models` : les ids sont ceux que le client emploie, donc les
    noms PRÉFIXÉS (« albert/openweight-large »)."""
    models = [str(m) for m in models if str(m)]
    if not models:
        return "", ()
    return (" AND model_key IN (%s)" % ", ".join("?" * len(models)),
            tuple(models))


def _key_filter(keys) -> tuple[str, tuple]:
    """Filtre `api_key_ids` : None = toutes les clés ; une liste = celles-là
    seulement («» : les requêtes sans clé, celles d'un proxy ouvert)."""
    if keys is None:
        return "", ()
    keys = tuple(str(k) for k in keys)
    if not keys:
        return " AND 0", ()
    return (" AND api_key_id IN (%s)" % ", ".join("?" * len(keys)), keys)


def _usage_page(start_time: int, end_time: int | None, bucket_width: str,
                limit: int | None, page: str | None, empty: bool,
                results) -> dict:
    """La page d'Usage API, commune à toutes les routes d'usage du proxy :
    fenêtre, découpage en seaux alignés, pagination, forme page → bucket.
    Ce qui diffère d'une route à l'autre lui est passé :
    `results(origin, width, lo, hi)` lit les agrégats de la plage [lo, hi[
    et rend, par ligne, (indice du seau, horodatage de sa première ligne,
    l'objet `result`).

    `empty` : un filtre porte sur une dimension que le proxy ne
    possède pas (projet, utilisateur, clé nommée, lot). Aucune ligne ne
    peut y correspondre — on rend une page vide plutôt que d'ignorer
    silencieusement le filtre et de sur-déclarer l'usage."""
    if empty:
        return _empty_page()
    now = int(time.time())
    start_time = max(int(start_time), 0)
    end_time = int(end_time) if end_time else now
    if end_time <= start_time:
        return _empty_page()

    single = bucket_width == "all"
    if single:
        # Un seul seau : sa largeur, c'est la plage entière.
        width = max(end_time - start_time, 1)
        origin = start_time
        total = 1
    else:
        width = BUCKET_WIDTHS.get(bucket_width, 86_400)
        # Seaux alignés sur des multiples de leur largeur depuis l'epoch,
        # comme chez OpenAI : deux appels décalés rendent les mêmes bornes.
        origin = (start_time // width) * width
        total = ((end_time - 1) // width) - (origin // width) + 1

    first = max(int(page or 0), 0)
    count = min(limit if limit else default_limit(bucket_width), MAX_LIMIT)
    count = max(min(count, total - first), 0)
    if count <= 0:
        return _empty_page()

    lo = origin + first * width
    hi = min(lo + count * width, end_time) if single else lo + count * width

    grouped: dict[int, list] = {}
    seen_first: dict[int, float] = {}
    for b, first_ts, result in results(origin, width, lo, hi):
        grouped.setdefault(b, []).append(result)
        seen_first[b] = min(seen_first.get(b, first_ts), first_ts)

    def bounds(i: int) -> tuple[int, int]:
        if not single:
            return origin + i * width, origin + (i + 1) * width
        # Seau unique : on le borne sur les données réellement présentes,
        # ce qui donne au client la date du premier enregistrement sans
        # ajouter de champ au schéma.
        return int(seen_first.get(i, lo)), end_time

    data = []
    for i in range(first, first + count):
        lo_i, hi_i = bounds(i)
        data.append({
            "object": "bucket",
            "start_time": lo_i,
            "end_time": hi_i,
            "results": grouped.get(i, []),
        })
    more = first + count < total
    return {
        "object": "page",
        "data": data,
        "has_more": more,
        "next_page": str(first + count) if more else None,
    }


def usage_completions(start_time: int, end_time: int | None = None,
                      bucket_width: str = "1d", group_by=(),
                      limit: int | None = None, page: str | None = None,
                      models=(), empty: bool = False, keys=None) -> dict:
    """`/v1/organization/usage/completions` : les requêtes relayées, par
    seau et — avec group_by=model / api_key_id — par modèle, par clé.
    `keys` : les seules clés comptées (None : toutes). Fenêtre, seaux et
    pagination : _usage_page."""
    model_expr = "model_key" if "model" in list(group_by) else "NULL"
    key_expr = "api_key_id" if "api_key_id" in list(group_by) else "NULL"
    where, tail = _model_filter(models)
    kwhere, ktail = _key_filter(keys)
    where, tail = where + kwhere, tail + ktail

    def results(origin, width, lo, hi):
        with _reader() as conn:
            rows = conn.execute(_AGG.format(model=model_expr, key=key_expr,
                                            filter=where),
                                (origin, width, lo, hi) + tail).fetchall()
        for (b, mk, key, prompt, completion, requests, errors, streamed,
             estimated, anthropic, cached, latency_sum, latency_max,
             first_ts, last_ts) in rows:
            yield b, first_ts, {
                "object": "organization.usage.completions.result",
                "input_tokens": prompt or 0,
                "output_tokens": completion or 0,
                # Inclus dans input_tokens, comme chez OpenAI (cached_tokens).
                # L'audio reste inconnu : 0.
                "input_cached_tokens": cached or 0,
                "input_audio_tokens": 0,
                "output_audio_tokens": 0,
                "num_model_requests": requests,
                "project_id": None,
                "user_id": None,
                "api_key_id": key,
                "model": mk,
                "batch": None,
                # ── extensions du proxy (hors schéma OpenAI) ──
                "num_errors": errors or 0,
                "num_streamed_requests": streamed or 0,
                "num_estimated_requests": estimated or 0,
                "num_anthropic_requests": anthropic or 0,
                # Latences recomposables d'un seau à l'autre (somme, maximum) :
                # un percentile ne le serait pas, il n'y en a donc pas.
                "total_latency_seconds": round(latency_sum or 0.0, 3),
                "avg_latency_seconds": round((latency_sum or 0.0) / requests, 3)
                if requests else 0.0,
                "max_latency_seconds": round(latency_max or 0.0, 3),
                "first_request_time": int(first_ts),
                "last_request_time": int(last_ts),
            }

    return _usage_page(start_time, end_time, bucket_width, limit, page,
                       empty, results)


# ── Usage des outils hébergés ───────────────────────────────────────────
#
# Aucune route de l'Usage API d'OpenAI ne couvre les appels d'outils en
# général (`web_search_calls` ne compte que des recherches, sans issue ni
# durée) : `/tools` est une route SŒUR, propre au proxy, à la forme
# commune. `num_requests` y est le nombre d'appels ; `tool` et `endpoint`
# sont des dimensions du proxy. Tout s'additionne ou se maximise.

TOOL_GROUP_FIELDS = ("tool", "endpoint", "model", "project_id", "user_id",
                     "api_key_id")


_TOOL_AGG = """
SELECT CAST((ts - ?) / ? AS INTEGER)      AS b,
       {tool}                             AS t,
       {endpoint}                         AS e,
       {model}                            AS mk,
       {key}                              AS k,
       COUNT(*),
       SUM(outcome = 'error'),
       SUM(outcome = 'limit'),
       SUM(duration),
       MAX(duration),
       SUM(result_chars),
       MIN(ts),
       MAX(ts)
FROM tool_calls WHERE ts >= ? AND ts < ?{filter}
GROUP BY b, t, e, mk, k
ORDER BY b, t, e, mk, k
"""


def usage_tools(start_time: int, end_time: int | None = None,
                bucket_width: str = "1d", group_by=(),
                limit: int | None = None, page: str | None = None,
                models=(), empty: bool = False, keys=None) -> dict:
    """`/v1/organization/usage/tools` : les exécutions d'outils hébergés,
    par seau et — selon group_by — par outil, par route d'appel, par
    modèle, par clé. Mêmes paramètres et même page que usage_completions."""
    group_by = list(group_by)
    exprs = {
        "tool": "tool" if "tool" in group_by else "NULL",
        "endpoint": "endpoint" if "endpoint" in group_by else "NULL",
        # Appel direct (/v1/tools) : pas de modèle, donc null — pas «».
        "model": "NULLIF(model_key, '')" if "model" in group_by else "NULL",
        "key": "api_key_id" if "api_key_id" in group_by else "NULL",
    }
    where, tail = _model_filter(models)
    kwhere, ktail = _key_filter(keys)
    where, tail = where + kwhere, tail + ktail

    def results(origin, width, lo, hi):
        with _reader() as conn:
            rows = conn.execute(_TOOL_AGG.format(filter=where, **exprs),
                                (origin, width, lo, hi) + tail).fetchall()
        for (b, tool, endpoint, mk, key, calls, errors, limited, duration_sum,
             duration_max, chars, first_ts, last_ts) in rows:
            # Un refus par limite n'exécute rien (durée 0) : la moyenne
            # porte sur les appels réellement lancés.
            ran = calls - (limited or 0)
            yield b, first_ts, {
                "object": "organization.usage.tools.result",
                "num_requests": calls,
                "project_id": None,
                "user_id": None,
                "api_key_id": key,
                "model": mk,
                # ── dimensions et mesures du proxy ──
                "tool": tool,
                "endpoint": endpoint,
                # Le résultat rendu au modèle commençait par «Error:».
                "num_errors": errors or 0,
                # Refus par limite d'appels (tools.max_calls, max_uses du
                # client, limite propre à l'outil) : comptés à part, ce
                # ne sont pas des pannes de l'outil.
                "num_limited": limited or 0,
                "total_duration_seconds": round(duration_sum or 0.0, 3),
                "avg_duration_seconds": round((duration_sum or 0.0) / ran, 3)
                if ran else 0.0,
                "max_duration_seconds": round(duration_max or 0.0, 3),
                # Caractères rendus au modèle, tous résultats confondus.
                "result_chars": chars or 0,
                "first_request_time": int(first_ts),
                "last_request_time": int(last_ts),
            }

    return _usage_page(start_time, end_time, bucket_width, limit, page,
                       empty, results)
