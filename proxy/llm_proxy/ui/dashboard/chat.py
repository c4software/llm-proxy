"""
Le CHAT de la page et ce qu'il appelle : la session (ce que la page sait
d'elle), les modèles, /ui/chat/completions, les images, la voix, l'usage.
"""

import json
import os
import httpx2
from fastapi import Request
from ... import prompts, tools
from ...access import accounts, fairshare
from ...core import config
from ...core.settings import INCOGNITO
from ...storage import backends as user_backends, files, stats
from ...surfaces import chat_api, relay_routes
from ...upstream import catalog, quotas, relaying
from ...upstream.backends import no_route, route_backend, visible
from ...access.auth import auth_required, request_user, ui_client
from ...core.httputil import error_response, json_only, parse_json
from ...exposure import tool_routes
from ...surfaces.endpoints import chat, forward_post
from ...upstream.catalog import CHAT_TYPES, IMAGE_TYPE
from ...upstream.relaying import Call, forward, gate
from ..usage_api import usage_page

from .accounts import ui_login_mode
from .. import dataurl, linux_relay, page_tools
from .page import UI_CHAT, UI_CHAT_MODEL, router

# Tours d'outils de la page au plus, par réponse du chat (et questions à
# l'utilisateur au plus) : la page s'arrête au-delà.
MAX_ROUNDS = config.integer("ui.chat_max_rounds", 50, minimum=1)


def chat_tool_groups() -> list[dict]:
    """Ce que le chat de la page peut déclarer : un type par groupe
    (`{"type": "web_search"}` présente web_search, web_fetch et
    web_grep), avec les outils actifs qu'il présente. Relu à chaque
    appel : un serveur MCP découvert depuis y entre. `specs` : les
    fonctions que le proxy présentera au modèle pour ce groupe, que la
    page montre (le prompt qu'elle n'envoie pas) et compte en tokens."""
    groups: dict[str, list] = {}
    for tool in tools.Hosted(offer=tools.Offer.WEB).tools:
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


def _source() -> str:
    """L'adresse du dépôt de l'image (LLM_PROXY_SOURCE), http(s) seulement :
    la page en fait un lien."""
    url = (os.environ.get("LLM_PROXY_SOURCE") or "").strip().rstrip("/")
    return url if url.startswith(("https://", "http://")) else ""


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
        # Premier démarrage, sans data/config.toml : la page d'installation.
        "setup": config.SETUP,
        # La version de l'image (Dockerfile, LLM_PROXY_VERSION) ; hors
        # image, « dev ».
        "version": os.environ.get("LLM_PROXY_VERSION") or "dev",
        # Son dépôt (LLM_PROXY_SOURCE, posé par la CI) : la version y mène.
        "source": _source(),
        "auth_required": auth_required(),
        "login": ui_login_mode(),
        "account": user,
        # Un fragment du condensé, pour dire «c'est bien vous» sans rien
        # dire de la clé.
        "client": ui_client(request)[:8] or None,
        "chat": {
            "enabled": UI_CHAT,
            "model": UI_CHAT_MODEL or None,
            "max_rounds": MAX_ROUNDS,
            "hosted_tools": chat_tools,
            "tools": chat_tool_groups() if chat_tools else [],
            "always": chat_api.always_shown() if chat_tools else [],
            "image_sizes": list(tools.image_generation.SIZES),
            "image_size": tools.image_generation.SIZE,
            # Les passes proposées par modèle (motif → liste) ; un modèle
            # qu'aucun motif ne couvre n'a pas le choix.
            "image_steps": tools.image_generation.STEPS,
            # Les LoRA proposés par modèle (motif → noms), même règle.
            "image_loras": tools.image_generation.LORAS,
            # « Agrandir » une image (une retouche à plus grande taille) ;
            # None : pas de bouton.
            "image_upscale": {
                "model": tools.image_generation.UPSCALE_MODEL,
                "factors": tools.image_generation.UPSCALE_FACTORS,
                "prompt": prompts.text("image_generation.upscale"),
                "max_pixels": tools.image_generation.UPSCALE_MAX_PIXELS,
                "strengths": tools.image_generation.UPSCALE_STRENGTHS,
            } if tools.image_generation.UPSCALE_MODEL else None,
            # Les outils du NAVIGATEUR : familles et déclarations, sans
            # ceux qui sont refusés ([ui].page_tools_disabled) ni ceux d'une
            # extension désactivée (ui/page_tools.py).
            "page": page_tools.declarations(),
            # Les textes que la page ajoute aux messages (le résumé, l'état
            # du moment…), qu'elle remplit elle-même (prompts.for_page).
            "prompts": prompts.for_page(),
            # Le relais réseau de la VM Linux ([ui].linux_relay).
            "linux_relay": linux_relay.allowed(user),
        },
        "memory": tools.memory.ENABLED,
        # Les webhooks du compte (Configuration → Webhooks).
        "webhooks": tools.webhook.ENABLED and UI_CHAT,
        # Les sources de dépôts du compte (Configuration → Dépôts).
        "repos": tools.repository.TOOL.enabled,
        # Les backends du compte (Configuration → Backends).
        "backends": user_backends.ENABLED,
        # L'exécuteur de code est configuré : la partie Exécution (ses bacs).
        "executor": bool(tools.code_execution.URL and tools.code_execution.TOKEN),
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
    seen = visible()
    for entry in entries:
        name, _, plain = str(entry.get("id", "")).partition("/")
        b = seen.get(name)
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


