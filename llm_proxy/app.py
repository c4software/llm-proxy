"""
L'application FastAPI : routes, middleware d'authentification, relais.

Rôles :
  1. peut injecter tool_choice quand `tools` est présent sans `tool_choice`
     (le défaut du schéma Albert est "none", ce qui casse le tool calling).
     RIEN N'EST INJECTÉ par défaut : cela s'active PAR BACKEND via
     [backends.<nom>].force_tool_choice ;
  2. porte les clés upstream pour tous les clients ;
  3. multi-backends, routés par le PRÉFIXE du modèle — voir backends.py.
     GET /v1/models interroge les backends en direct et fusionne les
     catalogues, chaque id exposé préfixé ;
  4. ne transmet QUE les routes nécessaires (proxy.forward_post_paths,
     /v1/chat/completions, /v1/models) — toute autre URL reçoit un 404
     local, rien n'est relayé aveuglément aux backends ;
  4bis. GET /v1/organization/usage/completions : l'Usage API d'OpenAI,
     servie depuis les compteurs du proxy (SQLite, une ligne par requête
     — voir stats.py). C'est la SEULE lecture des statistiques. GET /ui
     en est le tableau de bord — une page HTML statique
     (web/templates/index.html) que web/static/dashboard.js remplit en
     appelant cette même route sous /ui/usage, en vues All / Week / Day ;
  4ter. la surface Anthropic (anthropic_api.py), si [anthropic].enabled :
     POST /v1/messages et /v1/messages/count_tokens, et GET /v1/models à
     la forme Anthropic quand la requête porte `anthropic-version`. Un
     client Claude Code s'y branche avec ANTHROPIC_BASE_URL ;
  5. auth optionnelle du proxy lui-même : proxy.api_keys (liste vide par
     défaut = ouvert) exige des clients un «Authorization: Bearer <clé>»
     à la OpenAI — ou «x-api-key: <clé>», à l'Anthropic — (401 sinon,
     /healthz exempté). C'est la clé DU PROXY : jamais relayée aux
     backends quand l'auth est active.

Chaque requête relayée est portée par UN objet `Call` (backend visé,
modèle demandé, endpoint, dialecte du client) : c'est lui qui écrit la
ligne de stats — une fois, quel que soit le chemin de sortie (relais,
429 local, client parti, backend éteint) — et qui construit les erreurs
à la forme que le client attend. La porte de quota (`gate`) et le relais
(`forward`) sont les mêmes pour toutes les routes.

TOUTE la configuration vit dans data/config.toml (voir config.py).
Tout ce qui est spécifique à Albert (limiteur de quotas, familles,
association routeurs ↔ modèles) vit dans albert.py.
"""

import asyncio
import hmac
import json
import os
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import (
    HTMLResponse, JSONResponse, RedirectResponse,
    StreamingResponse,
)
from starlette.concurrency import run_in_threadpool

from . import albert
from . import anthropic_api
from . import config
from . import stats
from .backends import (
    BACKENDS, Backend, FALLBACK_BACKEND, backend_offline_message,
    close_clients, open_clients, route_backend, strip_backend_prefix,
    unknown_prefix_message,
)
from .settings import (
    EXEMPT_SUFFIXES, FORWARD_POST_PATHS, HOP_BY_HOP, PROXY_API_KEYS,
    TOOL_CHOICE, is_exempt, log,
)

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("configuration : %s", config.CONFIG_PATH)
    stats.init()
    await open_clients()

    for name, b in BACKENDS.items():
        log.info(
            "backend %s%s → %s | %s | clé : %s",
            name, " (fallback sans modèle)" if b is FALLBACK_BACKEND else "",
            b.url,
            "quotas (limiteur)" if b.quotas
            else "sans quota, contacté à la demande",
            "injectée" if b.api_key else "aucune (Authorization du client "
            "transmis tel quel)",
        )
        log.info(
            "backend %s : %s%s",
            name,
            f"tool_choice={b.tool_choice!r} injecté si absent"
            if b.tool_choice else "aucune injection de tool_choice",
            f" | max_tokens plafonné à {b.max_tokens}" if b.max_tokens else "",
        )
    if PROXY_API_KEYS:
        log.info(
            "auth proxy ACTIVE : %d clé(s) acceptée(s), /healthz exempté",
            len(PROXY_API_KEYS),
        )
    else:
        log.info("auth proxy inactive (proxy.api_keys vide) : proxy ouvert")
    if anthropic_api.ENABLED:
        log.info(
            "surface Anthropic ACTIVE : POST /v1/messages, "
            "/v1/messages/count_tokens, GET /v1/models (anthropic-version) "
            "| model_map : %s",
            ", ".join(f"{k} → {v}" for k, v in anthropic_api.MODEL_MAP.items())
            or "vide (seuls les noms préfixés passent)",
        )
    else:
        log.info("surface Anthropic inactive ([anthropic].enabled absent "
                 "ou false) : /v1/messages → 404")
    if albert.ROUTER_MODELS:
        log.info("mapping manuel ROUTER_MODELS actif : %s", albert.ROUTER_MODELS)

    refresh_tasks = []
    for b in BACKENDS.values():
        if not b.quotas:
            continue
        if not b.api_key:
            log.warning(
                "backend %s sans api_key : 401 upstream probables", b.name,
            )
        got_account = await b.quota_state.refresh(b)
        if not got_account:
            log.info(
                "[%s] limites du compte indisponibles — familles "
                "statiques : %s",
                b.name,
                ", ".join(
                    f"{fam}({cfg['rpm']}rpm/{cfg['tpm']}tpm)"
                    for fam, cfg in albert.FAMILY_LIMITS.items()
                ),
            )
        elif albert.LIMITS_REFRESH > 0:
            refresh_tasks.append(
                asyncio.create_task(b.quota_state.refresh_loop(b))
            )
    log.info(
        "générique %d RPM / %d TPM | marge %.0f%% | attente max %ds | "
        "cartouche %.0fs | exemptés : %s",
        albert.GENERIC_RPM, albert.GENERIC_TPM, albert.MARGIN * 100,
        int(albert.MAX_QUEUE_SECONDS), albert.STATUS_INTERVAL,
        ", ".join(EXEMPT_SUFFIXES) or "aucun",
    )

    yield
    for task in refresh_tasks:
        task.cancel()
    await close_clients()
    stats.close()


