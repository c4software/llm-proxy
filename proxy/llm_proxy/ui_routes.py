"""
Le tableau de bord, sous /ui : la page (statique, versionnée), son
service worker, l'entrée (clé ou OIDC) et la sortie, la session, les
comptes et leurs clés, le chat de la page (qui EST /v1/chat/completions
sous la session), les images, les statistiques de la personne connectée
et sa mémoire. Le doublon de certaines routes de l'API sous /ui n'existe
que pour l'auth : un fetch de navigateur porte la session, pas d'en-tête
Authorization.
"""

import asyncio
import base64
import binascii
import hashlib
import os
import re
import shutil
import tempfile
import uuid
from urllib.parse import quote

import httpx2
from fastapi import APIRouter, Request, Response
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, RedirectResponse,
)
from starlette.background import BackgroundTask

from . import accounts
from . import albert
from . import auth
from . import backup
from . import catalog
from . import chat_api
from . import config
from . import fairshare
from . import files
from . import memories
from . import oidc
from . import relaying
from . import stats
from . import tools
from .backends import BACKENDS, route_backend, unknown_prefix_message
from .settings import INCOGNITO, log
from .auth import (
    LEGACY_COOKIE, UI_PREFIX, auth_required, request_user,
    scope_https, toml_digests, ui_client, valid_key,
)
from .catalog import CHAT_TYPES, IMAGE_TYPE, always_shown
from .httputil import error_response, json_only, parse_json
from .relaying import Call, forward, gate
from .surfaces import chat, forward_post
from .usage_api import usage_page


router = APIRouter()

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
# La page du tableau de bord : du HTML statique, sans moteur de template —
# tout est rempli par dashboard.js à partir de l'Usage API.
UI_HTML = os.path.join(WEB_DIR, "templates", "index.html")


def _static_version() -> str:
    """Empreinte des fichiers de web/static, posée en «?v=» sur leurs URL
    dans la page. Le gabarit et dashboard.js vont ENSEMBLE : après une mise
    à jour, un navigateur qui gardait l'ancien script en cache recevait le
    nouveau gabarit, dont les variables n'existaient pas encore — page
    blanche jusqu'au rechargement forcé. Une URL qui change avec le contenu
    ne peut pas sortir du cache par erreur."""
    h = hashlib.blake2b(digest_size=6)
    static = os.path.join(WEB_DIR, "static")
    for name in sorted(os.listdir(static)):
        st = os.stat(os.path.join(static, name))
        h.update(f"{name}:{st.st_size}:{st.st_mtime_ns};".encode())
    return h.hexdigest()


UI_VERSION = _static_version()
# Le chat du tableau de bord (POST /ui/chat/completions) : le cookie de la
# page y DÉPENSE des tokens. Absent du TOML = inactif — sans lui, le cookie
# ne sert qu'à lire (statistiques, mémoire).
UI_CHAT = config.flag("ui.chat", False)
# Le modèle présélectionné dans le chat de la page (<backend>/<modèle>) :
# chaque nouvelle conversation part sur lui, s'il est au catalogue. Vide :
# le dernier modèle choisi dans ce navigateur, sinon le premier.
UI_CHAT_MODEL = config.text("ui.chat_model", "").strip()


@router.get("/ui", response_class=HTMLResponse)
async def ui_page(request: Request):
    """Tableau de bord : page statique. Les chiffres sont lus côté client
    sur /ui/usage, toutes les 5 s, sans jamais recharger la page."""
    with open(UI_HTML, encoding="utf-8") as fh:
        html = re.sub(r'(/ui/static/[\w.-]+)(?=")', rf"\1?v={UI_VERSION}",
                      fh.read())
    # La page elle-même n'est jamais gardée : c'est elle qui porte les URL
    # versionnées de ses fichiers.
    response = HTMLResponse(html, headers={"Cache-Control": "no-cache"})
    return response


