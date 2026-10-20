"""
Le CHAT de la page et ce qu'il appelle : la session (ce que la page sait
d'elle), les modèles, /ui/chat/completions, les images, la voix, l'usage.
"""

import base64
import binascii
import re
import httpx2
from fastapi import Request
from ... import tools
from ...access import accounts, fairshare
from ...core.settings import INCOGNITO
from ...storage import files, stats
from ...surfaces import chat_api, relay_routes
from ...upstream import albert, catalog, relaying
from ...upstream.backends import BACKENDS, route_backend, unknown_prefix_message
from ...access.auth import auth_required, request_user, ui_client
from ...core.httputil import error_response, json_only, parse_json
from ...surfaces.endpoints import chat, forward_post
from ...upstream.catalog import CHAT_TYPES, IMAGE_TYPE, always_shown
from ...upstream.relaying import Call, forward, gate
from ..usage_api import usage_page

from .accounts import ui_login_mode
from .. import linux_relay, page_tools
from .page import UI_CHAT, UI_CHAT_MODEL, router


def chat_tool_groups() -> list[dict]:
    """Ce que le chat de la page peut déclarer : un type par groupe
    (`{"type": "web_search"}` présente web_search, web_fetch et
    web_grep), avec les outils actifs qu'il présente. Relu à chaque
    appel : un serveur MCP découvert depuis y entre. `specs` : les
    fonctions que le proxy présentera au modèle pour ce groupe, que la
    page montre (le prompt qu'elle n'envoie pas) et compte en tokens."""
    groups: dict[str, list] = {}
    for tool in tools.Hosted().tools:
        if tool.kinds:
            # Un outil MCP se déclare par son nom ou par son serveur
            # (`mcp:<serveur>`) : un interrupteur par SERVEUR, pas par outil.
            kind = next((k for k in tool.kinds
                         if k.startswith(tools.mcp.KIND + ":")), tool.kinds[0])
            groups.setdefault(kind, []).append(tool)
    out = []
    for kind, found in groups.items():
        present = frozenset(t.name for t in found)
        out.append({"type": kind, "tools": [t.name for t in found],
                    "specs": [t.spec(present) for t in found]})
    return out


@router.get("/ui/session")
async def ui_session(request: Request):
    """Ce que la page doit savoir de qui la regarde et de ce qui lui est
    ouvert. Auth active sans clé valide : 401 (le middleware), et la page
    demande la clé."""
    chat_tools = UI_CHAT and chat_api.ENABLED
    user = request_user(request)
    if user is not None and user["role"] not in accounts.ACTIVE:
        # En attente ou bloqué : qui il est, rien de plus.
        return {"auth_required": True, "login": "oidc", "account": user,
                "client": None, "chat": {"enabled": False}, "memory": False}
    return {
        "auth_required": auth_required(),
        "login": ui_login_mode(),
        "account": user,
        # Un fragment du condensé, pour dire «c'est bien vous» sans rien
        # dire de la clé.
        "client": ui_client(request)[:8] or None,
        "chat": {
            "enabled": UI_CHAT,
            "model": UI_CHAT_MODEL or None,
            "hosted_tools": chat_tools,
            "tools": chat_tool_groups() if chat_tools else [],
            "always": always_shown() if chat_tools else [],
            "image_sizes": list(tools.image_generation.SIZES),
            "image_size": tools.image_generation.SIZE,
            # Les outils de la page refusés à tous ([ui].page_tools_disabled).
            "page_tools_disabled": page_tools.disabled(),
            # Le relais réseau de la VM Linux ([ui].linux_relay).
            "linux_relay": linux_relay.allowed(user),
        },
        "memory": tools.memory.ENABLED,
        # None : proxy ouvert, personne n'est compté.
        "fair_share": await fair_share_state(),
    }


async def fair_share_state() -> dict | None:
    client = fairshare.CLIENT.get()
    if client is None:
        return None
    return {"share": await relaying.share_of(), "used": fairshare.used(client),
            "default": fairshare.DEFAULT_SHARE or None,
            "window_seconds": int(fairshare.WINDOW)}


@router.get("/ui/models")
async def ui_models(request: Request):
    """Les modèles de conversation du catalogue (CHAT_TYPES), sous /ui pour
    le cookie. Servi même chat fermé : /v1/models le dit déjà à la même
    clé. `?all=1` : tout le catalogue, avec le type de chaque modèle ;
    `?images=1` : les modèles d'images en plus."""
    every = request.query_params.get("all") in ("1", "true")
    images = request.query_params.get("images") in ("1", "true")
    entries = await catalog.merged_models(request)
    if entries is None:
        return error_response("openai", 502, "proxy_error",
                              "aucun backend joignable pour /v1/models")
    data = []
    for entry in entries:
        name, _, plain = str(entry.get("id", "")).partition("/")
        b = BACKENDS.get(name)
        # Le type du catalogue normalisé, à défaut celui de la configuration.
        kind = entry.get("type") or (
            b.model_types.get(plain, "text-generation") if b else "")
        if every or kind in CHAT_TYPES or (images and kind == IMAGE_TYPE):
            data.append({"id": entry["id"], "type": kind,
                         # Pour le générateur de configuration.
                         "context": entry.get("max_context_length"),
                         "chat": kind in CHAT_TYPES,
                         "vision": kind == "image-text-to-text",
                         "image": kind == IMAGE_TYPE})
    return {"object": "list", "data": data}


