"""
Ce que toutes les routes partagent pour parler HTTP : la forme des
erreurs (OpenAI ou Anthropic, selon le client), la lecture d'un corps
JSON, et l'exigence d'un corps JSON sur les routes /ui qui écrivent.
Ne connaît que FastAPI : la forme d'erreur Anthropic est définie ici.
"""

import json
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse


# Types d'erreur de l'API Anthropic par statut HTTP.
ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    429: "rate_limit_error",
    500: "api_error",
    529: "overloaded_error",
}


def error_type(status: int) -> str:
    if status in ERROR_TYPES:
        return ERROR_TYPES[status]
    return "api_error" if status >= 500 else "invalid_request_error"


def error_body(message: str, type_: str) -> dict:
    return {"type": "error", "error": {"type": type_, "message": message}}


def dialect_of(request: Request) -> str:
    """Forme des erreurs attendue par le client : «anthropic» si la
    requête vient d'un SDK Anthropic (il pose `anthropic-version` sur
    chaque appel) ou vise /v1/messages ; «openai» sinon."""
    if request.url.path.startswith("/v1/messages") \
            or "anthropic-version" in request.headers:
        return "anthropic"
    return "openai"


def error_response(dialect: str, status: int, type_: str, message: str,
                   headers: dict | None = None) -> JSONResponse:
    """L'UNIQUE constructeur d'erreur : {"error": {message, type}} pour
    un client OpenAI, {"type": "error", "error": {type, message}} pour
    un client Anthropic. `type_` est le type OpenAI (celui des logs et
    de la doc) ; côté Anthropic le type se déduit du statut."""
    if dialect == "anthropic":
        body = error_body(message, error_type(status))
    else:
        body = {"error": {"message": message, "type": type_}}
    return JSONResponse(body, status_code=status, headers=headers)


def parse_json(raw: bytes):
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def json_only(request: Request) -> JSONResponse | None:
    """Une route /ui qui ÉCRIT ou DÉPENSE exige un corps JSON : un
    formulaire d'une page tierce ne peut pas en envoyer sans requête
    préliminaire CORS, que le proxy ne sert pas : c'est ce qui la ferme
    aux autres sites (la session est SameSite=Lax, pour le retour OIDC)."""
    ct = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ct != "application/json":
        return error_response("openai", 415, "invalid_request_error",
                              "corps JSON attendu (content-type: "
                              "application/json)")
    return None


def anthropic_disabled(dialect: str) -> JSONResponse:
    return error_response(
        dialect, 404, "unknown_route",
        "surface Anthropic désactivée sur ce proxy ([anthropic].enabled "
        "dans config.toml)",
    )


def origin(url: str) -> tuple:
    """(schéma, hôte, port) d'une URL, port par défaut compris : ce qui doit
    rester le même pour qu'un jeton la suive (une redirection, une source
    de dépôts modifiée). Un port illisible compte pour None."""
    p = urlsplit(url)
    try:
        port = p.port
    except ValueError:
        port = None
    return (p.scheme.lower(), (p.hostname or "").lower(),
            port or (443 if p.scheme.lower() == "https" else 80))