app = FastAPI(title="albert-proxy", lifespan=lifespan)

# Feuille de style et script du tableau de bord : servis par le proxy
# lui-même, aucun CDN. Monté AVANT le catch-all, qui sinon répondrait
# 404. Le mount reste sous /ui : l'auth du proxy s'y applique.
app.mount("/ui/static", StaticFiles(directory=os.path.join(WEB_DIR, "static")),
          name="static")
# La page du tableau de bord : du HTML statique, sans moteur de template —
# tout est rempli par dashboard.js à partir de l'Usage API.
UI_HTML = os.path.join(WEB_DIR, "templates", "index.html")


UI_PREFIX = "/ui"
UI_COOKIE = "proxy_key"


def is_ui_path(path: str) -> bool:
    # «/» y est inclus : il ne fait que rediriger vers /ui, autant qu'un
    # navigateur déjà porteur du cookie y arrive sans repasser par ?key=.
    return path == "/" or path == UI_PREFIX or path.startswith(UI_PREFIX + "/")


def dialect_of(request: Request) -> str:
    """Forme des erreurs attendue par le client : «anthropic» si la
    requête vient d'un SDK Anthropic (il pose `anthropic-version` sur
    chaque appel) ou vise /v1/messages ; «openai» sinon."""
    if request.url.path.startswith("/v1/messages") \
            or "anthropic-version" in request.headers:
        return "anthropic"
    return "openai"


def client_token(request: Request) -> str:
    """Clé présentée par le client. Les clients API l'envoient en
    «Authorization: Bearer» (OpenAI) ou «x-api-key» (Anthropic) ; un
    NAVIGATEUR ne le peut pas sur une simple URL — les pages /ui
    acceptent donc «?key=…» (mémorisé ensuite en cookie), et rien
    d'autre ne change."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:]
    api_key = request.headers.get("x-api-key", "")
    if api_key:
        return api_key
    if is_ui_path(request.url.path):
        return (request.query_params.get("key")
                or request.cookies.get(UI_COOKIE, ""))
    return ""


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


@app.middleware("http")
async def require_proxy_key(request: Request, call_next):
    if PROXY_API_KEYS and request.url.path != "/healthz":
        token = client_token(request)
        if not any(hmac.compare_digest(token, k) for k in PROXY_API_KEYS):
            log.warning(
                "clé proxy absente ou invalide : %s %s → 401",
                request.method, request.url.path,
            )
            return error_response(
                dialect_of(request), 401, "invalid_api_key",
                "clé API du proxy absente ou invalide (en-tête "
                "«Authorization: Bearer <clé>» ou «x-api-key: <clé>» attendu)",
            )
    return await call_next(request)


# En-têtes du dialecte Anthropic : sans sens pour un backend OpenAI, et
# `x-api-key` porte la clé DU PROXY (ou une valeur bidon du client) —
# jamais relayés, auth active ou non.
ANTHROPIC_HEADERS = {"x-api-key", "anthropic-version", "anthropic-beta"}


def clean_headers(request: Request, api_key: str) -> dict:
    # Authorization du client jamais relayé si le backend a sa clé, ni si
    # l'auth proxy est active (le Bearer du client est la clé DU PROXY,
    # elle ne doit pas fuiter vers l'upstream).
    skip = HOP_BY_HOP | ANTHROPIC_HEADERS | (
        {"authorization"} if api_key or PROXY_API_KEYS else set()
    )
    # Auth proxy active : le cookie posé par /ui porte la clé DU PROXY —
    # même raison que l'Authorization, il ne doit pas fuiter en amont.
    if PROXY_API_KEYS:
        skip = skip | {"cookie"}
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in skip
    }
    headers["accept-encoding"] = "identity"
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def response_headers(upstream: httpx.Response) -> dict:
    return {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in {"content-length", "content-encoding", "transfer-encoding", "connection"}
    }


def inject_tool_choice(payload: dict, b: Backend) -> bool:
    """Injecte `tool_choice` si le backend le demande et que le client
    n'en a pas mis. Un `tool_choice` explicite n'est jamais écrasé."""
    if b.tool_choice is None:
        return False
    tools = payload.get("tools")
    if isinstance(tools, list) and tools and payload.get("tool_choice") is None:
        payload["tool_choice"] = b.tool_choice
        return True
    return False


def cap_max_tokens(payload: dict, b: Backend) -> bool:
    """Ramène `max_tokens` / `max_completion_tokens` au plafond du
    backend ([backends.<nom>].max_tokens), s'il en a un. Jamais
    augmenté, jamais ajouté s'il est absent."""
    if not b.max_tokens:
        return False
    changed = False
    for key in ("max_tokens", "max_completion_tokens"):
        value = payload.get(key)
        if isinstance(value, int) and value > b.max_tokens:
            payload[key] = b.max_tokens
            changed = True
    return changed