def drop_legacy_cookie(response: Response, request: Request) -> None:
    """Efface l'ancien cookie de /ui (la clé en clair), s'il traîne."""
    if LEGACY_COOKIE in request.cookies:
        response.delete_cookie(LEGACY_COOKIE, httponly=True,
                               samesite="strict",
                               secure=scope_https(request.scope))


# Le service worker de l'application installable (manifest.webmanifest).
# Il ne garde RIEN : chaque requête va au proxy comme avant ; seule une
# navigation qui échoue (proxy injoignable) reçoit une page « hors ligne ».
# Servi sous /ui (pas /ui/static) avec Service-Worker-Allowed, pour que sa
# portée couvre /ui lui-même.
SERVICE_WORKER = """// llm-proxy : service worker de l'application installable.
const OFFLINE = `<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>llm-proxy — hors ligne</title>
<body style="margin:0;min-height:100vh;display:grid;place-items:center;
font:16px system-ui,sans-serif;background:#1a1917;color:#f2efe8">
<div style="text-align:center;padding:24px"><p style="font:28px Georgia,serif;margin:0 0 8px">
Proxy injoignable</p><p style="color:#9c968b;margin:0 0 18px">Vérifiez la connexion, puis réessayez.</p>
<button onclick="location.reload()" style="font:inherit;padding:8px 16px;border-radius:8px;
border:1px solid #e08b6d;background:#e08b6d;color:#fff">Réessayer</button></div>`;
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (event) => {
  if (event.request.mode !== "navigate") return;
  event.respondWith(fetch(event.request).catch(() => new Response(OFFLINE,
    {headers: {"content-type": "text/html; charset=utf-8"}})));
});
"""


@router.get("/ui/sw.js")
async def ui_service_worker():
    return Response(SERVICE_WORKER, media_type="text/javascript",
                    headers={"Service-Worker-Allowed": UI_PREFIX,
                             "Cache-Control": "no-cache"})


@router.post("/ui/login")
async def ui_login(request: Request):
    """La clé saisie dans la page : vérifiée, puis son CONDENSÉ gardé dans
    la session de la page (jamais la clé elle-même)."""
    if (bad := json_only(request)) is not None:
        return bad
    if oidc.ENABLED:
        return error_response("openai", 404, "unknown_route",
                              "la page s'ouvre par OIDC (GET /ui/auth/login)")
    body = parse_json(await request.body())
    key = body.get("key") if isinstance(body, dict) else None
    if not auth.PROXY_API_KEYS:
        return {"ok": True}
    if not isinstance(key, str) or not key or not valid_key(key):
        log.warning("/ui/login : clé invalide → 401")
        return error_response("openai", 401, "invalid_api_key",
                              "clé du proxy invalide")
    request.session.clear()
    request.session["kd"] = tools.owner(key)
    response = JSONResponse({"ok": True})
    drop_legacy_cookie(response, request)
    return response


@router.post("/ui/logout")
async def ui_logout(request: Request):
    if (bad := json_only(request)) is not None:
        return bad
    response = JSONResponse({"ok": True})
    drop_legacy_cookie(response, request)
    if "session" in request.scope:
        request.session.clear()
    return response


# ── Comptes OIDC (oidc.py, accounts.py) ─────────────────────────────────

def ui_login_mode() -> str | None:
    if oidc.ENABLED:
        return "oidc"
    return "key" if auth.PROXY_API_KEYS else None


@router.get("/ui/auth")
async def ui_auth():
    """Ce que la page doit proposer pour entrer, sans clé ni session."""
    return {"mode": ui_login_mode(),
            "label": oidc.LABEL if oidc.ENABLED else None}


@router.get("/ui/auth/login")
async def ui_auth_login(request: Request):
    """Vers le fournisseur OIDC (Authlib : découverte, PKCE, state,
    nonce, gardés dans la session jusqu'au retour)."""
    if not oidc.ENABLED:
        return error_response("openai", 404, "unknown_route",
                              "OIDC inactif ([oidc].issuer, client_id)")
    try:
        return await oidc.client().authorize_redirect(
            request, oidc.redirect_uri(str(request.base_url)))
    except Exception as exc:          # fournisseur injoignable, découverte
        log.warning("OIDC : connexion impossible : %s", exc)
        return RedirectResponse(
            "/ui?auth_error=" + quote("fournisseur OIDC injoignable"), 303)


