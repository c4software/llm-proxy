"""
Ce que toutes les routes partagent pour parler HTTP : la forme des
erreurs (OpenAI ou Anthropic, selon le client), la lecture d'un corps
JSON, et l'exigence d'un corps JSON sur les routes /ui qui écrivent.
Ne connaît que FastAPI et anthropic_api (la forme d'erreur Anthropic).
"""

import json

from fastapi import Request
from fastapi.responses import JSONResponse

from ..surfaces import anthropic_api


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
        body = anthropic_api.error_body(message, anthropic_api.error_type(status))
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