# Convention nginx : le client a fermé la connexion avant la réponse.
CLIENT_CLOSED = 499


class Call:
    """Une requête relayée, du routage à la ligne de stats.

    Fixé au routage : le backend, le modèle PRÉFIXÉ tel que le client
    l'a demandé (la clé des stats — vide = rien n'est compté), l'endpoint
    et le dialecte du client. `done()` écrit la ligne UNE fois, quel que
    soit le chemin qui y mène ; `error()` la compte ET construit la
    réponse dans la forme que le client attend."""

    def __init__(self, backend: Backend, model_key: str, endpoint: str,
                 dialect: str = "openai"):
        self.backend = backend
        self.model_key = model_key
        self.endpoint = "/" + endpoint.strip("/")
        self.dialect = dialect
        self.started = time.monotonic()
        self._done = False

    @property
    def plain_model(self) -> str:
        b = self.backend.name
        if self.model_key.lower().startswith(b + "/"):
            return self.model_key[len(b) + 1:]
        return self.model_key

    def done(self, status: int, prompt_tokens: int = 0,
             completion_tokens: int = 0, exact: bool = False,
             streamed: bool = False, cached_tokens: int = 0) -> None:
        if self._done or not self.model_key:
            return
        self._done = True
        # Erreur sans `usage` upstream (500, 429 local, client parti…) :
        # rien n'a été consommé de mesurable. Compter le corps envoyé en
        # tokens «estimés» gonflait l'entrée de ~20 k tokens par erreur
        # de Claude Code et faisait passer ces lignes pour des mesures
        # approximatives — ce sont des zéros exacts.
        if status >= 400 and not exact:
            prompt_tokens = completion_tokens = cached_tokens = 0
            exact = True
        stats.record(self.model_key, self.backend.name, self.plain_model,
                     self.endpoint, status, time.monotonic() - self.started,
                     prompt_tokens, completion_tokens, exact, streamed,
                     cached_tokens)

    def error(self, status: int, type_: str, message: str,
              headers: dict | None = None) -> JSONResponse:
        self.done(status)
        return error_response(self.dialect, status, type_, message, headers)


async def wait_disconnect(request: Request) -> None:
    """Ne rend la main qu'à la déconnexion du client. Le corps ayant
    déjà été lu, la seule chose que `receive()` puisse encore livrer est
    `http.disconnect` — et il BLOQUE jusque-là, y compris à travers
    BaseHTTPMiddleware (où `request.is_disconnected()`, lui, sonde sous
    un délai si court qu'il ne voit jamais rien)."""
    while True:
        message = await request.receive()
        if message.get("type") == "http.disconnect":
            return


async def gate(call: Call, request: Request, payload, cost: int) -> Response | None:
    """La porte de quota, commune à toutes les routes. None = passer.
    Sinon la réponse à renvoyer, déjà comptée : 429 local si l'attente
    dépasserait MAX_QUEUE_SECONDS, 499 si le client a raccroché pendant
    l'attente (la requête n'est PAS partie à l'upstream, le quota est
    préservé — personne ne lira cette réponse)."""
    b = call.backend
    if not b.quotas or is_exempt(call.endpoint):
        return None
    limiter = b.quota_state.get_limiter(payload)  # model dé-préfixé
    try:
        waited = await limiter.acquire(cost, lambda: wait_disconnect(request))
    except albert.QuotaWaitTooLong as exc:
        return quota_error(call, exc, limiter)
    except albert.ClientGone:
        client_gone(call, limiter)
        return Response(status_code=CLIENT_CLOSED)
    released(call, limiter, waited, cost)
    return None


