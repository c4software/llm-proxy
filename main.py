"""
Proxy transparent devant l'API Albert (DINUM), OpenAI-compatible.

Rôles :
  1. peut injecter tool_choice quand `tools` est présent sans `tool_choice`
     (le défaut du schéma Albert est "none", ce qui casse le tool calling).
     RIEN N'EST INJECTÉ par défaut : cela s'active PAR BACKEND via
     BACKENDS[...].force_tool_choice (true = FORCE_TOOL_CHOICE, une chaîne
     = cette valeur) ;
  2. porte les clés upstream pour tous les clients ;
  3. multi-backends : BACKENDS (env, JSON {"<nom>": {url, ...}}) déclare
     les backends OpenAI-compatibles ; le PRÉFIXE du modèle est LE
     discriminant de routage — « albert/openweight-large » part vers le
     backend « albert », « bigchuck/qwen3 » vers « bigchuck », préfixe
     retiré avant transfert. Le backend marqué "quotas": true (unique,
     Albert) passe par le limiteur du module `albert` ; les autres
     (locaux, llama.cpp) sont illimités mais pas toujours allumés :
     AUCUN sondage périodique — contactés à la demande (connect court,
     5 s), backend éteint → 503 backend_offline. TOUT modèle doit être
     préfixé : sans préfixe reconnu → 400 ; seules les requêtes sans
     champ model (endpoints de compte, corps non JSON) partent vers le
     backend à quotas. GET /v1/models interroge les backends en direct
     et fusionne les catalogues, chaque id exposé préfixé ;
  4. ne transmet QUE les routes nécessaires (FORWARD_POST_PATHS,
     /v1/chat/completions, /v1/models) — toute autre URL reçoit un 404
     local, rien n'est relayé aveuglément aux backends ;
  4bis. GET /v1/stats : compteurs d'usage par modèle (nom préfixé),
     alimentés en lisant l'`usage` des réponses upstream (ou par
     estimation à défaut) ; un modèle n'y apparaît QUE s'il a servi au
     moins une requête — voir stats.py. GET /ui en est le tableau de
     bord : une page HTML statique (templates/index.html) que
     static/dashboard.js remplit en relisant ce JSON toutes les 5 s ;
  5. auth optionnelle du proxy lui-même : PROXY_API_KEY (env, vide par
     défaut = ouvert) exige des clients un «Authorization: Bearer <clé>»
     à la OpenAI (plusieurs clés séparées par des virgules, 401 sinon,
     /healthz exempté). Ce Bearer est la clé DU PROXY : il n'est jamais
     relayé aux backends quand l'auth est active.

Tout ce qui est spécifique à Albert (limiteur de quotas, familles,
association routeurs ↔ modèles) vit dans albert.py.
"""

import asyncio
import hmac
import json
import logging
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
from starlette.background import BackgroundTask

import albert
import stats

TOOL_CHOICE = os.environ.get("FORCE_TOOL_CHOICE", "auto")
TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "600"))
# Timeout par défaut (secondes) des appels de méta-données vers les
# backends (GET /v1/models, /v1/me/info…) : court, un backend lent ne
# doit pas bloquer le catalogue. Surchargeable par BACKENDS[...].meta_timeout.
META_TIMEOUT = 5.0

# Clé(s) exigée(s) DES CLIENTS pour appeler le proxy (à la OpenAI :
# «Authorization: Bearer <clé>»). Vide (défaut) = proxy ouvert.
# Plusieurs clés possibles, séparées par des virgules. /healthz reste
# toujours accessible sans clé.
PROXY_API_KEYS = frozenset(
    k.strip() for k in os.environ.get("PROXY_API_KEY", "").split(",")
    if k.strip()
)

# Seules routes POST relayées aux backends, en plus des handlers dédiés
# (/v1/chat/completions, /v1/models). Tout le reste → 404 local.
FORWARD_POST_PATHS = frozenset(
    "/" + p.strip().strip("/") for p in os.environ.get(
        "FORWARD_POST_PATHS",
        "/v1/completions,/v1/embeddings,/v1/rerank,"
        "/v1/audio/transcriptions,/v1/ocr",
    ).split(",") if p.strip()
)