@router.get(oidc.CALLBACK_PATH)
async def ui_auth_callback(request: Request):
    """Le retour du fournisseur : Authlib échange le code et valide
    l'id_token ; le compte (iss, sub) est créé ou retrouvé, et la session
    ne porte plus que lui."""
    if not oidc.ENABLED:
        return error_response("openai", 404, "unknown_route", "OIDC inactif")
    try:
        token = await oidc.client().authorize_access_token(request)
        info = token.get("userinfo") or {}
        user = await asyncio.to_thread(
            accounts.login, info.get("iss", ""), info.get("sub", ""),
            info.get("email") or "",
            info.get("name") or info.get("preferred_username") or "")
    except (oidc.OAuthError, accounts.AccountError) as exc:
        log.warning("OIDC : retour refusé : %s", exc)
        return RedirectResponse(
            "/ui?auth_error=" + quote(f"connexion refusée ({exc})"), 303)
    except Exception as exc:          # jeton invalide, fournisseur muet
        log.warning("OIDC : retour en échec : %r", exc)
        return RedirectResponse(
            "/ui?auth_error=" + quote("connexion impossible"), 303)
    request.session.clear()
    request.session["uid"] = user["id"]
    log.info("OIDC : connexion du compte %d (%s, %s)", user["id"],
             user["email"] or user["name"], user["role"])
    return RedirectResponse("/ui", 303)


def accounts_off() -> JSONResponse | None:
    if not oidc.ENABLED:
        return error_response("openai", 404, "unknown_route",
                              "comptes inactifs ([oidc] dans config.toml)")
    return None


def admin_only(request: Request) -> JSONResponse | None:
    if (off := accounts_off()) is not None:
        return off
    if (request_user(request) or {}).get("role") != "admin":
        return error_response("openai", 403, "forbidden", "réservé aux admins")
    return None


@router.get("/ui/admin/backup")
async def ui_backup(request: Request):
    """Une sauvegarde de data/, à télécharger (admin) : la même archive que
    `python -m llm_proxy.backup` — les trois bases copiées à chaud,
    client.secret et config.toml —, faite à la demande dans un dossier
    temporaire, effacé une fois l'archive partie. Réservée aux admins des
    comptes OIDC : elle contient le secret des condensés et la mémoire de
    tous ; sans comptes, pas d'admin, pas de téléchargement."""
    if (bad := admin_only(request)) is not None:
        return bad
    tmp = tempfile.mkdtemp(prefix="llm-proxy-backup-")
    try:
        path = await asyncio.to_thread(backup.backup, tmp, 0)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    user = request_user(request)
    log.info("sauvegarde téléchargée par le compte %d (%s)", user["id"],
             user["email"] or user["name"])
    return FileResponse(
        path, media_type="application/gzip",
        filename=os.path.basename(path),
        headers={"Cache-Control": "no-store"},
        background=BackgroundTask(shutil.rmtree, tmp, True))


def account_error(exc: accounts.AccountError) -> JSONResponse:
    return error_response("openai", exc.status, "invalid_request_error",
                          str(exc))


@router.get("/ui/keys")
async def ui_keys(request: Request, all: int = 0):
    """Les clés du compte connecté, et celles du TOML qui lui sont
    rattachées ; `?all=1` (admin) : celles de tous, et toutes celles du
    TOML. Une clé du TOML ne se montre que par son condensé."""
    if (off := accounts_off()) is not None:
        return off
    user = request_user(request)
    if all and (bad := admin_only(request)) is not None:
        return bad
    data = await asyncio.to_thread(accounts.keys, None if all else user["id"])
    links = await asyncio.to_thread(accounts.bindings)
    toml = [{"api_key_id": d, "hint": d[:8], **links.get(
                d, {"user_id": None, "user": None})}
            for d in sorted(toml_digests())]
    if not all:
        toml = [k for k in toml if k["user_id"] == user["id"]]
    return {"object": "list", "data": data, "toml": toml}