def quota_error(call: Call, exc: albert.QuotaWaitTooLong,
                limiter: albert.Limiter) -> JSONResponse:
    retry_after = max(int(exc.delay), 1)
    log.warning(
        "[%s] quota %s : attente %.0fs > MAX_QUEUE_SECONDS (%ds) → 429 local",
        limiter.name, exc.window_name, exc.delay,
        int(albert.MAX_QUEUE_SECONDS),
    )
    return call.error(
        429, "rate_limit_exceeded_proxy",
        f"quota {exc.window_name} épuisé pour «{limiter.name}» ; "
        f"réessayer dans ~{retry_after}s",
        headers={"Retry-After": str(retry_after)},
    )


def client_gone(call: Call, limiter: albert.Limiter) -> None:
    log.info("[%s] client parti pendant l'attente : %s abandonné, "
             "quota préservé", limiter.name, call.endpoint)
    call.done(CLIENT_CLOSED)


def released(call: Call, limiter: albert.Limiter, waited: float,
             cost: int) -> None:
    if waited:
        log.info(
            "[%s] %s relâché après %.1fs d'attente (~%d tokens)",
            limiter.name, call.endpoint, waited, cost,
        )


def default_tap(status: int, content_type: str):
    return stats.UsageCollector(content_type)


async def send_upstream(call: Call, request: Request, path: str,
                        body: bytes) -> httpx.Response | JSONResponse:
    """Envoie `body` vers `path` du backend de `call` ; rend la réponse
    upstream ouverte en flux, ou — backend injoignable — la réponse
    d'erreur locale, déjà comptée (503 parlant pour un backend local
    éteint, 502 sinon)."""
    b = call.backend
    albert.maybe_log_status()
    req = b.client.build_request(
        request.method,
        f"/{path}",
        content=body or None,
        headers=clean_headers(request, b.api_key),
        # Un client Anthropic ajoute «?beta=true» et consorts à SES URLs :
        # sans sens pour l'upstream. Un client OpenAI, lui, est relayé
        # tel quel.
        params=request.query_params if call.dialect == "openai" else None,
    )
    try:
        upstream = await b.client.send(req, stream=True)
    except httpx.RequestError as exc:
        # backend sans quota (local) : éteint fait partie du fonctionnement
        # normal → 503 parlant plutôt qu'un 502.
        if not b.quotas:
            return call.error(503, "backend_offline",
                              backend_offline_message(b, exc),
                              headers={"Retry-After": "30"})
        log.error("backend %s injoignable : %s", b.name, exc)
        return call.error(502, "proxy_error", f"upstream unreachable: {exc}")

    if upstream.status_code == 429 and b.quotas:
        log.warning(
            "429 reçu d'Albert malgré le limiteur — comparer /healthz avec "
            "/v1/me/info ; l'association routeur est peut-être fausse "
            "(fixer via ROUTER_MODELS)"
        )
    return upstream


# Corps d'une réponse d'erreur upstream retenu pour le log : assez pour
# lire le message, pas plus.
ERROR_EXCERPT = 600


async def relay(call: Call, upstream: httpx.Response, robinet,
                prompt_estimate: int):
    """Les octets upstream, passés par le robinet. Le `finally` compte
    aussi les flux interrompus (client parti) ; l'upstream est fermé
    quoi qu'il arrive. Une erreur upstream (4xx/5xx) est loggée avec le
    début de son corps — le client, lui, ne voit souvent qu'un statut."""
    failed = upstream.status_code >= 400
    excerpt = bytearray()
    try:
        async for chunk in upstream.aiter_raw():
            if failed and len(excerpt) < ERROR_EXCERPT:
                excerpt += chunk[:ERROR_EXCERPT - len(excerpt)]
            out = robinet.feed(chunk)
            if out:
                yield out
        out = robinet.finish()
        if out:
            yield out
    finally:
        if failed:
            log.warning(
                "[%s] %s → %d upstream pour %s : %s", call.backend.name,
                call.endpoint, upstream.status_code, call.model_key,
                excerpt.decode("utf-8", "replace").replace("\n", " "),
            )
        prompt, completion, exact = robinet.tokens(prompt_estimate)
        call.done(upstream.status_code, prompt, completion, exact,
                  robinet.sse, robinet.cached())
        await upstream.aclose()