# Sites connus qu'un appel de programme peut porter.
MAX_KNOWN_SITES = 10_000


@router.post("/ui/tools/{name}")
async def ui_tool(name: str, request: Request):
    """/v1/tools/<nom> sous /ui pour le cookie : un outil hébergé appelé
    par la page elle-même — par un programme de `run_tools_program`
    (chat/tools/sandbox/programs.js). Mêmes droits, même borne par minute
    ([tools].direct_calls_per_minute), même réponse. La provenance des URL
    porte sur les sites que la conversation a fait connaître (`hosts` du
    corps, que le proxy a donnés à la page en fin de réponse :
    x_known_sites) et sur ceux que les appels du programme y ajoutent,
    rendus à chaque réponse (`hosts`). Corps : {"arguments": {...},
    "hosts": [...]}. Fermée aussi quand l'admin a
    refusé run_tools_program ([ui].page_tools_disabled) : une page
    modifiée ne la rouvre pas. Incognito : pas de mémoire persistante,
    comme /ui/chat/completions."""
    program = next(t for t in tools.PAGE if t.name == tools.ToolName.RUN_TOOLS_PROGRAM)
    if not (UI_CHAT and chat_api.ENABLED) or not page_tools.served(program):
        return error_response("openai", 404, "unknown_route",
                              "programmes d'outils de la page désactivés "
                              "([ui].chat, [chat].hosted_tools, "
                              "[ui].page_tools_disabled)")
    if (bad := json_only(request)) is not None:
        return bad
    incognito(request)
    if INCOGNITO.get() and name == tools.ToolName.MEMORY:
        return error_response("openai", 403, "tool_not_permitted",
                              "conversation incognito : pas de mémoire "
                              "persistante")
    body = parse_json(await request.body())
    body = body if isinstance(body, dict) else {}
    hosts = body.get("hosts")
    if not isinstance(hosts, list) or not isinstance(body.get("arguments"), dict):
        return error_response("openai", 400, "invalid_request_error",
                              "corps attendu : {\"arguments\": {...}, "
                              "\"hosts\": [les sites connus]}")
    if len(hosts) > MAX_KNOWN_SITES:
        return error_response("openai", 400, "invalid_request_error",
                              f"plus de {MAX_KNOWN_SITES} sites connus : la "
                              f"conversation en cite trop pour un programme")
    known = {h for h in hosts if isinstance(h, str) and 0 < len(h) <= 253}
    return await tool_routes.run_direct(request, name, "/ui/tools",
                                        body.get("arguments"), known,
                                        offer=tools.Offer.WEB)


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


# La voix du chat (synthèse, transcription), relayée comme /v1/audio/*,
# comptée à la clé connectée.
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