@router.put("/ui/keys/toml/{digest}")
async def ui_toml_bind(request: Request, digest: str):
    """Rattache une clé du TOML (désignée par son condensé) à un compte,
    `{"user_id": <id>}`, ou l'en détache, `{"user_id": null}` (admin).
    Ce qui était rangé sous la clé seule n'est pas déplacé."""
    if (bad := admin_only(request)) is not None:
        return bad
    if (bad := json_only(request)) is not None:
        return bad
    if digest not in toml_digests():
        return error_response("openai", 404, "not_found",
                              "pas une clé de proxy.api_keys")
    body = parse_json(await request.body())
    uid = body.get("user_id") if isinstance(body, dict) else 0
    if uid is not None and (not isinstance(uid, int) or isinstance(uid, bool)):
        return error_response("openai", 400, "invalid_request_error",
                              "corps attendu : {\"user_id\": <id> | null}")
    try:
        await asyncio.to_thread(accounts.bind, digest, uid)
    except accounts.AccountError as exc:
        return account_error(exc)
    links = await asyncio.to_thread(accounts.bindings)
    return {"api_key_id": digest, "hint": digest[:8],
            **links.get(digest, {"user_id": None, "user": None})}


@router.post("/ui/keys")
async def ui_key_create(request: Request):
    """Une nouvelle clé pour le compte connecté : un uuid4, rendu ICI
    seulement (`key`) — le proxy n'en garde que le condensé."""
    if (off := accounts_off()) is not None:
        return off
    if (bad := json_only(request)) is not None:
        return bad
    body = parse_json(await request.body())
    label = body.get("label", "") if isinstance(body, dict) else ""
    if not isinstance(label, str):
        return error_response("openai", 400, "invalid_request_error",
                              "label : une chaîne")
    key = str(uuid.uuid4())
    try:
        record = await asyncio.to_thread(
            accounts.create_key, request_user(request)["id"], key,
            tools.owner(key), label)
    except accounts.AccountError as exc:
        return account_error(exc)
    return record | {"key": key}


@router.delete("/ui/keys/{kid}")
async def ui_key_revoke(request: Request, kid: str):
    """Révoque une clé : du compte connecté, ou de n'importe qui (admin)."""
    if (off := accounts_off()) is not None:
        return off
    user = request_user(request)
    try:
        return await asyncio.to_thread(
            accounts.revoke_key, kid,
            None if user["role"] == "admin" else user["id"])
    except accounts.AccountError as exc:
        return account_error(exc)


@router.get("/ui/users")
async def ui_users(request: Request):
    if (bad := admin_only(request)) is not None:
        return bad
    return {"object": "list", "data": await asyncio.to_thread(accounts.users)}


@router.patch("/ui/users/{uid}")
async def ui_user_update(request: Request, uid: int):
    """Un compte (admin) : son rôle (`role` : accepter un compte en
    attente, le bloquer, le promouvoir ; le dernier admin garde le sien)
    et/ou sa part dans le partage équitable (`share` : null = la part par
    défaut, 0 = illimitée, sinon des tokens par heure glissante)."""
    if (bad := admin_only(request)) is not None:
        return bad
    if (bad := json_only(request)) is not None:
        return bad
    body = parse_json(await request.body())
    if not isinstance(body, dict) or not ({"role", "share"} & body.keys()):
        return error_response("openai", 400, "invalid_request_error",
                              "corps attendu : {\"role\": …} et/ou "
                              "{\"share\": null | 0 | <tokens par heure>}")
    try:
        user = None
        if "role" in body:
            user = await asyncio.to_thread(accounts.set_role, uid, body["role"])
        if "share" in body:
            user = await asyncio.to_thread(accounts.set_share, uid,
                                           body["share"])
        return user
    except accounts.AccountError as exc:
        return account_error(exc)