async def forward(call: Call, request: Request, path: str, body: bytes,
                  tap=default_tap) -> Response:
    """Relaie `body` vers `path` du backend de `call` et rend la réponse
    en flux. `tap(status, content_type)` fabrique le robinet par lequel
    passent les octets upstream : stats.UsageCollector (identité, lit
    l'usage au passage) par défaut, anthropic_api.Translator pour
    réécrire la réponse."""
    upstream = await send_upstream(call, request, path, body)
    if isinstance(upstream, JSONResponse):
        return upstream
    robinet = tap(upstream.status_code, upstream.headers.get("content-type", ""))
    # Filet de sécurité si l'upstream ne renvoie aucun `usage` : même
    # approximation que le limiteur (corps envoyé ≈ tokens d'entrée).
    prompt_estimate = albert.estimate_chat_cost(body) if body else 0
    return StreamingResponse(
        relay(call, upstream, robinet, prompt_estimate),
        status_code=upstream.status_code,
        headers=response_headers(upstream),
    )


@app.get("/healthz")
async def healthz():
    return {
        "status": "ok",
        "tool_choice": TOOL_CHOICE,
        "auth_required": bool(PROXY_API_KEYS),
        "anthropic": {
            "enabled": anthropic_api.ENABLED,
            "model_map": anthropic_api.MODEL_MAP,
        },
        "backends": {
            name: {
                "url": b.url,
                "quotas": b.quotas,
                "key_injection": bool(b.api_key),
                "tool_choice": b.tool_choice or False,
                "max_tokens": b.max_tokens or None,
                "images": b.images,
                "tokenize_path": b.tokenize_path or None,
                "timeout": b.timeout,
                "meta_timeout": b.meta_timeout,
                "connect_timeout": b.connect_timeout,
                "last_seen_models": sorted(f"{name}/{m}" for m in b.models),
                **(b.quota_state.snapshot() if b.quotas else {}),
            }
            for name, b in BACKENDS.items()
        },
        "generic_limits": {"rpm": albert.GENERIC_RPM, "tpm": albert.GENERIC_TPM},
        "max_queue_seconds": albert.MAX_QUEUE_SECONDS,
        "exempt_paths": list(EXEMPT_SUFFIXES),
    }


def _model_max_context(m: dict) -> int | None:
    """max_context_length d'une entrée /v1/models. Albert le fournit ;
    llama.cpp non — on le dérive du «--ctx-size» de status.args (présent
    même modèle déchargé), sinon de meta.n_ctx_train."""
    if isinstance(m.get("max_context_length"), int):
        return m["max_context_length"]
    status = m.get("status")
    if isinstance(status, dict) and isinstance(status.get("args"), list):
        args = status["args"]
        try:
            return int(args[args.index("--ctx-size") + 1])
        except (ValueError, IndexError, TypeError):
            pass
    meta = m.get("meta")
    if isinstance(meta, dict):
        for key in ("n_ctx_train", "n_ctx"):
            if isinstance(meta.get(key), int):
                return meta[key]
    return None


def _model_type(m: dict) -> str:
    """`type` d'une entrée /v1/models. Albert le fournit ; llama.cpp non —
    on le dérive d'architecture.{input,output}_modalities."""
    if isinstance(m.get("type"), str):
        return m["type"]
    arch = m.get("architecture")
    if isinstance(arch, dict):
        inp = arch.get("input_modalities") or []
        out = arch.get("output_modalities") or []
        if "audio" in inp:
            return "automatic-speech-recognition"
        if "image" in inp and "text" in out:
            return "image-text-to-text"
    return "text-generation"


async def fetch_models(b: Backend, request: Request | None = None) -> list | None:
    """Le catalogue d'UN backend, normalisé et préfixé ; None s'il ne
    répond pas. Met à jour b.models et b.model_types au passage."""
    headers = b.auth_headers() or (
        clean_headers(request, "") if request is not None else {})
    try:
        r = await b.client.get(
            "/v1/models", headers=headers, timeout=b.meta_timeout,
        )
        if r.status_code != 200:
            log.warning("/v1/models %s → %d", b.name, r.status_code)
            return None
        data = r.json().get("data") or []
    except Exception as exc:
        log.warning("/v1/models %s injoignable : %s", b.name, exc)
        return None
    # Entrées NORMALISÉES sur un schéma UNIFORME (celui d'Albert),
    # id/aliases préfixés, champs manquants dérivés ou par défaut.
    # Le reste (status, args, preset, chemins de .gguf, meta… —
    # détail interne llama.cpp) est écarté : illisible pour les
    # clients, et à ne pas publier.
    entries = []
    types: dict[str, str] = {}
    for m in data:
        if isinstance(m, dict) and m.get("id"):
            costs = m.get("costs")
            kind = _model_type(m)
            types[str(m["id"]).lower()] = kind
            for a in m.get("aliases") or []:
                if isinstance(a, str):
                    types[a.lower()] = kind
            entries.append({
                "object": "model",
                "id": f"{b.name}/{m['id']}",
                "created": m.get("created") or 0,
                "owned_by": m.get("owned_by") or b.name,
                "type": kind,
                "costs": costs if isinstance(costs, dict)
                else {"prompt_tokens": 0.0, "completion_tokens": 0.0},
                "max_context_length": _model_max_context(m),
                "aliases": [
                    f"{b.name}/{a}" for a in (m.get("aliases") or [])
                    if isinstance(a, str)
                ],
            })
    b.models = {
        str(m.get("id")).lower() for m in data
        if isinstance(m, dict) and m.get("id")
    }
    b.model_types = types
    return entries