EXEMPT_SUFFIXES = tuple(
    s.strip() for s in os.environ.get(
        "EXEMPT_PATHS",
        "/embeddings,/rerank,/audio/transcriptions,/ocr",
    ).split(",") if s.strip()
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("albert-proxy")

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
    "host", "content-length", "accept-encoding",
}

class Backend:
    """Un backend OpenAI-compatible, identifié par son nom = préfixe de
    routage. Celui marqué quotas=True (unique, Albert) passe par le
    limiteur ; les autres sont illimités mais pas toujours allumés —
    contactés uniquement à la demande, jamais sondés en tâche de fond."""

    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.url = str(cfg.get("url", "") or "").rstrip("/")
        if not self.url:
            raise SystemExit(f"BACKENDS[{name}] : champ 'url' manquant")
        self.api_key = str(cfg.get("api_key", "") or "")
        self.verify_ssl = bool(cfg.get("verify_ssl", True))
        self.quotas = bool(cfg.get("quotas", False))
        self.timeout = float(cfg.get("timeout", TIMEOUT))
        self.meta_timeout = float(cfg.get("meta_timeout", META_TIMEOUT))
        # Injection de `tool_choice` : DÉSACTIVÉE PAR DÉFAUT. Le
        # correctif ne vaut que pour les backends dont le schéma a
        # "none" pour défaut (Albert) ; ailleurs il est au mieux inutile,
        # au pire nuisible. On l'active donc backend par backend :
        #   absent / false → aucune injection (défaut) ;
        #   true           → valeur globale FORCE_TOOL_CHOICE ;
        #   "auto", "required"… → cette valeur-là, pour ce backend.
        self.tool_choice = self._tool_choice(cfg.get("force_tool_choice"))
        # Chaque backend à quotas a SES limiteurs/routeurs (deux comptes
        # Albert avec des clés différentes ne partagent rien).
        self.quota_state = albert.QuotaState(name) if self.quotas else None
        self.client: httpx.AsyncClient | None = None
        # Dernier catalogue vu lors d'un GET /v1/models (informel, healthz).
        self.models: set[str] = set()

    @staticmethod
    def _tool_choice(value) -> str | None:
        """None = ne rien injecter pour ce backend (le défaut)."""
        if value is None or value is False:
            return None
        if value is True:
            return TOOL_CHOICE
        return str(value)

    def auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}


# Défaut si BACKENDS n'est pas défini : Albert seul. URLs ET clés ne
# vivent QUE dans BACKENDS (champ api_key par backend), rien ailleurs.
DEFAULT_BACKENDS = {
    # Albert est le seul à avoir besoin du correctif tool_choice (son
    # schéma déclare «default: none») : ce fallback l'active donc, alors
    # que l'option est à false partout ailleurs.
    "albert": {"url": "https://albert.api.etalab.gouv.fr", "quotas": True,
               "force_tool_choice": "auto"},
}


def load_backends() -> dict[str, Backend]:
    raw = os.environ.get("BACKENDS", "")
    try:
        parsed = json.loads(raw) if raw.strip() else DEFAULT_BACKENDS
    except json.JSONDecodeError as exc:
        raise SystemExit(f"BACKENDS invalide (JSON attendu) : {exc}")
    if not isinstance(parsed, dict) or not parsed:
        raise SystemExit("BACKENDS : objet JSON non vide attendu")
    out: dict[str, Backend] = {}
    for name, cfg in parsed.items():
        key = str(name).strip().strip("/").lower()
        if not key:
            raise SystemExit("BACKENDS : nom de backend vide")
        out[key] = Backend(key, cfg if isinstance(cfg, dict) else {})
    return out


BACKENDS: dict[str, Backend] = load_backends()
# Les requêtes SANS champ model (endpoints de compte, corps non JSON)
# n'ont pas de préfixe à router : elles partent vers le premier backend
# à quotas (à défaut, le premier déclaré).
FALLBACK_BACKEND = next(
    (b for b in BACKENDS.values() if b.quotas),
    next(iter(BACKENDS.values())),
)