def chat_tool_groups() -> list[dict]:
    """Ce que le chat de la page peut déclarer : un type par groupe
    (`{"type": "web_search"}` présente web_search, web_fetch et
    web_grep), avec les outils actifs qu'il présente. Relu à chaque
    appel : un serveur MCP découvert depuis y entre."""
    groups: dict[str, list[str]] = {}
    for tool in tools.Hosted().tools:
        if tool.kinds:
            # Un outil MCP se déclare par son nom ou par son serveur
            # (`mcp:<serveur>`) : un interrupteur par SERVEUR, pas par outil.
            kind = next((k for k in tool.kinds
                         if k.startswith(tools.mcp.KIND + ":")), tool.kinds[0])
            groups.setdefault(kind, []).append(tool.name)
    return [{"type": kind, "tools": names} for kind, names in groups.items()]


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
        # Le compte OIDC connecté (rôle compris), None sans comptes.
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
            # Les tailles qu'un modèle d'images peut recevoir du chat :
            # celles de l'outil image_generation.
            "image_sizes": list(tools.image_generation.SIZES),
            "image_size": tools.image_generation.SIZE,
        },
        # La mémoire persistante (outil `memory`) : son panneau, à côté du
        # chat comme dans les statistiques.
        "memory": tools.memory.ENABLED,
        # Le partage équitable : la part du client connecté (None =
        # illimitée) et ce qu'il en a consommé sur l'heure glissante.
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
    """Les modèles de CONVERSATION du catalogue (ni image, ni voix, ni
    embeddings — CHAT_TYPES), sous /ui pour le cookie ; `vision` : le
    catalogue le dit capable de lire une image. Pour le chat et pour le
    générateur de configuration : servi même chat fermé (c'est ce que
    /v1/models dit déjà à la même clé). `?all=1` : tout le catalogue, avec
    le type de chaque modèle — le générateur y choisit le modèle de l'OCR,
    de la transcription ou des images. `?images=1` : les modèles
    d'images en plus (`image`), que le chat sait faire générer."""
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
        # Le type que le catalogue normalisé publie (celui du backend, sinon
        # déduit du nom) ; à défaut, les corrections de la configuration.
        kind = entry.get("type") or (
            b.model_types.get(plain, "text-generation") if b else "")
        if every or kind in CHAT_TYPES or (images and kind == IMAGE_TYPE):
            data.append({"id": entry["id"], "type": kind,
                         # La fenêtre de contexte, si le backend la dit :
                         # le générateur la donne à Codex, qui l'ignore.
                         "context": entry.get("max_context_length"),
                         "vision": kind == "image-text-to-text",
                         "image": kind == IMAGE_TYPE})
    return {"object": "list", "data": data}


@router.post("/ui/chat/completions")
async def ui_chat(request: Request):
    """Le chat de la page : /v1/chat/completions, sous /ui pour le cookie
    — donc compté à la clé de la personne connectée. Les outils hébergés
    y sont ceux que la page DÉCLARE ([chat].always ne s'y ajoute pas :
    les interrupteurs de la page font foi), et leur exécution est
    annoncée dans le flux (chat_api.Context.progress)."""
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
    """Le chat de la page, quand son modèle est un modèle d'images :
    /v1/images/generations, sous /ui pour le cookie — comptée à la clé
    connectée, comme le chat. Relayée telle quelle : la page lit
    `b64_json` (ou une `url` absolue)."""
    if not UI_CHAT:
        return error_response("openai", 404, "unknown_route",
                              "chat de la page désactivé ([ui].chat)")
    if (bad := json_only(request)) is not None:
        return bad
    incognito(request)
    return await forward_post(request, "/v1/images/generations", "openai")


# Une image à retoucher, en data URL, du chat de la page.
DATA_URL = re.compile(r"data:(image/[\w.+-]+);base64,([A-Za-z0-9+/=\s]+)\Z")


