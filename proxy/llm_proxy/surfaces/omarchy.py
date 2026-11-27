"""
Les modèles du proxy vus comme une machine du tailnet par `omarchy models`
(Omarchy) : la liste des modèles partagés et, par modèle, une base
OpenAI-compatible.

  GET  /local-ai/models.json                      la liste de ce que voit la clé
  GET  /local-ai/<id>/v1/models                   le modèle, au format OpenAI
  POST /local-ai/<id>/v1/chat/completions         le chat, comme /v1/chat/completions

Omarchy nomme un modèle par un identifiant de recette
(`^[a-z0-9]+([.-][a-z0-9]+)*$`) : le nom préfixé du proxy s'y ramène par
`recipe_id` («albert/Mistral_Small» → «albert.mistral-small»). Deux noms
qui donnent le même identifiant sont tous deux écartés. Ne sont
proposés que les modèles de conversation que la clé peut employer ;
[omarchy].enabled faux (défaut), les routes répondent 404.

La clé est celle que l'appairage d'Omarchy range : une clé du proxy, avec
ses modèles permis et ses statistiques. Une machine du tailnet lit la liste
sans clé tant qu'elle n'est pas appairée : le middleware la laisse passer
(auth.OPEN_PATHS), et elle est vide quand l'auth est active.
"""

import json
import re

from fastapi import APIRouter, Request

from ..access.auth import auth_required, request_owner
from ..core import config
from ..core.httputil import error_response, parse_json
from ..upstream.backends import route_backend
from ..upstream.catalog import CHAT_TYPES, accepts_images, merged_models
from .endpoints import chat

PREFIX = "/local-ai"
LISTING_PATH = PREFIX + "/models.json"
# Le schéma de la liste qu'attend omarchy-local-ai (NETWORK_SCHEMA).
SCHEMA = 1
# Omarchy ne lit que les 64 premiers modèles d'une liste.
MAX_MODELS = 64
# Le plafond de réponse qu'Omarchy prend d'une liste qui n'en donne pas
# (DEFAULT_MAX_OUTPUT_TOKENS), quand le backend n'en fixe pas.
DEFAULT_MAX_TOKENS = 16384

ENABLED = config.flag("omarchy.enabled", False)
DEFAULT_CONTEXT = config.integer("omarchy.context", 32768, minimum=1)

RECIPE_RE = re.compile(r"^[a-z0-9]+([.-][a-z0-9]+)*$")

router = APIRouter()


def recipe_id(name: str) -> str:
    """Le nom préfixé → un identifiant de recette Omarchy, «» s'il n'en
    reste rien : minuscules, «/» → «.», tout autre caractère → «-», une
    suite de séparateurs réduite à un seul («.» s'il y en a un)."""
    s = re.sub(r"[^a-z0-9./-]+", "-", name.lower()).replace("/", ".")
    s = re.sub(r"[.-]+", lambda m: "." if "." in m.group() else "-", s)
    s = s.strip(".-")
    return s if RECIPE_RE.match(s) else ""


async def offered(request: Request) -> dict[str, dict]:
    """Les modèles de conversation que voit le client de la requête, par
    identifiant de recette ; aucun pour une requête sans clé, l'auth
    active."""
    if auth_required() and not request_owner(request):
        return {}
    seen: dict[str, list[dict]] = {}
    for e in await merged_models(request) or []:
        if e.get("type") in CHAT_TYPES and (rid := recipe_id(str(e["id"]))):
            seen.setdefault(rid, []).append(e)
    return {rid: found[0] for rid, found in sorted(seen.items())
            if len(found) == 1}


async def limits(entry: dict) -> tuple[int, int, list[str]]:
    """(contexte, plafond de réponse, entrées) annoncés pour `entry`."""
    ctx = entry.get("max_context_length") or DEFAULT_CONTEXT
    b = route_backend({"model": entry["id"]})
    if b is None:
        return ctx, min(DEFAULT_MAX_TOKENS, ctx), ["text"]
    plain = str(entry["id"])[len(b.name) + 1:]
    sees = entry.get("type") == "image-text-to-text" \
        and await accepts_images(b, plain)
    return ctx, min(b.max_tokens or DEFAULT_MAX_TOKENS, ctx), \
        ["text", "image"] if sees else ["text"]


def disabled():
    return error_response("openai", 404, "unknown_route",
                          "surface Omarchy désactivée ([omarchy].enabled)")


@router.get(LISTING_PATH)
async def listing(request: Request):
    """La liste qu'Omarchy lit sur chaque machine du tailnet
    (`omarchy models network`), ce que voit la clé présentée. Chaque modèle
    y est «active» : le proxy ne démarre ni n'arrête rien."""
    if not ENABLED:
        return disabled()
    models = []
    for rid, e in list((await offered(request)).items())[:MAX_MODELS]:
        vendor = str(e.get("owned_by") or "").lower()
        ctx, max_tokens, entries = await limits(e)
        models.append({
            "recipe": rid,
            "name": str(e["id"]),
            "vendor": vendor if RECIPE_RE.match(vendor) else "",
            "kind": "text",
            "edit": False,
            "architecture": "",
            "ctx": ctx,
            "max_tokens": max_tokens,
            "input": entries,
            "unit": "active",
            "error": "",
            "fits": True,
            "since_ms": None,
            "expected_load_seconds": None,
            "expected_stop_seconds": None,
            "observed": None,
            "decode_tok_s": None,
            "prefill_tok_s": None,
        })
    # Ni `host` ni `dns` : Omarchy prend ceux de l'appairage ou du tailnet.
    return {"schema": SCHEMA, "ram_total_gib": None, "models": models}


async def resolve(request: Request, rid: str) -> dict | None:
    """Le modèle derrière `rid`, parmi ceux que le client peut employer."""
    if not RECIPE_RE.match(rid):
        return None
    return (await offered(request)).get(rid)


def unknown(rid: str):
    return error_response("openai", 404, "model_not_found",
                          f"modèle «{rid}» inconnu ou non permis à cette clé")


@router.get(PREFIX + "/{rid}/v1/models")
async def model_list(request: Request, rid: str):
    if not ENABLED:
        return disabled()
    if (e := await resolve(request, rid)) is None:
        return unknown(rid)
    return {"object": "list", "data": [{
        "id": rid, "object": "model", "created": e.get("created") or 0,
        "owned_by": e.get("owned_by") or "",
        "max_context_length": e.get("max_context_length")}]}


@router.post(PREFIX + "/{rid}/v1/chat/completions")
async def chat_completions(request: Request, rid: str):
    """/v1/chat/completions, le `model` du corps (l'identifiant de recette)
    remplacé par le nom préfixé du proxy."""
    if not ENABLED:
        return disabled()
    if (e := await resolve(request, rid)) is None:
        return unknown(rid)
    payload = parse_json(await request.body())
    if not isinstance(payload, dict):
        return error_response("openai", 400, "invalid_request_error",
                              "corps JSON attendu")
    payload["model"] = e["id"]
    return await chat(request, raw=json.dumps(payload, ensure_ascii=False).encode())
