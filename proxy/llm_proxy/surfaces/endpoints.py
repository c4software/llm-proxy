"""
Les trois surfaces de conversation : /v1/chat/completions (relais brut,
ou boucle des outils hébergés si la requête en déclare), /v1/messages et
/v1/messages/count_tokens (Anthropic, traduits par anthropic_api), et
/v1/responses (traduit par responses_api). Toutes passent par le même
relais (relaying.py) et, pour les outils, la même boucle (tool_loop.py).
"""

import asyncio
import json

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .. import tools
from ..core import multipart
from ..core import settings
from ..ui import page_tools
from ..upstream import quotas, relaying
from . import anthropic_api, chat_api, responses_api
from ..upstream.backends import (
    Backend,
    no_route,
    route_backend,
    strip_backend_prefix,
    visible,
)
from ..access.auth import request_owner
from ..core.httputil import anthropic_disabled, dialect_of, error_response, parse_json
from ..core.settings import INCOGNITO, log
from ..upstream.catalog import accepts_images, converses, merged_models
from ..upstream.relaying import (
    CLIENT_CLOSED,
    Call,
    cap_max_tokens,
    client_gone,
    default_tap,
    fit_fields,
    forward,
    gate,
    inject_tool_choice,
    quota_error,
    relay,
    released,
    response_headers,
)
from .tool_loop import hosted_loop, hosted_stream


router = APIRouter()


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    return await chat(request)


async def chat(request: Request, page: bool = False, raw: bytes | None = None):
    """/v1/chat/completions, et le chat du tableau de bord (`page`) : pas
    de [chat].always, l'exécution des outils annoncée dans le flux. `raw` :
    le corps à relayer à la place de celui de la requête (Omarchy, qui
    renomme le modèle)."""
    if raw is None:
        raw = await request.body()
    payload = parse_json(raw)
    if INCOGNITO.get() and isinstance(payload, dict) \
            and isinstance(payload.get("tools"), list):
        # Incognito : pas de mémoire persistante, même déclarée.
        kept = [t for t in payload["tools"]
                if not (isinstance(t, dict) and t.get("type") == tools.ToolName.MEMORY)]
        payload["tools"] = kept

    # Le chat de la page : ses outils refusés à tous ([ui].page_tools_disabled)
    # sont retirés ici, quoi que la page déclare.
    if page and isinstance(payload, dict):
        page_tools.strip(payload)

    # Le routage d'abord : l'injection de tool_choice dépend du backend
    # visé (chacun peut la désactiver ou choisir sa valeur).
    backend = route_backend(payload)
    if backend is None:
        return no_route("openai", str(payload.get("model", "") or "")
                        if isinstance(payload, dict) else "")

    # Outils hébergés ([chat].hosted_tools) : seule une requête qui en
    # DÉCLARE un quitte le relais brut — sa déclaration est remplacée par
    # les fonctions du paquet tools/, avant l'injection de tool_choice
    # (qui compte les outils). Toute autre requête ne coûte que ce test.
    # [chat].always en présente D'OFFICE, sans déclaration, à un modèle
    # de conversation : la requête est alors traitée comme si elle les
    # avait déclarés ; s'il ne reste rien à présenter (noms pris par le
    # client, outils inactifs), elle repart en relais brut, intacte.
    ctx = hosted = None
    declared = always = False
    if chat_api.ENABLED:
        declared = chat_api.declares(payload, tools.kinds())
        model = str(payload.get("model") or "")
        always = not page and chat_api.unasked(payload) \
            and converses(backend, model) and not chat_api.spares(model)
    if declared or always:
        # Le chat de la page et les webhooks : les outils offerts au web.
        hosted = tools.Hosted(offer=tools.Offer.WEB if page else tools.Offer.API)
        try:
            ctx = chat_api.prepare(
                payload, hosted, tools.kinds(),
                chat_api.offered(hosted.tools) if always else ())
        except chat_api.Refused as exc:
            return error_response("openai", 400, "invalid_request_error",
                                  str(exc))
        ctx.progress = page
        if ctx.hosted:
            # Les échanges cachés des réponses précédentes, absents de
            # l'historique du client, reprennent leur place ([chat].memory),
            # cloisonnés par client. Incognito : rien n'est réinséré ni rangé.
            found = 0 if INCOGNITO.get() else chat_api.restore(
                payload, ctx, request_owner(request))
            if found:
                log.info("chat/completions : %d échange(s) d'outils hébergés "
                         "réinséré(s) depuis la mémoire (model=%s)", found,
                         payload.get("model"))
    if inject_tool_choice(payload, backend):
        log.info(
            "tool_choice=%s injecté (backend=%s, model=%s, %d tools)",
            backend.tool_choice, backend.name, payload.get("model"),
            len(payload["tools"]),
        )
    cap_max_tokens(payload, backend)
    fit_fields(payload, backend)
    # Nom PRÉFIXÉ tel que demandé : c'est la clé des stats (le corps, lui,
    # est dé-préfixé juste après pour l'upstream).
    model_key = str(payload["model"])
    raw = strip_backend_prefix(payload, backend)

    call = Call(backend, model_key, "/v1/chat/completions")
    blocked = await gate(call, request, payload, quotas.estimate_chat_cost(raw))
    if blocked is not None:
        return blocked
    if ctx is None or not ctx.hosted:
        return await forward(call, request, "v1/chat/completions", raw)
    return await chat_hosted(call, request, payload, ctx, hosted, raw)