@router.post("/ui/images/edits")
async def ui_images_edits(request: Request):
    """La retouche d'une image du chat de la page : un corps JSON
    (`model`, `prompt`, `image` en data URL, `size` facultatif) → le
    formulaire de /v1/images/edits, relayé comme lui (routage au préfixe,
    quota, statistiques à la clé connectée). JSON et non formulaire : une
    page tierce ne peut pas l'envoyer (json_only)."""
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
    """La route d'Usage API ci-dessus, mais sous /ui : le cookie posé par
    la page y suffit comme authentification (un fetch de navigateur ne
    peut pas porter le Bearer). Auth active : les seules requêtes de la
    clé connectée."""
    return await usage_page(request, own=True)


@router.get("/ui/usage/tools")
async def ui_usage_tools(request: Request):
    """La route d'usage des outils, sous /ui pour la même raison."""
    return await usage_page(request, stats.usage_tools,
                            stats.TOOL_GROUP_FIELDS, own=True)


# ── Mémoire, sous /ui ───────────────────────────────────────────────────
# Ce que l'outil `memory` a gardé pour le client CONNECTÉ (la session de
# la page, ou l'en-tête) : la liste, un fichier, l'écriture, la
# suppression, le renommage. Jamais la mémoire d'un autre. Les écritures
# exigent un corps JSON (json_only) : une page tierce ne peut pas les
# déclencher.

def memory_disabled() -> JSONResponse | None:
    if not tools.memory.ENABLED:
        return error_response("openai", 404, "unknown_route",
                              "mémoire désactivée ([tools.memory].enabled)")
    return None


def memory_call(fn, *args):
    """Une opération du magasin → (résultat, None) ou (None, réponse 400)."""
    try:
        return fn(*args), None
    except memories.MemoryError_ as exc:
        return None, error_response("openai", 400, "invalid_request_error", str(exc))


@router.get("/ui/memory")
async def ui_memory_list(request: Request):
    if (off := memory_disabled()) is not None:
        return off
    owner = ui_client(request)
    files = await asyncio.to_thread(memories.list_files, owner)
    return {"object": "list", "data": files,
            "usage": await asyncio.to_thread(memories.usage, owner)}


@router.get("/ui/memory/file")
async def ui_memory_read(request: Request, path: str = ""):
    if (off := memory_disabled()) is not None:
        return off
    content, bad = await asyncio.to_thread(
        memory_call, memories.read, ui_client(request), path)
    if bad is not None:
        return bad
    if content is None:
        return error_response("openai", 404, "not_found", f"{path} n'existe pas")
    return {"path": memories.normalize(path), "content": content}


@router.put("/ui/memory/file")
async def ui_memory_write(request: Request):
    if (off := memory_disabled()) is not None:
        return off
    body = parse_json(await request.body())
    if not isinstance(body, dict):
        return error_response("openai", 400, "invalid_request_error",
                              "corps JSON attendu : {path, content}")
    existed, bad = await asyncio.to_thread(
        memory_call, memories.write, ui_client(request),
        body.get("path"), body.get("content"))
    if bad is not None:
        return bad
    return {"path": memories.normalize(body.get("path")), "created": not existed}


@router.delete("/ui/memory/file")
async def ui_memory_delete(request: Request, path: str = ""):
    if (off := memory_disabled()) is not None:
        return off
    n, bad = await asyncio.to_thread(
        memory_call, memories.delete, ui_client(request), path)
    if bad is not None:
        return bad
    if not n:
        return error_response("openai", 404, "not_found", f"{path} n'existe pas")
    return {"deleted": n}


@router.post("/ui/memory/rename")
async def ui_memory_rename(request: Request):
    if (off := memory_disabled()) is not None:
        return off
    body = parse_json(await request.body())
    if not isinstance(body, dict):
        return error_response("openai", 400, "invalid_request_error",
                              "corps JSON attendu : {old_path, new_path}")
    n, bad = await asyncio.to_thread(
        memory_call, memories.rename, ui_client(request),
        body.get("old_path"), body.get("new_path"))
    if bad is not None:
        return bad
    return {"renamed": n, "path": memories.normalize(body.get("new_path"))}