async def accepts_images(b: Backend, plain_model: str) -> bool:
    """Le modèle verra-t-il une image ? Jamais sans `images = true` sur
    le backend ; avec, c'est le TYPE du modèle au catalogue qui décide
    (chargé à la demande s'il ne l'a pas encore été). Modèle absent du
    catalogue : on fait confiance au flag."""
    if not b.images:
        return False
    if not b.model_types:
        await fetch_models(b)
    kind = b.model_types.get(plain_model.lower())
    return kind is None or kind == "image-text-to-text"


async def merged_models(request: Request) -> list[dict] | None:
    """Catalogue unifié : chaque backend en ligne est interrogé, ses
    modèles exposés préfixés par son nom («albert/…», «bigchuck/…») —
    les noms renvoyés sont directement routables. None = aucun backend
    joignable."""
    results = await asyncio.gather(
        *(fetch_models(b, request) for b in BACKENDS.values()))
    if all(lst is None for lst in results):
        return None
    return [e for lst in results if lst for e in lst]


@app.get("/v1/models")
async def list_models(request: Request):
    """Même catalogue, deux formes : OpenAI par défaut, Anthropic quand
    la requête porte `anthropic-version` (et que la surface est active)."""
    dialect = dialect_of(request)
    if dialect == "anthropic" and not anthropic_api.ENABLED:
        return anthropic_disabled(dialect)
    entries = await merged_models(request)
    if entries is None:
        return error_response(dialect, 502, "proxy_error",
                              "aucun backend joignable pour /v1/models")
    if dialect == "anthropic":
        return anthropic_api.models_list(entries)
    return {"object": "list", "data": entries}


def parse_json(raw: bytes):
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    raw = await request.body()
    payload = parse_json(raw)

    # Le routage d'abord : l'injection de tool_choice dépend du backend
    # visé (chacun peut la désactiver ou choisir sa valeur).
    backend, prefixed = route_backend(payload)
    if backend is None:
        return error_response("openai", 400, "unknown_backend_prefix",
                              unknown_prefix_message(str(payload.get("model", ""))))

    modified = False
    if isinstance(payload, dict):
        if inject_tool_choice(payload, backend):
            modified = True
            log.info(
                "tool_choice=%s injecté (backend=%s, model=%s, %d tools)",
                backend.tool_choice, backend.name, payload.get("model"),
                len(payload["tools"]),
            )
        modified = cap_max_tokens(payload, backend) or modified
    # Nom PRÉFIXÉ tel que demandé : c'est la clé des stats (le corps, lui,
    # est dé-préfixé juste après pour l'upstream).
    model_key = str(payload.get("model", "") or "") if isinstance(payload, dict) else ""
    if prefixed:
        raw = strip_backend_prefix(payload, backend)
    elif modified:
        raw = json.dumps(payload, ensure_ascii=False).encode()

    call = Call(backend, model_key, "/v1/chat/completions")
    blocked = await gate(call, request, payload, albert.estimate_chat_cost(raw))
    if blocked is not None:
        return blocked
    return await forward(call, request, "v1/chat/completions", raw)


# ── Surface Anthropic ───────────────────────────────────────────────────

def anthropic_disabled(dialect: str) -> JSONResponse:
    return error_response(
        dialect, 404, "unknown_route",
        "surface Anthropic désactivée sur ce proxy ([anthropic].enabled "
        "dans config.toml)",
    )