async def chat_hosted(call: Call, request: Request, payload: dict, ctx,
                      hosted, raw: bytes) -> Response:
    """Une requête chat/completions à qui un outil hébergé est présenté :
    même départ que `forward` (statut et en-têtes du PREMIER upstream),
    puis la boucle commune (`hosted_loop`) avec le robinet
    chat_api.Translator, qui rend au client UNE réponse chat/completions.

    Chaque tour suivant repart du corps d'origine suivi de ce que le
    robinet a gardé des tours faits. Le client ne les aura pas dans son
    historique : le robinet les range à la conclusion, chat_api.restore
    les réinsère à la requête suivante."""
    base = payload.get("messages")
    base = list(base) if isinstance(base, list) else []
    # `payload` a déjà perdu son préfixe (strip_backend_prefix), et la
    # boucle le retire à chaque tour : il est reposé ici.
    model = f"{call.backend.name}/{payload.get('model') or ''}"

    def rebuild(robinet) -> dict:
        return {**payload, "model": model, "messages": base + robinet.history}

    def loop(robinet, upstream, estimate):
        # Pas d'événement `ping` en chat/completions : un commentaire SSE
        # (ligne «:», ignorée par tout lecteur de flux) tient la connexion
        # pendant une recherche ou l'attente d'un quota. ping_interval = 0 :
        # aucun (`_settled` sonderait sinon sans attendre).
        ping = b": ping\n\n" if robinet.sse \
            and settings.PING_INTERVAL > 0 else None
        # Le chat de la page : ses outils dont le résultat est écrit par
        # le modèle (ses bacs de code) ne font connaître aucun site.
        return hosted_loop(call, request, hosted, robinet, upstream, estimate,
                           rebuild, ping=ping, messages=base,
                           authored=page_tools.authored() if ctx.progress else ())

    # En JSON, la réponse prend le statut de l'issue de la boucle.
    return await start_hosted(
        call, request, raw,
        lambda status, ct: chat_api.Translator(status, ct, ctx), loop,
        json_status=lambda robinet: robinet.status, error_tap=default_tap)


