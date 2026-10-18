"""
L'Usage API, à la forme de celle d'OpenAI :
/v1/organization/usage/completions (les requêtes) et
/v1/organization/usage/tools (les exécutions d'outils hébergés). La
lecture vient de stats.py ; ici, la validation des paramètres, la
pagination, et ce qu'une page connectée a le droit de voir (own_keys).
"""

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ..access import accounts
from ..access.auth import account_owner, auth_required, request_user, ui_client
from ..storage import stats


router = APIRouter()


# ── Usage API ───────────────────────────────────────────────────────────

USAGE_PATH = "/v1/organization/usage/completions"
# Route sœur, propre au proxy : les exécutions d'outils hébergés, à la
# même forme (stats.usage_tools).
USAGE_TOOLS_PATH = "/v1/organization/usage/tools"


def usage_query(request: Request,
                group_fields=stats.GROUP_FIELDS) -> dict | JSONResponse:
    """Lit et valide les paramètres de l'Usage API. `start_time` est le
    seul obligatoire, comme à l'upstream. `group_fields` : les champs de
    `group_by` de la route interrogée — le seul paramètre qui en dépende."""
    q = request.query_params

    def repeated(name: str) -> list[str]:
        """Un paramètre de liste s'écrit «?x=a&x=b» ou «?x[]=a&x[]=b» —
        c'est cette seconde forme que sérialise le SDK OpenAI. Les deux
        sont acceptées, la virgule aussi."""
        return q.getlist(name) + q.getlist(name + "[]")

    def invalid(message: str, param: str) -> JSONResponse:
        return JSONResponse(
            {"error": {"message": message, "type": "invalid_request_error",
                       "param": param}},
            status_code=400,
        )

    try:
        group_by = stats.parse_group_by(repeated("group_by"), group_fields)
    except ValueError as exc:
        return invalid(str(exc), "group_by")
    width = q.get("bucket_width", "1d")
    if width not in stats.BUCKET_WIDTHS and width != "all":
        return invalid("bucket_width doit valoir 1m, 1h, 1d ou all "
                       "(extension du proxy : un seul seau sur toute la "
                       "plage)", "bucket_width")

    def number(name: str, default):
        raw = q.get(name)
        if raw is None or raw == "":
            return default
        try:
            return int(float(raw))
        except ValueError:
            raise ValueError(name)

    try:
        start_time = number("start_time", None)
        end_time = number("end_time", None)
        limit = number("limit", None)
    except ValueError as name:
        return invalid(f"{name} doit être un entier (secondes Unix)", str(name))
    if start_time is None:
        return invalid("start_time est obligatoire (secondes Unix)", "start_time")
    models = [m for value in repeated("models")
              for m in str(value).split(",") if m.strip()]
    # Filtres portant sur des dimensions que le proxy n'a pas : aucune
    # ligne ne peut y répondre. On le dit en rendant une page vide plutôt
    # qu'en ignorant le filtre — sur-déclarer l'usage serait pire.
    absent = any(repeated(name) for name in ("project_ids", "user_ids")) \
        or q.get("batch") == "true"
    # Les clés : leurs condensés, tels que group_by=api_key_id les rend.
    keys = [k.strip() for value in repeated("api_key_ids")
            for k in str(value).split(",") if k.strip()] or None
    return {
        "start_time": start_time, "end_time": end_time,
        "bucket_width": width, "group_by": group_by,
        "limit": limit, "page": q.get("page"),
        "models": models, "empty": absent, "keys": keys,
    }


async def usage_page(request: Request, read=stats.usage_completions,
                     group_fields=stats.GROUP_FIELDS, own: bool = False):
    """Forme de l'Usage API OpenAI, page → buckets → results. Un modèle
    n'apparaît que s'il a servi au moins une requête dans le seau
    concerné. Les agrégats sortent de SQLite : la lecture part dans le
    pool de threads pour ne pas bloquer la boucle d'événements.
    `read` / `group_fields` : la lecture et les champs de `group_by` de la
    route — les requêtes par défaut, les outils pour usage_tools. `own` :
    les seules lignes de la clé présentée (le tableau de bord, auth
    active), quel que soit le filtre demandé."""
    params = usage_query(request, group_fields)
    if isinstance(params, JSONResponse):
        return params
    if own and (keys := await own_keys(request)) is not None:
        params["keys"] = keys
    return await run_in_threadpool(read, **params)


async def own_keys(request: Request) -> list[str] | None:
    """Les api_key_id que la page connectée peut voir ; None = tous (proxy
    ouvert, ou un admin). Un compte voit ses clés, révoquées comprises, et
    le chat de sa page."""
    if not auth_required():
        return None
    user = request_user(request)
    if user is None:
        return [ui_client(request)]
    if user["role"] == "admin":
        return None
    return [account_owner(user["id"])] + await asyncio.to_thread(
        accounts.digests, user["id"])


@router.get(USAGE_PATH)
async def usage_completions(request: Request):
    return await usage_page(request)


@router.get(USAGE_TOOLS_PATH)
async def usage_tools(request: Request):
    """Les exécutions d'outils hébergés : mêmes paramètres, mêmes
    validations, même pagination que la route ci-dessus ; `group_by` sur
    `tool`, `endpoint` (la route d'appel) et `model`."""
    return await usage_page(request, stats.usage_tools,
                            stats.TOOL_GROUP_FIELDS)