def route_backend(payload) -> tuple[Backend | None, bool]:
    """Discriminant de routage : le préfixe du modèle. «<nom>/…» part
    vers le backend <nom> ; requête sans champ model → FALLBACK_BACKEND ;
    modèle sans préfixe reconnu → None (400). Renvoie (backend, préfixé)
    — préfixé impose de retirer «nom/»."""
    model = ""
    if isinstance(payload, dict):
        model = str(payload.get("model", "") or "")
    if not model:
        return FALLBACK_BACKEND, False
    m = model.lower()
    for name, b in BACKENDS.items():
        if m.startswith(name + "/"):
            return b, True
    return None, False


def unknown_prefix_response(model: str) -> JSONResponse:
    prefixes = ", ".join(f"«{n}/»" for n in BACKENDS)
    log.warning("modèle %r sans préfixe backend reconnu → 400", model)
    return JSONResponse(
        {
            "error": {
                "message": (
                    f"modèle «{model}» sans préfixe backend ; "
                    f"préfixes attendus : {prefixes}"
                ),
                "type": "unknown_backend_prefix",
            }
        },
        status_code=400,
    )


def strip_backend_prefix(payload: dict, b: Backend) -> bytes:
    """Retire «<nom>/» du champ model (l'upstream ne connaît pas le
    préfixe) et ré-encode le corps."""
    payload["model"] = str(payload["model"])[len(b.name) + 1:]
    return json.dumps(payload).encode()


def backend_offline_response(b: Backend, exc: Exception) -> JSONResponse:
    log.warning("backend %s (%s) injoignable : %s → 503", b.name, b.url, exc)
    return JSONResponse(
        {
            "error": {
                "message": (
                    f"backend «{b.name}» ({b.url}) hors ligne : {exc}"
                ),
                "type": "backend_offline",
            }
        },
        status_code=503,
        headers={"Retry-After": "30"},
    )


def is_exempt(path: str) -> bool:
    normalized = "/" + path.strip("/")
    return normalized.endswith(EXEMPT_SUFFIXES)


@asynccontextmanager
async def lifespan(app: FastAPI):
    for b in BACKENDS.values():
        # connect court pour les backends sans quota : souvent éteints,
        # échouer vite.
        b.client = httpx.AsyncClient(
            base_url=b.url,
            timeout=httpx.Timeout(b.timeout, connect=15.0 if b.quotas else 5.0),
            follow_redirects=False,
            verify=b.verify_ssl,
        )

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
            "backend %s : %s",
            name,
            f"tool_choice={b.tool_choice!r} injecté si absent"
            if b.tool_choice else "aucune injection de tool_choice",
        )
    if PROXY_API_KEYS:
        log.info(
            "auth proxy ACTIVE : %d clé(s) acceptée(s), /healthz exempté",
            len(PROXY_API_KEYS),
        )
    else:
        log.info("auth proxy inactive (PROXY_API_KEY vide) : proxy ouvert")
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
    for b in BACKENDS.values():
        if b.client is not None:
            await b.client.aclose()


app = FastAPI(title="albert-proxy", lifespan=lifespan)

# Feuille de style et script du tableau de bord : servis par le proxy
# lui-même, aucun CDN. Monté AVANT le catch-all, qui sinon répondrait
# 404. Le mount reste sous /ui : l'auth du proxy s'y applique.
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/ui/static", StaticFiles(directory=STATIC_DIR), name="static")
# La page du tableau de bord : du HTML statique, sans moteur de template —
# tout est rempli par static/dashboard.js à partir du JSON des stats.
UI_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "templates", "index.html")


UI_PREFIX = "/ui"
UI_COOKIE = "proxy_key"


def is_ui_path(path: str) -> bool:
    # «/» y est inclus : il ne fait que rediriger vers /ui, autant qu'un
    # navigateur déjà porteur du cookie y arrive sans repasser par ?key=.
    return path == "/" or path == UI_PREFIX or path.startswith(UI_PREFIX + "/")