async def start_hosted(call: Call, request: Request, raw: bytes, tap, loop,
                       json_status=None, error_tap=None) -> Response:
    """Le départ commun des trois surfaces quand un outil hébergé est
    présenté : même départ que `forward` (statut et en-têtes du PREMIER
    upstream), puis la boucle `loop(robinet, upstream, estimate)`, qui
    peut enchaîner plusieurs tours upstream.
      * erreur dès le premier tour : rien à boucler, le relais ordinaire —
        par le robinet, ou par `error_tap` s'il est donné ;
      * en flux : la boucle, telle quelle ;
      * en JSON, rien ne part avant la fin : la boucle est menée à son
        terme ICI (relaying.drain : arrêtée si le client raccroche), et la
        réponse prend le statut `json_status(robinet)`. Sans `json_status`
        (Responses, qui a un objet `failed`), le JSON part en flux comme
        le reste."""
    upstream = await relaying.send_upstream(call, request, "v1/chat/completions", raw)
    if isinstance(upstream, JSONResponse):
        return upstream
    estimate = quotas.estimate_chat_cost(raw)
    status, content_type = upstream.status_code, upstream.headers.get("content-type", "")
    if status >= 400 and error_tap is not None:
        return StreamingResponse(
            relay(call, upstream, error_tap(status, content_type), estimate),
            status_code=status, headers=response_headers(upstream))
    robinet = tap(status, content_type)
    if not robinet.ok:
        return StreamingResponse(relay(call, upstream, robinet, estimate),
                                 status_code=status,
                                 headers=response_headers(upstream))
    stream = loop(robinet, upstream, estimate)
    if robinet.sse or json_status is None:
        return StreamingResponse(stream, status_code=status,
                                 headers=response_headers(upstream))
    content = await relaying.drain(stream, call, request)
    if content is None:
        return Response(status_code=CLIENT_CLOSED)
    return Response(content, status_code=json_status(robinet),
                    media_type="application/json")


# ── Surface Anthropic ───────────────────────────────────────────────────

@router.post("/v1/messages")
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
    resolved = anthropic_api.resolve_model(requested, visible())
    if resolved is None:
        return error_response(
            dialect, 400, "unknown_backend_prefix",
            f"modèle «{requested}» inconnu : l'ajouter à "
            f"[anthropic.model_map] (ou «default»), ou le préfixer "
            f"par un backend",
        )
    payload["model"] = resolved
    backend = route_backend(payload)
    if backend is None:
        return no_route(dialect, resolved)
    if requested and requested != resolved:
        log.info("anthropic : modèle %r → %r", requested, resolved)

    images = False
    if backend.images and anthropic_api.has_images(payload):
        images = await accepts_images(backend, resolved[len(backend.name) + 1:])
    # Aucun outil hébergé = None : les outils serveur `web_search_…` et
    # `web_fetch_…` du client sont ignorés. `ctx.hosted` n'est rempli que
    # de ceux que la requête DÉCLARE.
    hosted = tools.Hosted() or None
    ctx = anthropic_api.Context(payload, hosted)
    openai_payload = anthropic_api.to_openai(payload, images=images,
                                             hosted=hosted)
    if inject_tool_choice(openai_payload, backend):
        log.info(
            "tool_choice=%s injecté (backend=%s, model=%s, %d tools)",
            backend.tool_choice, backend.name, resolved,
            len(openai_payload["tools"]),
        )
    cap_max_tokens(openai_payload, backend)
    fit_fields(openai_payload, backend)
    raw = strip_backend_prefix(openai_payload, backend)

    call = Call(backend, resolved, "/v1/messages", dialect)
    cost = quotas.estimate_chat_cost(raw)
    tap = lambda status, ct: anthropic_api.Translator(status, ct, resolved, ctx)
    if ctx.hosted:
        base = payload.get("messages")
        base = list(base) if isinstance(base, list) else []

        def rebuild(robinet) -> dict:
            # Le tour suivant : les messages d'origine, puis CE que le
            # robinet a déjà rendu, en message assistant — relu par
            # to_openai comme le sera le rejeu du client.
            return anthropic_api.to_openai(
                {**payload, "messages": base + [
                    {"role": "assistant", "content": robinet.content}]},
                images=images, hosted=hosted, results=robinet.results)

        def body(robinet, upstream, estimate=cost):
            # En flux, des `ping` pendant les attentes de la boucle
            # (recherche en cours, quota d'un tour suivant) : même raison
            # que pinged_stream, Claude Code coupe un flux muet.
            ping = anthropic_api.ping_event() \
                if robinet.sse and settings.PING_INTERVAL > 0 else None
            return hosted_loop(call, request, hosted, robinet, upstream,
                               estimate, rebuild, limit=ctx.limits,
                               options=ctx.options, ping=ping,
                               messages=openai_payload.get("messages"))
    else:
        body = None

    if openai_payload.get("stream") and backend.quotas \
            and settings.PING_INTERVAL > 0:
        return StreamingResponse(
            pinged_stream(call, request, openai_payload, raw, cost, tap, body),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache"},
        )
    blocked = await gate(call, request, openai_payload, cost)
    if blocked is not None:
        return blocked
    if body is None:
        return await forward(call, request, "v1/chat/completions", raw, tap=tap)

    # En JSON, un backend qui tombe au deuxième tour donne un vrai 503 à la
    # forme Anthropic, pas un 200 dont le corps serait une erreur (un SDK
    # le lirait comme un Message).
    return await start_hosted(call, request, raw, tap, body,
                              json_status=lambda robinet: call.status or 200)


