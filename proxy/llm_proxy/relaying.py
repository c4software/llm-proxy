"""
Le relais vers les backends. Chaque requête relayée est portée par UN
objet `Call` (backend visé, modèle demandé, endpoint, dialecte du
client) : c'est lui qui écrit la ligne de stats — une fois, quel que
soit le chemin de sortie (relais, 429 local, client parti, backend
éteint) — et qui construit les erreurs à la forme que le client attend.
La porte de quota (`gate`) et le relais (`forward`) sont les mêmes pour
toutes les routes ; `send_upstream` est le seul endroit qui joint un
backend (les tests le remplacent).
"""

import time

import httpx2
from fastapi import Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from . import albert
from . import anthropic_api
from . import stats
from .backends import Backend, backend_offline_message
from .settings import HOP_BY_HOP, is_exempt, log
from .auth import auth_required
from .httputil import error_response


# En-têtes du dialecte Anthropic : sans sens pour un backend OpenAI, et
# `x-api-key` porte la clé DU PROXY (ou une valeur bidon du client) —
# jamais relayés, auth active ou non.
ANTHROPIC_HEADERS = {"x-api-key", "anthropic-version", "anthropic-beta"}


def clean_headers(request: Request, api_key: str) -> dict:
    # Authorization du client jamais relayé si le backend a sa clé, ni si
    # l'auth proxy est active (le Bearer du client est la clé DU PROXY,
    # elle ne doit pas fuiter vers l'upstream).
    skip = HOP_BY_HOP | ANTHROPIC_HEADERS | {"x-incognito"} | (
        {"authorization"} if api_key or auth_required() else set()
    )
    # Auth proxy active : le cookie posé par /ui porte la clé DU PROXY, ou
    # la session OIDC de la page — même raison que l'Authorization, il ne
    # doit pas fuiter en amont.
    if auth_required():
        skip = skip | {"cookie"}
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in skip
    }
    headers["accept-encoding"] = "identity"
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def response_headers(upstream: httpx2.Response) -> dict:
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
        # Le statut compté par done() : ce qu'une réponse construite APRÈS
        # la boucle des outils hébergés doit porter (messages(), en JSON).
        self.status = 0

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
        self.status = status
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
                        body: bytes) -> httpx2.Response | JSONResponse:
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
    except httpx2.RequestError as exc:
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


async def relay(call: Call, upstream: httpx2.Response, robinet,
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
        if anthropic_api.TRACE and hasattr(robinet, "summary"):
            log.info("[%s] %s → %s", call.backend.name, call.model_key,
                     robinet.summary())
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