def client_token(request: Request) -> str:
    """Clé présentée par le client. Les clients API l'envoient en
    «Authorization: Bearer» ; un NAVIGATEUR ne le peut pas sur une simple
    URL — les pages /ui acceptent donc «?key=…» (mémorisé ensuite en
    cookie), et rien d'autre ne change."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:]
    if is_ui_path(request.url.path):
        return (request.query_params.get("key")
                or request.cookies.get(UI_COOKIE, ""))
    return ""


@app.middleware("http")
async def require_proxy_key(request: Request, call_next):
    if PROXY_API_KEYS and request.url.path != "/healthz":
        token = client_token(request)
        if not any(hmac.compare_digest(token, k) for k in PROXY_API_KEYS):
            log.warning(
                "clé proxy absente ou invalide : %s %s → 401",
                request.method, request.url.path,
            )
            return JSONResponse(
                {
                    "error": {
                        "message": (
                            "clé API du proxy absente ou invalide "
                            "(en-tête «Authorization: Bearer <clé>» attendu)"
                        ),
                        "type": "invalid_api_key",
                    }
                },
                status_code=401,
            )
    return await call_next(request)


def clean_headers(request: Request, api_key: str) -> dict:
    # Authorization du client jamais relayé si le backend a sa clé, ni si
    # l'auth proxy est active (le Bearer du client est la clé DU PROXY,
    # elle ne doit pas fuiter vers l'upstream).
    skip = HOP_BY_HOP | (
        {"authorization"} if api_key or PROXY_API_KEYS else set()
    )
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


def quota_exceeded_response(exc: albert.QuotaWaitTooLong,
                            limiter: albert.Limiter) -> JSONResponse:
    retry_after = max(int(exc.delay), 1)
    log.warning(
        "[%s] quota %s : attente %.0fs > MAX_QUEUE_SECONDS (%ds) → 429 local",
        limiter.name, exc.window_name, exc.delay,
        int(albert.MAX_QUEUE_SECONDS),
    )
    return JSONResponse(
        {
            "error": {
                "message": (
                    f"quota {exc.window_name} épuisé pour «{limiter.name}» ; "
                    f"réessayer dans ~{retry_after}s"
                ),
                "type": "rate_limit_exceeded_proxy",
            }
        },
        status_code=429,
        headers={"Retry-After": str(retry_after)},
    )


def record_stat(model_key: str, b: Backend, endpoint: str, status: int,
                latency: float, prompt_tokens: int = 0,
                completion_tokens: int = 0, exact: bool = False,
                streamed: bool = False) -> None:
    """Alimente stats.py. `model_key` = modèle PRÉFIXÉ demandé par le
    client ; vide (requête sans champ model) = rien n'est compté."""
    if not model_key:
        return
    plain = model_key[len(b.name) + 1:] if model_key.lower().startswith(
        b.name + "/") else model_key
    stats.record(model_key, b.name, plain, endpoint, status, latency,
                 prompt_tokens, completion_tokens, exact, streamed)