async def pinged_stream(call: Call, request: Request, payload: dict,
                        raw: bytes, cost: int, tap, body=None):
    """Flux Anthropic derrière un limiteur : le 200 part TOUT DE SUITE et
    des `event: ping` tiennent la connexion pendant l'attente du quota —
    un flux muet de plusieurs minutes, Claude Code le coupe. Ce que
    `gate` rendait en réponse HTTP devient ici un `event: error` ; un
    client qui raccroche annule ce générateur, l'attente s'arrête et le
    quota n'est pas consommé (499).
    `body(robinet, upstream)` : le générateur qui rend la réponse une
    fois le premier upstream ouvert, à la place du relais ordinaire — la
    boucle des outils hébergés (messages())."""
    limiter = call.backend.quota_state.get_limiter(payload)
    waiter = None
    acquired = False

    async def pings(task):
        while True:
            done, _ = await asyncio.wait({task},
                                         timeout=settings.PING_INTERVAL)
            if done:
                return
            yield anthropic_api.ping_event()

    try:
        # Le partage équitable d'abord, comme `gate` : sans lui un client
        # en flux passait outre sa part. Le départ du client annule ce
        # générateur, donc l'attente : `gone` n'a pas à le guetter.
        waiter = asyncio.ensure_future(
            relaying.share_gate(call, asyncio.Event().wait))
        async for ping in pings(waiter):
            yield ping
        if not waiter.result():
            acquired = True          # déjà compté (499)
            return
        waiter = asyncio.ensure_future(
            limiter.acquire(cost, ticket=call.tickets))
        async for ping in pings(waiter):
            yield ping
        try:
            waited = waiter.result()
        except quotas.QuotaWaitTooLong as exc:
            acquired = True          # compté par quota_error : pas « parti »
            body = json.loads(quota_error(call, exc, limiter).body)
            yield anthropic_api.sse_error(body)
            return
        acquired = True
        released(call, limiter, waited, cost)

        upstream = await relaying.send_upstream(call, request, "v1/chat/completions", raw)
        if isinstance(upstream, JSONResponse):
            yield anthropic_api.sse_error(json.loads(upstream.body))
            return
        robinet = tap(upstream.status_code,
                      upstream.headers.get("content-type", ""))
        if robinet.ok:
            stream = body(robinet, upstream) if body else \
                relay(call, upstream, robinet, cost)
            async for chunk in stream:
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
            if waiter is not None:
                waiter.cancel()
            client_gone(call, limiter)


@router.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    """Aucun équivalent OpenAI. Exact si le backend visé expose un
    endpoint de tokenisation ([backends.<nom>].tokenize_path — llama.cpp
    a /tokenize) ; estimation locale sinon, ou s'il ne répond pas. Un
    chiffre juste évite que la jauge de contexte du client dérive."""
    if not anthropic_api.ENABLED:
        return anthropic_disabled("anthropic")
    payload = parse_json(await request.body())
    if not isinstance(payload, dict):
        return error_response("anthropic", 400, "invalid_request_error",
                              "corps JSON attendu")
    resolved = anthropic_api.resolve_model(
        str(payload.get("model", "") or ""), visible())
    backend = route_backend({"model": resolved})
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


# ── Surface Responses ───────────────────────────────────────────────────