@router.post("/ui/images/edits")
async def ui_images_edits(request: Request):
    """La retouche d'une image du chat : un corps JSON (`model`, `prompt`,
    `image` en data URL, `size`, `steps`, `strength`, `loras`) converti en formulaire /v1/images/edits et
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
    image = dataurl.decode(payload.get("image"))
    prompt = payload.get("prompt")
    if image is None or not isinstance(prompt, str) or not prompt.strip():
        return error_response("openai", 400, "invalid_request_error",
                              "`prompt` et `image` (data URL d'une image) "
                              "attendus")
    kind = files.sniff(image) or ("image/webp" if image[8:12] == b"WEBP"
                                  else None)
    if kind is None:
        return error_response("openai", 400, "invalid_request_error",
                              "`image` n'est ni PNG, ni JPEG, ni GIF, ni WebP")
    fields = {"prompt": prompt.strip()}
    for name in ("size", "n", "steps", "strength"):
        if payload.get(name) not in (None, ""):
            fields[name] = str(payload[name])
    # `loras` : une liste, que gufo lit en texte JSON dans un formulaire.
    if isinstance(payload.get("loras"), list):
        fields["loras"] = json.dumps(payload["loras"])
    one = ("image", "image." + kind.split("/")[1], image, kind)
    # Sous une force de 1, gufo prend la première image comme image de
    # DÉPART et les suivantes comme références : seule, elle n'est plus
    # qu'un départ bruité, et le modèle redessine d'après la consigne
    # (« Agrandir » à 0,6 a perdu le canoë de l'image, 17/11/2026). Elle est
    # donc envoyée deux fois, départ puis référence (image[]).
    images = [one]
    if "strength" in fields:
        images = [("image[]", *one[1:]), ("image[]", *one[1:])]
    # Le coût : le prompt, pas les octets de l'image.
    return await _relay_form(request, payload, fields, images,
                             "/v1/images/edits", prompt.encode())


async def _relay_form(request: Request, payload: dict, fields: dict,
                      file: tuple | list, normalized: str, cost: bytes):
    """Un corps JSON du chat relayé en FORMULAIRE (multipart) à la route
    `normalized` du backend que nomme son `model` : préfixe retiré, porte
    de quota (coût estimé sur `cost`), flux. `file` : (champ, nom,
    octets, type), ou une liste de tels fichiers."""
    model_key = str(payload.get("model") or "")
    backend = route_backend({"model": model_key})
    if backend is None:
        return no_route("openai", model_key)
    form = httpx2.Request("POST", "http://form.invalid", data={
        "model": model_key[len(backend.name) + 1:],
        **fields}, files=[(field, (name, data, kind)) for field, name, data, kind
                          in (file if isinstance(file, list) else [file])])
    raw = form.read()
    # La requête relayée porte le type du formulaire, pas celui du JSON.
    headers = [(k, v) for k, v in request.scope["headers"]
               if k.lower() not in (b"content-type", b"content-length")]
    headers.append((b"content-type", form.headers["content-type"].encode()))
    relayed = Request({**request.scope, "headers": headers}, request.receive)
    call = Call(backend, model_key, normalized, "openai")
    blocked = await gate(call, relayed, payload,
                         quotas.estimate_generic_cost(cost, payload))
    if blocked is not None:
        return blocked
    return await forward(call, relayed, normalized.lstrip("/"), raw)


@router.post("/ui/audio/transcriptions")
async def ui_transcriptions(request: Request):
    """La transcription d'un audio du chat (modèle de transcription choisi
    dans la conversation) : un corps JSON (`model`, `file` en data URL,
    `language` et `prompt` au choix) converti en formulaire
    /v1/audio/transcriptions. JSON pour la même raison que la retouche."""
    if (bad := _chat_closed()) is not None:
        return bad
    if (bad := json_only(request)) is not None:
        return bad
    incognito(request)
    payload = parse_json(await request.body())
    if not isinstance(payload, dict):
        return error_response("openai", 400, "invalid_request_error",
                              "corps JSON attendu")
    data = dataurl.decode(payload.get("file"))
    if data is None:
        return error_response("openai", 400, "invalid_request_error",
                              "`file` (data URL d'un audio) attendu")
    kind = files.sniff_audio(data)
    if kind is None:
        return error_response("openai", 400, "invalid_request_error",
                              "`file` n'est ni WAV, ni MP3, ni Ogg, ni FLAC, "
                              "ni WebM, ni MP4")
    fields = {name: str(payload[name]) for name in ("language", "prompt")
              if isinstance(payload.get(name), str) and payload[name].strip()}
    ext = {"audio/mpeg": "mp3", "audio/mp4": "m4a"}.get(kind, kind.split("/")[1])
    # Le coût : le prompt, pas les octets de l'audio.
    return await _relay_form(request, payload, fields, (
        "file", "audio." + ext, data, kind), "/v1/audio/transcriptions",
        fields.get("prompt", "").encode())


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