async def forward(request: Request, path: str, content=None,
                  backend: Backend | None = None,
                  model_key: str = "") -> Response:
    b = backend or FALLBACK_BACKEND
    client: httpx.AsyncClient = b.client
    body = content if content is not None else await request.body()
    endpoint = "/" + path.strip("/")
    started = time.monotonic()
    # Filet de sécurité si l'upstream ne renvoie aucun `usage` : même
    # approximation que le limiteur (corps envoyé ≈ tokens d'entrée).
    prompt_estimate = albert.estimate_chat_cost(body) if body else 0

    req = client.build_request(
        request.method,
        f"/{path}",
        content=body or None,
        headers=clean_headers(request, b.api_key),
        params=request.query_params,
    )
    try:
        upstream = await client.send(req, stream=True)
    except httpx.RequestError as exc:
        # backend sans quota (local) : éteint fait partie du fonctionnement
        # normal → 503 parlant plutôt qu'un 502.
        if not b.quotas:
            record_stat(model_key, b, endpoint, 503,
                        time.monotonic() - started)
            return backend_offline_response(b, exc)
        log.error("backend %s injoignable : %s", b.name, exc)
        record_stat(model_key, b, endpoint, 502, time.monotonic() - started)
        return JSONResponse(
            {"error": {"message": f"upstream unreachable: {exc}", "type": "proxy_error"}},
            status_code=502,
        )

    if upstream.status_code == 429 and b.quotas:
        log.warning(
            "429 reçu d'Albert malgré le limiteur — comparer /healthz avec "
            "/v1/me/info ; l'association routeur est peut-être fausse "
            "(fixer via ROUTER_MODELS)"
        )

    collector = stats.UsageCollector(upstream.headers.get("content-type", ""))

    async def tapped():
        """Relaie les octets tels quels et, au passage, en extrait l'usage.
        Le `finally` compte aussi les flux interrompus (client parti)."""
        try:
            async for chunk in upstream.aiter_raw():
                collector.feed(chunk)
                yield chunk
        finally:
            collector.finish()
            prompt, completion, exact = collector.tokens(prompt_estimate)
            record_stat(
                model_key, b, endpoint, upstream.status_code,
                time.monotonic() - started, prompt, completion,
                exact, collector.sse,
            )

    return StreamingResponse(
        tapped(),
        status_code=upstream.status_code,
        headers=response_headers(upstream),
        background=BackgroundTask(upstream.aclose),
    )