@router.post("/v1/responses")
async def responses(request: Request):
    """L'API Responses (Codex CLI), traduite vers /v1/chat/completions du
    backend que désigne le préfixe du modèle. La réponse repasse par
    responses_api.Translator (JSON ou flux d'événements `response.*`)."""
    if not responses_api.ENABLED:
        return error_response(
            "openai", 404, "unknown_route",
            "surface Responses désactivée sur ce proxy ([responses].enabled "
            "dans config.toml)",
        )
    payload = parse_json(await request.body())
    if not isinstance(payload, dict):
        return error_response("openai", 400, "invalid_request_error",
                              "corps JSON attendu")
    model_key = str(payload.get("model", "") or "")
    backend = route_backend(payload)
    if backend is None:
        return no_route("openai", model_key)

    images = False
    if backend.images and responses_api.has_images(payload):
        images = await accepts_images(backend, model_key[len(backend.name) + 1:])
    # Aucun outil hébergé = None : un `web_search` déclaré est ignoré.
    hosted = tools.Hosted() or None
    # Propriétaire des résultats gardés en mémoire (jamais la clé). Proxy
    # ouvert = un seul client, quel que soit le Bearer.
    client = request_owner(request)
    try:
        chat_payload, ctx = responses_api.to_chat(payload, images=images,
                                                  hosted=hosted, client=client)
    except responses_api.Refused as exc:
        return error_response("openai", 400, "invalid_request_error", str(exc))
    if ctx.ignored:
        log.info("responses : %s sans équivalent chat, ignoré(s) (model=%s)",
                 ", ".join(sorted(set(ctx.ignored))), model_key)
    if ctx.custom or ctx.shell:
        log.info("responses : outil(s) du client présenté(s) en fonction : "
                 "%s (model=%s)", ", ".join(
                     [f"custom {name}" for name, _ in ctx.custom.values()]
                     + (["local_shell"] if ctx.shell else [])), model_key)
    if inject_tool_choice(chat_payload, backend):
        log.info(
            "tool_choice=%s injecté (backend=%s, model=%s, %d tools)",
            backend.tool_choice, backend.name, model_key,
            len(chat_payload["tools"]),
        )
    cap_max_tokens(chat_payload, backend)
    fit_fields(chat_payload, backend)
    raw = strip_backend_prefix(chat_payload, backend)

    call = Call(backend, model_key, "/v1/responses")
    blocked = await gate(call, request, chat_payload,
                         quotas.estimate_chat_cost(raw))
    if blocked is not None:
        return blocked
    tap = lambda status, ct: responses_api.Translator(status, ct, ctx)
    if not ctx.hosted:
        return await forward(call, request, "v1/chat/completions", raw, tap=tap)

    # Le client a déclaré un outil que le proxy héberge. En JSON aussi la
    # réponse part en flux : l'API Responses a un objet `failed` pour dire
    # l'échec d'un tour suivant.
    return await start_hosted(
        call, request, raw, tap,
        lambda robinet, upstream, estimate: hosted_stream(
            call, request, payload, images, hosted, robinet, upstream,
            estimate, messages=chat_payload.get("messages")))


async def forward_post(request: Request, normalized: str, dialect: str):
    """Relaie le POST de `request` vers la route `normalized` du backend
    que nomme son modèle (JSON ou formulaire) : préfixe retiré, porte de
    quota, flux. Le relais générique, et /ui/images/generations."""
    raw = await request.body()
    payload = parse_json(raw)
    content_type = request.headers.get("content-type", "")
    if payload is None and multipart.is_multipart(content_type):
        # Formulaire (transcription audio, édition d'image) : le préfixe du
        # champ model route aussi, et se retire comme en JSON.
        model_key = multipart.model_field(raw, content_type) or ""
        backend = route_backend({"model": model_key})
        if backend is None:
            return no_route(dialect, model_key)
        raw = multipart.rewrite_model_field(
            raw, content_type, model_key[len(backend.name) + 1:])
    else:
        backend = route_backend(payload)
        if backend is None:
            model = payload.get("model", "") if isinstance(payload, dict) else ""
            return no_route(dialect, str(model or ""))
        model_key = str(payload["model"])
        cap_max_tokens(payload, backend)
        fit_fields(payload, backend)
        raw = strip_backend_prefix(payload, backend)

    call = Call(backend, model_key, normalized, dialect)
    blocked = await gate(call, request, payload,
                         quotas.estimate_generic_cost(raw, payload))
    if blocked is not None:
        return blocked
    return await forward(call, request, normalized.lstrip("/"), raw)


# ── Catalogue ────────────────────────────────────────────────────────────

@router.get("/v1/models")
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