@router.post("/ui/chat/completions")
async def ui_chat(request: Request):
    """/v1/chat/completions sous /ui pour le cookie, compté à la personne
    connectée. Les outils hébergés y sont ceux que la page DÉCLARE
    ([chat].always ne s'y ajoute pas), leur exécution annoncée dans le flux
    (chat_api.Context.progress)."""
    if not UI_CHAT:
        return error_response("openai", 404, "unknown_route",
                              "chat de la page désactivé ([ui].chat)")
    if (bad := json_only(request)) is not None:
        return bad
    incognito(request)
    return await chat(request, page=True)


def incognito(request: Request) -> None:
    """Une requête du chat de la page marquée `X-Incognito: 1` : rien
    d'elle n'est gardé au-delà de la réponse (settings.INCOGNITO)."""
    if request.headers.get("x-incognito") == "1":
        INCOGNITO.set(True)


@router.post("/ui/images/generations")
async def ui_images(request: Request):
    """/v1/images/generations sous /ui pour le cookie, quand le modèle du
    chat est un modèle d'images. Relayée telle quelle."""
    if not UI_CHAT:
        return error_response("openai", 404, "unknown_route",
                              "chat de la page désactivé ([ui].chat)")
    if (bad := json_only(request)) is not None:
        return bad
    incognito(request)
    return await forward_post(request, "/v1/images/generations", "openai")


# La voix du chat (synthèse), relayée comme /v1/audio/*, comptée à la clé
# connectée.
def _chat_closed():
    if not UI_CHAT:
        return error_response("openai", 404, "unknown_route",
                              "chat de la page désactivé ([ui].chat)")
    return None


@router.post("/ui/audio/speech")
async def ui_speech(request: Request):
    """JSON (model, input, voice) : /v1/audio/speech, l'audio relayé."""
    if (bad := _chat_closed()) is not None:
        return bad
    if (bad := json_only(request)) is not None:
        return bad
    incognito(request)
    return await forward_post(request, "/v1/audio/speech", "openai")


@router.get("/ui/audio/voices")
async def ui_voices(request: Request):
    """?model=<backend>/<modèle> : les voix d'un modèle de synthèse."""
    if (bad := _chat_closed()) is not None:
        return bad
    return await relay_routes.audio_voices(request)


DATA_URL = re.compile(r"data:(image/[\w.+-]+);base64,([A-Za-z0-9+/=\s]+)\Z")


@router.post("/ui/images/edits")
async def ui_images_edits(request: Request):
    """La retouche d'une image du chat : un corps JSON (`model`, `prompt`,
    `image` en data URL, `size`) converti en formulaire /v1/images/edits et
    relayé comme lui. JSON et non formulaire : une page tierce ne peut pas
    l'envoyer (json_only)."""
    if not UI_CHAT:
        return error_response("openai", 404, "unknown_route",
                              "chat de la page désactivé ([ui].chat)")
    if (bad := json_only(request)) is not None:
        return bad
    incognito(request)
    payload = parse_json(await request.body())
    if not isinstance(payload, dict):
        return error_response("openai", 400, "invalid_request_error",
                              "corps JSON attendu")
    found = DATA_URL.match(str(payload.get("image") or ""))
    prompt = payload.get("prompt")
    if found is None or not isinstance(prompt, str) or not prompt.strip():
        return error_response("openai", 400, "invalid_request_error",
                              "`prompt` et `image` (data URL d'une image) "
                              "attendus")
    try:
        image = base64.b64decode(found.group(2))
    except (binascii.Error, ValueError):
        return error_response("openai", 400, "invalid_request_error",
                              "`image` illisible")
    kind = files.sniff(image) or ("image/webp" if image[8:12] == b"WEBP"
                                  else None)
    if kind is None:
        return error_response("openai", 400, "invalid_request_error",
                              "`image` n'est ni PNG, ni JPEG, ni GIF, ni WebP")
    model_key = str(payload.get("model") or "")
    backend, prefixed = route_backend({"model": model_key})
    if backend is None:
        return error_response("openai", 400, "unknown_backend_prefix",
                              unknown_prefix_message(model_key))
    fields = {"model": model_key[len(backend.name) + 1:] if prefixed
              else model_key, "prompt": prompt.strip()}
    for name in ("size", "n"):
        if payload.get(name) not in (None, ""):
            fields[name] = str(payload[name])
    form = httpx2.Request("POST", "http://form.invalid", data=fields, files={
        "image": ("image." + kind.split("/")[1], image, kind)})
    raw = form.read()
    # La requête relayée porte le type du formulaire, pas celui du JSON.
    headers = [(k, v) for k, v in request.scope["headers"]
               if k.lower() not in (b"content-type", b"content-length")]
    headers.append((b"content-type", form.headers["content-type"].encode()))
    relayed = Request({**request.scope, "headers": headers}, request.receive)
    call = Call(backend, model_key, "/v1/images/edits", "openai")
    # Le coût : le prompt, pas les octets de l'image.
    blocked = await gate(call, relayed, payload, albert.estimate_generic_cost(
        prompt.encode(), payload))
    if blocked is not None:
        return blocked
    return await forward(call, relayed, "v1/images/edits", raw)


@router.get("/ui/usage")
async def ui_usage(request: Request):
    """La route d'usage (usage_api), sous /ui pour le cookie. Auth active :
    les seules requêtes de la clé connectée."""
    return await usage_page(request, own=True)


@router.get("/ui/usage/tools")
async def ui_usage_tools(request: Request):
    """La route d'usage des outils, sous /ui pour la même raison."""
    return await usage_page(request, stats.usage_tools,
                            stats.TOOL_GROUP_FIELDS, own=True)