@app.post("/v1/messages")
async def messages(request: Request):
    """L'API Messages, traduite vers /v1/chat/completions du backend que
    désigne le modèle — après passage par [anthropic.model_map]. La
    réponse repasse par anthropic_api.Translator (JSON ou SSE)."""
    dialect = "anthropic"
    if not anthropic_api.ENABLED:
        return anthropic_disabled(dialect)
    payload = parse_json(await request.body())
    if not isinstance(payload, dict):
        return error_response(dialect, 400, "invalid_request_error",
                              "corps JSON attendu")

    requested = str(payload.get("model", "") or "")
    resolved = anthropic_api.resolve_model(requested, BACKENDS)
    if resolved is None:
        return error_response(
            dialect, 400, "unknown_backend_prefix",
            f"modèle «{requested}» inconnu : l'ajouter à "
            f"[anthropic.model_map] (ou «default»), ou le préfixer "
            f"par un backend",
        )
    payload["model"] = resolved
    backend, _ = route_backend(payload)
    if backend is None:
        return error_response(dialect, 400, "unknown_backend_prefix",
                              unknown_prefix_message(resolved))
    if requested and requested != resolved:
        log.info("anthropic : modèle %r → %r", requested, resolved)

    images = False
    if backend.images and anthropic_api.has_images(payload):
        images = await accepts_images(backend, resolved[len(backend.name) + 1:])
    openai_payload = anthropic_api.to_openai(payload, images=images)
    if inject_tool_choice(openai_payload, backend):
        log.info(
            "tool_choice=%s injecté (backend=%s, model=%s, %d tools)",
            backend.tool_choice, backend.name, resolved,
            len(openai_payload["tools"]),
        )
    cap_max_tokens(openai_payload, backend)
    raw = strip_backend_prefix(openai_payload, backend)

    call = Call(backend, resolved, "/v1/messages", dialect)
    cost = albert.estimate_chat_cost(raw)
    tap = lambda status, ct: anthropic_api.Translator(status, ct, resolved)
    if openai_payload.get("stream") and backend.quotas \
            and anthropic_api.PING_INTERVAL > 0:
        return StreamingResponse(
            pinged_stream(call, request, openai_payload, raw, cost, tap),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache"},
        )
    blocked = await gate(call, request, openai_payload, cost)
    if blocked is not None:
        return blocked
    return await forward(call, request, "v1/chat/completions", raw, tap=tap)


async def pinged_stream(call: Call, request: Request, payload: dict,
                        raw: bytes, cost: int, tap):
    """Flux Anthropic derrière un limiteur : le 200 part TOUT DE SUITE et
    des `event: ping` tiennent la connexion pendant l'attente du quota —
    un flux muet de plusieurs minutes, Claude Code le coupe. Ce que
    `gate` rendait en réponse HTTP devient ici un `event: error` ; un
    client qui raccroche annule ce générateur, l'attente s'arrête et le
    quota n'est pas consommé (499)."""
    limiter = call.backend.quota_state.get_limiter(payload)
    acquired = False
    try:
        waiter = asyncio.ensure_future(limiter.acquire(cost))
        while True:
            done, _ = await asyncio.wait({waiter},
                                         timeout=anthropic_api.PING_INTERVAL)
            if done:
                break
            yield anthropic_api.ping_event()
        try:
            waited = waiter.result()
        except albert.QuotaWaitTooLong as exc:
            body = json.loads(quota_error(call, exc, limiter).body)
            yield anthropic_api.sse_error(body)
            return
        acquired = True
        released(call, limiter, waited, cost)

        upstream = await send_upstream(call, request, "v1/chat/completions", raw)
        if isinstance(upstream, JSONResponse):
            yield anthropic_api.sse_error(json.loads(upstream.body))
            return
        robinet = tap(upstream.status_code,
                      upstream.headers.get("content-type", ""))
        if robinet.ok:
            async for chunk in relay(call, upstream, robinet, cost):
                yield chunk
            return
        # Statut d'erreur upstream : le robinet en fait un corps d'erreur
        # Anthropic (finish), qu'on ne peut plus que dire dans le flux.
        chunks = []
        async for chunk in relay(call, upstream, robinet, cost):
            chunks.append(chunk)
        try:
            body = json.loads(b"".join(chunks))
        except ValueError:
            body = anthropic_api.error_body("réponse upstream illisible",
                                            "api_error")
        yield anthropic_api.sse_error(body)
    finally:
        if not acquired:
            # Annulé (client parti) pendant l'attente : la tâche d'attente
            # est coupée avec lui — `acquire` rend le verrou, rien n'est
            # compté dans la fenêtre.
            waiter.cancel()
            client_gone(call, limiter)


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    """Aucun équivalent OpenAI. Exact si le backend visé expose un
    endpoint de tokenisation ([backends.<nom>].tokenize_path — llama.cpp
    a /tokenize) ; estimation locale sinon, ou s'il ne répond pas.
    Claude Code s'en sert pour sa jauge de contexte (et le moment de
    son /compact) : un chiffre juste évite qu'elle dérive."""
    if not anthropic_api.ENABLED:
        return anthropic_disabled("anthropic")
    payload = parse_json(await request.body())
    if not isinstance(payload, dict):
        return error_response("anthropic", 400, "invalid_request_error",
                              "corps JSON attendu")
    resolved = anthropic_api.resolve_model(
        str(payload.get("model", "") or ""), BACKENDS)
    backend, _ = route_backend({"model": resolved}) if resolved else (None, False)
    if backend is not None and backend.tokenize_path:
        exact = await tokenize_upstream(backend, resolved, payload)
        if exact is not None:
            return {"input_tokens": exact}
    return {"input_tokens": anthropic_api.estimate_tokens(payload)}


