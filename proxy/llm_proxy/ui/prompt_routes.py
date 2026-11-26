"""
Les PROMPTS, pour la page (Configuration → Prompts) : le catalogue
(prompts/) et les textes que l'admin a modifiés, effectifs à la requête
suivante. Les lire : comme la configuration du proxy (config_view.forbidden) ;
les modifier écrit config.toml : un admin OIDC seulement
(config_view.write_forbidden).

  GET    /ui/prompts          {max_chars, writable, categories: [{label,
                              essential}], prompts: [{key, domain, category,
                              essential, default, override, variables}]}
  PUT    /ui/prompts/{clé}    {"text": "…"} → l'entrée mise à jour, écrite
                              dans [prompts] de config.toml ; 404 : clé
                              inconnue ; 400 : variable inconnue ou mal
                              écrite, texte vide ou trop long ; 409 : pas de
                              config.toml
  DELETE /ui/prompts/{clé}    rend le texte par défaut → l'entrée
"""

import asyncio

from fastapi import APIRouter, Request

from .. import prompts
from ..core.httputil import error_response, json_only, parse_json
from .config_view import WRITE_ERRORS, forbidden, writable, write_error, write_forbidden

router = APIRouter()


def _unknown(key: str):
    return error_response("openai", 404, "not_found", f"prompt « {key} » inconnu")


@router.get("/ui/prompts")
async def ui_prompts(request: Request):
    if (bad := forbidden(request)) is not None:
        return bad
    return {"max_chars": prompts.MAX_CHARS, "writable": writable(request),
            "categories": [{"label": label, "essential": essential}
                           for label, essential, _ in prompts.CATEGORIES],
            "prompts": prompts.catalog()}


@router.put("/ui/prompts/{key}")
async def ui_prompt_set(request: Request, key: str):
    if (bad := write_forbidden(request)) is not None:
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
    except WRITE_ERRORS as exc:
        return write_error(exc)
    return prompts.entry(key)


@router.delete("/ui/prompts/{key}")
async def ui_prompt_reset(request: Request, key: str):
    if (bad := write_forbidden(request)) is not None:
        return bad
    try:
        await asyncio.to_thread(prompts.reset, key)
    except KeyError:
        return _unknown(key)
    except WRITE_ERRORS as exc:
        return write_error(exc)
    return prompts.entry(key)
