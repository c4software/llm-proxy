"""
Le relais hors surfaces : GET /v1/audio/voices (routé par le préfixe de
son paramètre `model`) et le catch-all — les POST de
proxy.forward_post_paths relayés (surfaces.forward_post), tout le reste en
404 local. Son routeur s'inclut EN DERNIER (app.py) : le catch-all
prendrait sinon les routes des autres.
"""

import httpx2
from fastapi import APIRouter, Request, Response

from .backends import backend_offline_message, route_backend, unknown_prefix_message
from .httputil import dialect_of, error_response
from .relaying import clean_headers, response_headers
from .settings import FORWARD_POST_PATHS, log
from .surfaces import forward_post

router = APIRouter()


@router.get("/v1/audio/voices")
async def audio_voices(request: Request):
    """Voix d'un modèle de synthèse (GET, ?model=<backend>/<modèle>) : routé
    par le préfixe du paramètre, retiré avant relais comme pour un corps. Le
    relais générique ne transmet que des POST."""
    dialect = dialect_of(request)
    model = request.query_params.get("model", "")
    backend, prefixed = route_backend({"model": model} if model else None)
    if backend is None:
        return error_response(dialect, 400, "unknown_backend_prefix",
                              unknown_prefix_message(model))
    params = dict(request.query_params)
    if prefixed:
        params["model"] = model[len(backend.name) + 1:]
    try:
        upstream = await backend.client.get(
            "/v1/audio/voices", params=params,
            headers=clean_headers(request, backend.api_key),
            timeout=backend.meta_timeout)
    except httpx2.RequestError as exc:
        if not backend.quotas:
            return error_response(dialect, 503, "backend_offline",
                                  backend_offline_message(backend, exc))
        return error_response(dialect, 502, "proxy_error", f"upstream unreachable: {exc}")
    return Response(content=upstream.content, status_code=upstream.status_code,
                    headers=response_headers(upstream))


@router.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def passthrough(path: str, request: Request):
    normalized = "/" + path.strip("/")
    dialect = dialect_of(request)
    if request.method != "POST" or normalized not in FORWARD_POST_PATHS:
        log.info("route non gérée : %s %s → 404 local", request.method, normalized)
        return error_response(
            dialect, 404, "unknown_route",
            f"route non gérée par le proxy : {request.method} {normalized}",
        )

    return await forward_post(request, normalized, dialect)