async def tokenize_upstream(b: Backend, model: str, payload: dict) -> int | None:
    """POST tokenize_path du backend avec le texte du prompt ; rend le
    nombre de tokens, ou None (injoignable, forme inattendue) — l'appelant
    retombe sur l'estimation, jamais d'erreur pour un compteur."""
    body = {"content": anthropic_api.prompt_text(payload),
            "model": model[len(b.name) + 1:], "add_special": True}
    try:
        r = await b.client.post(
            "/" + b.tokenize_path.strip("/"), json=body,
            headers=b.auth_headers(), timeout=b.meta_timeout,
        )
        if r.status_code != 200:
            log.warning("%s %s → %d, estimation", b.name, b.tokenize_path,
                        r.status_code)
            return None
        doc = r.json()
    except Exception as exc:
        log.warning("%s %s injoignable : %s, estimation", b.name,
                    b.tokenize_path, exc)
        return None
    # llama.cpp : {"tokens": [...]} ; d'autres rendent un compte direct.
    tokens = doc.get("tokens") if isinstance(doc, dict) else None
    if isinstance(tokens, list):
        return len(tokens)
    for key in ("count", "n_tokens", "input_tokens"):
        if isinstance(doc, dict) and isinstance(doc.get(key), int):
            return doc[key]
    return None


# ── Usage API ───────────────────────────────────────────────────────────

USAGE_PATH = "/v1/organization/usage/completions"


def usage_query(request: Request) -> dict | JSONResponse:
    """Lit et valide les paramètres de l'Usage API. `start_time` est le
    seul obligatoire, comme à l'upstream."""
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
        group_by = stats.parse_group_by(repeated("group_by"))
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
    absent = any(repeated(name) for name in
                 ("project_ids", "user_ids", "api_key_ids")) \
        or q.get("batch") == "true"
    return {
        "start_time": start_time, "end_time": end_time,
        "bucket_width": width, "group_by": group_by,
        "limit": limit, "page": q.get("page"),
        "models": models, "empty": absent,
    }


async def usage_page(request: Request):
    """Forme de l'Usage API OpenAI, page → buckets → results. Un modèle
    n'apparaît que s'il a servi au moins une requête dans le seau
    concerné. Les agrégats sortent de SQLite : la lecture part dans le
    pool de threads pour ne pas bloquer la boucle d'événements."""
    params = usage_query(request)
    if isinstance(params, JSONResponse):
        return params
    return await run_in_threadpool(stats.usage_completions, **params)


@app.get(USAGE_PATH)
async def usage_completions(request: Request):
    return await usage_page(request)


@app.get("/ui", response_class=HTMLResponse)
async def ui_page(request: Request):
    """Tableau de bord : page statique. Les chiffres sont lus côté client
    sur /ui/usage, toutes les 5 s, sans jamais recharger la page."""
    with open(UI_HTML, encoding="utf-8") as fh:
        response = HTMLResponse(fh.read())
    # Auth active + clé passée en «?key=» : mémorisée pour que les appels
    # suivants (sans en-tête possible depuis le navigateur) restent
    # authentifiés.
    key = request.query_params.get("key", "")
    if PROXY_API_KEYS and key:
        response.set_cookie(
            UI_COOKIE, key, httponly=True, samesite="strict",
            secure=request.url.scheme == "https", max_age=30 * 86400,
        )
    return response


@app.get("/ui/usage")
async def ui_usage(request: Request):
    """Exactement la route d'Usage API ci-dessus, mais sous /ui : le
    cookie posé par la page y suffit comme authentification (un fetch de
    navigateur ne peut pas porter le Bearer)."""
    return await usage_page(request)


@app.get("/")
async def root():
    return RedirectResponse("/ui")


@app.api_route(
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

    raw = await request.body()
    payload = parse_json(raw)
    backend, prefixed = route_backend(payload)
    if backend is None:
        return error_response(dialect, 400, "unknown_backend_prefix",
                              unknown_prefix_message(str(payload.get("model", ""))))
    model_key = str(payload.get("model", "") or "") if isinstance(payload, dict) else ""
    modified = isinstance(payload, dict) and cap_max_tokens(payload, backend)
    if prefixed:
        raw = strip_backend_prefix(payload, backend)
    elif modified:
        raw = json.dumps(payload, ensure_ascii=False).encode()

    call = Call(backend, model_key, normalized, dialect)
    blocked = await gate(call, request, payload,
                         albert.estimate_generic_cost(raw, payload))
    if blocked is not None:
        return blocked
    return await forward(call, request, path, raw)