@app.get("/healthz")
async def healthz():
    return {
        "status": "ok",
        "tool_choice": TOOL_CHOICE,
        "auth_required": bool(PROXY_API_KEYS),
        "backends": {
            name: {
                "url": b.url,
                "quotas": b.quotas,
                "key_injection": bool(b.api_key),
                "tool_choice": b.tool_choice or False,
                "timeout": b.timeout,
                "meta_timeout": b.meta_timeout,
                # dernier catalogue vu lors d'un GET /v1/models (informel)
                "last_seen_models": sorted(f"{name}/{m}" for m in b.models),
                # état du limiteur (backends à quotas seulement)
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


@app.get("/v1/models")
async def list_models(request: Request):
    """Catalogue unifié : chaque backend en ligne est interrogé, ses
    modèles exposés préfixés par son nom («albert/…», «bigchuck/…») —
    les noms renvoyés sont directement routables."""
    async def fetch(b: Backend) -> list | None:
        headers = b.auth_headers() or clean_headers(request, "")
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
        b.models = {
            str(m.get("id")).lower() for m in data
            if isinstance(m, dict) and m.get("id")
        }
        # Entrées NORMALISÉES sur un schéma UNIFORME (celui d'Albert),
        # id/aliases préfixés, champs manquants dérivés ou par défaut.
        # Le reste (status, args, preset, chemins de .gguf, meta… —
        # détail interne llama.cpp) est écarté : illisible pour les
        # clients, et à ne pas publier.
        entries = []
        for m in data:
            if isinstance(m, dict) and m.get("id"):
                costs = m.get("costs")
                entries.append({
                    "object": "model",
                    "id": f"{b.name}/{m['id']}",
                    "created": m.get("created") or 0,
                    "owned_by": m.get("owned_by") or b.name,
                    "type": _model_type(m),
                    "costs": costs if isinstance(costs, dict)
                    else {"prompt_tokens": 0.0, "completion_tokens": 0.0},
                    "max_context_length": _model_max_context(m),
                    "aliases": [
                        f"{b.name}/{a}" for a in (m.get("aliases") or [])
                        if isinstance(a, str)
                    ],
                })
        return entries

    results = await asyncio.gather(*(fetch(b) for b in BACKENDS.values()))
    if all(lst is None for lst in results):
        return JSONResponse(
            {"error": {"message": "aucun backend joignable pour /v1/models",
                       "type": "proxy_error"}},
            status_code=502,
        )
    return {"object": "list",
            "data": [e for lst in results if lst for e in lst]}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    raw = await request.body()
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = None

    # Le routage d'abord : l'injection de tool_choice dépend du backend
    # visé (chacun peut la désactiver ou choisir sa valeur).
    backend, prefixed = route_backend(payload)
    if backend is None:
        return unknown_prefix_response(str(payload.get("model", "")))

    injected = isinstance(payload, dict) and inject_tool_choice(payload, backend)
    if injected:
        log.info(
            "tool_choice=%s injecté (backend=%s, model=%s, %d tools)",
            backend.tool_choice, backend.name, payload.get("model"),
            len(payload["tools"]),
        )
    # Nom PRÉFIXÉ tel que demandé : c'est la clé des stats (le corps, lui,
    # est dé-préfixé juste après pour l'upstream).
    model_key = str(payload.get("model", "") or "") if isinstance(payload, dict) else ""
    if prefixed:
        raw = strip_backend_prefix(payload, backend)
    elif injected:
        raw = json.dumps(payload).encode()
    if not backend.quotas:
        albert.maybe_log_status()
        return await forward(request, "v1/chat/completions", raw,
                             backend=backend, model_key=model_key)

    limiter = backend.quota_state.get_limiter(payload)  # model dé-préfixé
    cost = albert.estimate_chat_cost(raw)
    started = time.monotonic()
    try:
        waited = await limiter.acquire(cost)
    except albert.QuotaWaitTooLong as exc:
        # Rejet local : compté comme requête en erreur du modèle visé.
        record_stat(model_key, backend, "/v1/chat/completions", 429,
                    time.monotonic() - started)
        return quota_exceeded_response(exc, limiter)
    albert.maybe_log_status()
    if waited:
        log.info(
            "[%s] requête relâchée après %.1fs d'attente (~%d tokens)",
            limiter.name, waited, cost,
        )

    return await forward(request, "v1/chat/completions", raw,
                         backend=backend, model_key=model_key)


@app.get("/v1/stats")
async def usage_stats():
    """Compteurs d'usage par modèle, DYNAMIQUES : un modèle n'apparaît
    dans `data` qu'à partir de sa première requête servie (aucun bucket
    n'est pré-créé depuis le catalogue). Les id sont les noms préfixés,
    directement réutilisables comme `model`."""
    return stats.snapshot()


@app.get("/ui", response_class=HTMLResponse)
async def ui_page(request: Request):
    """Tableau de bord : page statique. Les chiffres sont lus côté client
    sur /ui/stats, toutes les 5 s, sans jamais recharger la page."""
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


@app.get("/ui/stats")
async def ui_stats():
    """Exactement le JSON de /v1/stats, mais sous /ui : le cookie posé par
    la page y suffit comme authentification (un fetch de navigateur ne
    peut pas porter le Bearer)."""
    return stats.snapshot()


@app.get("/")
async def root():
    return RedirectResponse("/ui")


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def passthrough(path: str, request: Request):
    normalized = "/" + path.strip("/")
    if request.method != "POST" or normalized not in FORWARD_POST_PATHS:
        log.info("route non gérée : %s %s → 404 local", request.method, normalized)
        return JSONResponse(
            {"error": {"message": f"route non gérée par le proxy : "
                                  f"{request.method} {normalized}",
                       "type": "unknown_route"}},
            status_code=404,
        )

    raw = await request.body()
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = None
    backend, prefixed = route_backend(payload)
    if backend is None:
        return unknown_prefix_response(str(payload.get("model", "")))
    model_key = str(payload.get("model", "") or "") if isinstance(payload, dict) else ""
    if prefixed:
        raw = strip_backend_prefix(payload, backend)
    # Limiteur : seulement le backend à quotas, hors endpoints exemptés.
    if backend.quotas and not is_exempt(path):
        limiter = backend.quota_state.get_limiter(payload)
        started = time.monotonic()
        try:
            waited = await limiter.acquire(
                albert.estimate_generic_cost(raw, payload)
            )
        except albert.QuotaWaitTooLong as exc:
            record_stat(model_key, backend, normalized, 429,
                        time.monotonic() - started)
            return quota_exceeded_response(exc, limiter)
        if waited:
            log.info(
                "[%s] POST %s relâché après %.1fs d'attente",
                limiter.name, normalized, waited,
            )
    albert.maybe_log_status()
    return await forward(request, path, raw, backend=backend,
                         model_key=model_key)
