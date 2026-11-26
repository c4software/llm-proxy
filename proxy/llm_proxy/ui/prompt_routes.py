"""
Les PROMPTS, pour la page (Configuration → Prompts, admin ; sans comptes
OIDC, la page connectée, comme la configuration du proxy) : le catalogue
(prompts/) et les textes que l'admin a modifiés, effectifs à la requête
suivante.

  GET    /ui/prompts          {max_chars, prompts: [{key, domain, default,
                              override, variables}]}
  PUT    /ui/prompts/{clé}    {"text": "…"} → l'entrée mise à jour ; 404 :
                              clé inconnue ; 400 : variable inconnue ou mal
                              écrite, texte trop long ; 409 : prompts.json
                              illisible
  DELETE /ui/prompts/{clé}    rend le texte par défaut → l'entrée
"""

import asyncio

from fastapi import APIRouter, Request

from .. import prompts
from ..core.httputil import error_response, json_only, parse_json
from .config_view import forbidden

router = APIRouter()


def _unknown(key: str):
    return error_response("openai", 404, "not_found", f"prompt « {key} » inconnu")


def _unreadable(exc: prompts.Unreadable):
    return error_response("openai", 409, "conflict", str(exc))


@router.get("/ui/prompts")
async def ui_prompts(request: Request):
    if (bad := forbidden(request)) is not None:
        return bad
    return {"max_chars": prompts.MAX_CHARS, "prompts": prompts.catalog()}


@router.put("/ui/prompts/{key}")
async def ui_prompt_set(request: Request, key: str):
    if (bad := forbidden(request)) is not None:
        return bad
    if (bad := json_only(request)) is not None:
        return bad
    body = parse_json(await request.body())
    text = body.get("text") if isinstance(body, dict) else None
    if not isinstance(text, str):
        return error_response("openai", 400, "invalid_request_error", 'corps attendu : {"text": "…"}')
    try:
        await asyncio.to_thread(prompts.set_override, key, text)
    except KeyError:
        return _unknown(key)
    except prompts.PromptError as exc:
        return error_response("openai", 400, "invalid_prompt", str(exc))
    except prompts.Unreadable as exc:
        return _unreadable(exc)
    return prompts.entry(key)


@router.delete("/ui/prompts/{key}")
async def ui_prompt_reset(request: Request, key: str):
    if (bad := forbidden(request)) is not None:
        return bad
    try:
        await asyncio.to_thread(prompts.reset, key)
    except KeyError:
        return _unknown(key)
    except prompts.Unreadable as exc:
        return _unreadable(exc)
    return prompts.entry(key)
