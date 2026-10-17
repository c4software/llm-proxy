"""
L'application FastAPI : son démarrage (lifespan), l'assemblage des
routes et des middlewares, et les routes qui ne sont à aucune surface —
/healthz, /v1/tools, les fichiers rendus, /mcp, /v1/audio/voices et le
catch-all (forward_post_paths, 404 local sinon). Le reste vit à côté :

  auth.py       clés, session de la page, middleware d'auth (rôle 5)
  relaying.py   le relais vers les backends : Call, gate, forward
  catalog.py    le catalogue des modèles (GET /v1/models, rôle 3)
  surfaces.py   /v1/chat/completions, /v1/messages, /v1/responses
  tool_loop.py  la boucle des outils hébergés, commune aux surfaces
  usage_api.py  l'Usage API (rôle 4bis)
  ui_routes.py  les routes du tableau de bord (/ui/*)
  httputil.py   forme des erreurs, corps JSON

Rôles du proxy :
  1. peut injecter tool_choice quand `tools` est présent sans `tool_choice`
     (le défaut du schéma Albert est "none", ce qui casse le tool calling).
     RIEN N'EST INJECTÉ par défaut : cela s'active PAR BACKEND via
     [backends.<nom>].force_tool_choice ;
  2. porte les clés upstream pour tous les clients ;
  3. multi-backends, routés par le PRÉFIXE du modèle — voir backends.py.
     GET /v1/models interroge les backends en direct et fusionne les
     catalogues, chaque id exposé préfixé ;
  4. ne transmet QUE les routes nécessaires (proxy.forward_post_paths,
     /v1/chat/completions, /v1/models, GET /v1/audio/voices) — toute autre
     URL reçoit un 404 local, rien n'est relayé aveuglément aux backends.
     Un corps multipart (transcription audio, édition d'image) est routé
     par son champ model, préfixe retiré, comme un corps JSON (multipart.py) ;
  4bis. GET /v1/organization/usage/completions : l'Usage API d'OpenAI,
     servie depuis les compteurs du proxy (SQLite, une ligne par requête
     — voir stats.py). C'est la SEULE lecture des statistiques. GET /ui
     en est le tableau de bord — une page HTML statique
     (web/templates/index.html) que web/static/dashboard.js remplit en
     appelant cette même route sous /ui/usage, en vues All / Week / Day ;
     GET /v1/organization/usage/tools en est la route sœur, de même
     forme, pour les exécutions d'outils hébergés (/ui/usage/tools) ;
  4ter. la surface Anthropic (anthropic_api.py), si [anthropic].enabled :
     POST /v1/messages et /v1/messages/count_tokens, et GET /v1/models à
     la forme Anthropic quand la requête porte `anthropic-version`. Un
     client Claude Code s'y branche avec ANTHROPIC_BASE_URL. Son outil
     serveur `web_search_…` (la sous-requête de son `WebSearch`) est
     exécuté ici quand la recherche hébergée est active — de même
     l'outil serveur `web_fetch_…` d'un client du SDK ;
  4quater. la surface Responses (responses_api.py), si [responses].enabled :
     POST /v1/responses, traduit vers /v1/chat/completions comme la
     surface Anthropic. Un client Codex CLI s'y branche avec
     wire_api = "responses". Les outils HÉBERGÉS (paquet tools/ :
     recherche web) y sont exécutés ici même : le backend est relancé
     avec leurs résultats jusqu'à la réponse finale — `hosted_loop`
     (tool_loop.py), la boucle commune aux surfaces ;
  4quinquies. les outils hébergés sur /v1/chat/completions (chat_api.py),
     si [chat].hosted_tools : une requête qui DÉCLARE `{"type":
     "web_search"}` dans `tools` passe par la même boucle et reçoit une
     réponse chat/completions ordinaire ; toute autre est relayée brute.
     [chat].always nomme des outils présentés D'OFFICE, sans déclaration
     (une interface de chat n'en fait pas). Les appels hébergés, que le client ne voit pas, sont réinsérés dans
     son historique à la requête suivante ([chat].memory) ;
  5. auth optionnelle du proxy lui-même : proxy.api_keys (liste vide par
     défaut = ouvert) exige des clients un «Authorization: Bearer <clé>»
     à la OpenAI — ou «x-api-key: <clé>», à l'Anthropic — (401 sinon,
     /healthz exempté). C'est la clé DU PROXY : jamais relayée aux
     backends quand l'auth est active. Avec [oidc] (oidc.py), des
     COMPTES : la page s'ouvre par le fournisseur OIDC, et chaque compte
     y crée ses propres clés (accounts.py), valables à côté de celles du
     TOML ;

Chaque requête relayée est portée par UN objet `Call` (relaying.py) :
c'est lui qui écrit la ligne de stats, une fois, quel que soit le chemin
de sortie.

TOUTE la configuration vit dans data/config.toml (voir config.py).
Tout ce qui est spécifique à Albert (limiteur de quotas, familles,
association routeurs ↔ modèles) vit dans albert.py.
"""

import asyncio
import dataclasses
import json
import mimetypes
import os
from contextlib import asynccontextmanager

import httpx2
from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, RedirectResponse

from . import accounts
from . import albert
from . import anthropic_api
from . import auth
from . import catalog
from . import chat_api
from . import config
from . import files
from . import mcp_server
from . import oidc
from . import responses_api
from . import stats
from . import surfaces
from . import tools
from . import ui_routes
from . import usage_api
from .backends import (
    BACKENDS, FALLBACK_BACKEND, backend_offline_message, close_clients,
    open_clients, route_backend, unknown_prefix_message,
)
from .settings import EXEMPT_SUFFIXES, FORWARD_POST_PATHS, TOOL_CHOICE, log
from .auth import (
    UISession, auth_required, client_token, key_client, request_owner,
)
from .catalog import CHAT_TYPES, always_shown
from .httputil import dialect_of, error_response, parse_json
from .relaying import clean_headers, response_headers
from .surfaces import forward_post
from .ui_routes import WEB_DIR, ui_login_mode

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("configuration : %s", config.CONFIG_PATH)
    stats.init()
    await open_clients()
    # Serveurs MCP ([tools.mcp.<serveur>]) : découverte de leurs outils,
    # attendue quelques secondes au plus — un serveur éteint ne retient
    # pas le démarrage, ses outils s'enregistrent quand il répond.
    await tools.mcp.start()

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
    if auth.PROXY_API_KEYS:
        log.info(
            "auth proxy ACTIVE : %d clé(s) acceptée(s), /healthz exempté",
            len(auth.PROXY_API_KEYS),
        )
    elif not oidc.ENABLED:
        log.info("auth proxy inactive (proxy.api_keys vide) : proxy ouvert")
    if oidc.ENABLED:
        log.info("comptes OIDC ACTIFS (%s) : /ui par session OIDC seulement, "
                 "clés gérées par compte (%s)", oidc.ISSUER, accounts.DB_PATH)
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
    if responses_api.ENABLED:
        log.info("surface Responses ACTIVE : POST /v1/responses, traduit "
                 "vers /v1/chat/completions (Codex CLI)")
    else:
        log.info("surface Responses inactive ([responses].enabled absent "
                 "ou false) : /v1/responses → 404")
    if tools.enabled():
        log.info(
            "outils hébergés ACTIFS (/v1/tools, et selon l'outil "
            "/v1/responses, /v1/messages, /v1/chat/completions) : %s | "
            "%d appels au plus par réponse",
            ", ".join(t.name for t in tools.enabled()), tools.MAX_CALLS,
        )
    else:
        log.info("aucun outil hébergé ([tools.<nom>].enabled absent ou "
                 "false%s) : `web_search` d'un client Responses ou Anthropic "
                 "est ignoré", ", aucun serveur MCP joint"
                 if tools.mcp.SERVERS else "")
    log.info(
        "outils hébergés sur /v1/chat/completions : %s",
        "ACTIFS — une requête qui déclare `{\"type\": \"web_search\"}` "
        "dans `tools` est bouclée par le proxy" if chat_api.ENABLED
        else "inactifs ([chat].hosted_tools absent ou false) : relais brut")
    if chat_api.ALWAYS:
        known = {t.name for t in tools.REGISTRY}
        servers = {f"{chat_api.SERVER}{s.name}" for s in tools.mcp.SERVERS}
        shown = always_shown()
        for name in chat_api.ALWAYS:
            if name in servers:
                # Un serveur éteint ou sans outil pour l'instant : ses
                # outils seront présentés dès leur découverte.
                continue
            if name.startswith(chat_api.SERVER) or name == "mcp":
                log.warning(
                    "[chat].always : «%s» ne désigne aucun serveur de "
                    "[tools.mcp.<serveur>] (forme admise : «mcp:<serveur>», "
                    "serveur par serveur) — ignoré", name)
            elif name not in shown:
                log.warning(
                    "[chat].always : outil «%s» %s — ignoré tant qu'il "
                    "l'est", name,
                    f"désactivé ([tools.{name}].enabled)" if name in known
                    else "inconnu du registre")
        if chat_api.ENABLED:
            log.info(
                "présentés D'OFFICE à toute requête /v1/chat/completions, "
                "sans déclaration ([chat].always) : %s%s",
                ", ".join(shown) or "aucun pour l'instant",
                f" — sauf aux modèles {', '.join(chat_api.ALWAYS_EXCEPT)} "
                f"([chat].always_except)" if chat_api.ALWAYS_EXCEPT else "")
        else:
            log.warning("[chat].always sans effet : [chat].hosted_tools "
                        "absent ou false")
    # Garde-fou de téléchargement ([tools.net]) : ses clés lues à leur
    # ancienne place, [tools.web_fetch].
    if tools.net.LEGACY:
        log.warning(
            "[tools.web_fetch].%s : ces clés sont celles du garde-fou "
            "commun à tous les outils qui téléchargent — lues ici faute de "
            "[tools.net], à déplacer dans [tools.net]",
            ", ".join(tools.net.LEGACY))
    if tools.net.SHADOWED:
        log.warning(
            "[tools.web_fetch].%s IGNORÉ : [tools.net] règle déjà ces clés",
            ", ".join(tools.net.SHADOWED))
    if tools.net.ALLOW_PRIVATE:
        log.warning("[tools.net].allow_private = true : les outils qui "
                    "téléchargent joignent AUSSI les adresses privées")
    # Exécution de code : l'exécuteur n'est PAS sondé au démarrage (le
    # proxy ne dépend pas de lui) ; seule la configuration est dite.
    ce = tools.code_execution
    if ce.ENABLED:
        if not ce.URL or not ce.TOKEN:
            log.warning(
                "code_execution actif mais NON configuré ([tools."
                "code_execution].url et .token) : l'outil répondra au "
                "modèle que l'exécution n'est pas disponible")
        else:
            log.info(
                "code_execution : exécuteur %s (joint à la demande ; "
                "injoignable, l'outil rend une erreur au modèle), %d s par "
                "programme, %d appels par réponse", ce.URL, int(ce.TIMEOUT),
                ce.MAX_CALLS)
        if not files.PUBLIC_URL:
            log.warning(
                "code_execution : [files].public_url est vide — les "
                "fichiers produits ne seront PAS remis au client (aucun "
                "lien ne peut être écrit), et le modèle en est prévenu")
    # Conversion audio de `transcribe` : ffmpeg, s'il est là.
    tr = tools.transcribe
    if tr.ENABLED and len(tr.ACCEPTED) < len(tr.FORMATS):
        if tr.FFMPEG:
            log.info("transcribe : hors %s, l'audio est converti en WAV par "
                     "%s", ", ".join(tr.ACCEPTED), tr.FFMPEG)
        elif tr.CONVERT and "wav" in tr.ACCEPTED:
            log.warning(
                "transcribe : ffmpeg introuvable — l'audio qui n'est pas du "
                "%s sera REFUSÉ (l'image Docker du proxy l'embarque ; hors "
                "conteneur, l'installer ou régler [tools.transcribe].ffmpeg)",
                ", ".join(tr.ACCEPTED))
        else:
            log.info("transcribe : pas de conversion — seul l'audio en %s "
                     "est transcrit", ", ".join(tr.ACCEPTED))
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
    await tools.mcp.stop()
    await close_clients()
    stats.close()


app = FastAPI(title="albert-proxy", lifespan=lifespan)

# Feuille de style et script du tableau de bord : servis par le proxy
# lui-même, aucun CDN. Monté AVANT le catch-all, qui sinon répondrait
# 404. Le mount reste sous /ui : l'auth du proxy s'y applique.
# Le manifeste de l'application installable : un type que `mimetypes` ne
# connaît pas toujours.
mimetypes.add_type("application/manifest+json", ".webmanifest")
app.mount("/ui/static", StaticFiles(directory=os.path.join(WEB_DIR, "static")),
          name="static")

# L'auth (auth.py) : le middleware, puis la session de la page AUTOUR de
# lui (ajoutée après, donc exécutée avant : la session y est déjà lue).
app.middleware("http")(auth.require_proxy_key)
app.add_middleware(UISession)

# Les routes des autres modules, AVANT le catch-all (passthrough, en fin
# de fichier), qui sinon les prendrait.
for module in (surfaces, catalog, usage_api, ui_routes):
    app.include_router(module.router)


@app.get("/healthz")
async def healthz(request: Request):
    """Vivant ? Sans clé (auth active) : `{"status": "ok"}`, et rien de
    plus — ni URL de backend, ni modèles, ni outils : c'est ce que lit le
    HEALTHCHECK du conteneur. Avec une clé valide (en-tête), ou proxy
    ouvert : l'état détaillé (health_detail). La page le lit sous sa
    session, par GET /ui/health."""
    if auth_required() and await key_client(client_token(request)) is None:
        return {"status": "ok"}
    return health_detail()


@app.get("/ui/health")
async def ui_health():
    """L'état détaillé, pour la page : sous /ui, la session suffit."""
    return health_detail()


def health_detail() -> dict:
    """L'état du déploiement : backends, surfaces, outils, caches."""
    return {
        "status": "ok",
        "tool_choice": TOOL_CHOICE,
        "auth_required": auth_required(),
        # Comment la page s'ouvre : «oidc» (comptes), «key» (une clé du
        # TOML), None (proxy ouvert).
        "ui_login": ui_login_mode(),
        "anthropic": {
            "enabled": anthropic_api.ENABLED,
            "model_map": anthropic_api.MODEL_MAP,
        },
        "responses": {"enabled": responses_api.ENABLED},
        # Le serveur MCP (POST /mcp) : de quoi savoir, sans clé, si un
        # client MCP (pi, omp, Claude Code) peut s'y brancher.
        "mcp_server": {"enabled": mcp_server.ENABLED},
        "chat": {"hosted_tools": chat_api.ENABLED,
                 # Présentés d'office EN CE MOMENT : les noms de
                 # [chat].always qui sont ceux d'un outil actif.
                 "always": always_shown() if chat_api.ENABLED else [],
                 "always_except": chat_api.ALWAYS_EXCEPT,
                 "memory": chat_api.MEMORY_ENABLED,
                 "memory_entries": len(chat_api.MEMORY),
                 "memory_chars": chat_api.MEMORY.size},
        # Le magasin des fichiers rendus par les outils (files.py) :
        # peut-il écrire des liens, et ce qu'il garde.
        "files": {"public_url": bool(files.PUBLIC_URL),
                  "entries": len(files.STORE), "bytes": files.STORE.size},
        "tools": {
            "enabled": [t.name for t in tools.enabled()],
            "max_calls": tools.MAX_CALLS,
            "memory_entries": len(tools.MEMORY),
            # Cache web (tools/webcache.py) : pages et recherches gardées
            # quelques minutes, et ce qu'il a épargné depuis le démarrage.
            "web_cache": {"ttl": tools.webcache.TTL,
                          "entries": len(tools.webcache.CACHE),
                          "bytes": tools.webcache.CACHE.size,
                          "hits": tools.webcache.CACHE.hits,
                          "misses": tools.webcache.CACHE.misses},
            # Cache des transcriptions (le texte, jamais l'audio).
            # Serveurs MCP de la configuration : joints ou non, leur
            # protocole, leurs outils — jamais leurs en-têtes.
            "mcp": tools.mcp.status(),
            "transcribe_cache": {"ttl": tools.transcribe.CACHE.ttl,
                                 "entries": len(tools.transcribe.CACHE),
                                 "hits": tools.transcribe.CACHE.hits,
                                 "misses": tools.transcribe.CACHE.misses},
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
                # Ceux qu'on peut proposer à un client de chat (ni image,
                # ni voix, ni embeddings) : le tableau de bord y prend ses
                # exemples. Vide tant que le catalogue n'a pas été lu.
                "chat_models": sorted(
                    f"{name}/{m}" for m in b.models
                    if b.model_types.get(m, "text-generation") in CHAT_TYPES),
                **(b.quota_state.snapshot() if b.quotas else {}),
            }
            for name, b in BACKENDS.items()
        },
        "generic_limits": {"rpm": albert.GENERIC_RPM, "tpm": albert.GENERIC_TPM},
        "max_queue_seconds": albert.MAX_QUEUE_SECONDS,
        "exempt_paths": list(EXEMPT_SUFFIXES),
    }


@app.get("/")
async def root():
    return RedirectResponse("/ui")


# ── Outils hébergés, appelés directement ────────────────────────────────
# Les mêmes outils que la boucle de /v1/responses, pour un client qui
# préfère les déclarer LUI-MÊME à son modèle et garder l'appel et son
# résultat dans son propre historique (c'était le chemin de l'extension
# pi / omp llm-proxy-web.ts, retirée le 07/10/2026 : ces clients reçoivent
# désormais les outils d'office, [chat].always). Le
# proxy n'a alors ni boucle ni mémoire à tenir : il exécute, c'est tout.
# Derrière la clé du proxy comme le reste ; mêmes garde-fous (tools/).

@app.get("/v1/tools")
async def tools_list():
    """Les outils hébergés actifs, à la forme d'une déclaration de
    fonction : de quoi les présenter tels quels à un modèle. C'est aussi
    là que se relit le PROMPT en vigueur de chacun (Tool.prompt,
    Tool.parameters), par la même fabrique que les surfaces (Tool.spec) :
    ce qu'un modèle lirait si tous les outils actifs lui étaient
    présentés ensemble."""
    active = tools.enabled()
    present = frozenset(t.name for t in active)
    return {"object": "list", "data": [
        {"name": t.name, **{k: v for k, v in t.spec(present)[
            "function"].items() if k != "name"}}
        for t in active]}


@app.post("/v1/tools/{name}")
async def tools_run(name: str, request: Request):
    """Exécute un outil hébergé. Corps : ses arguments (objet JSON).
    Toujours 200 quand l'outil existe : un échec de l'outil est un texte
    «Error: …» que le modèle doit lire, pas une erreur HTTP — `is_error`
    le signale au client, `error` en donne le code (tools.ERRORS, null
    pour un succès). `sources` et `meta` : ce que le résultat dit en plus
    de son texte (pages citées ; URL lue, titre, plage de caractères…),
    pour l'affichage — voir docs/outils.md."""
    hosted = tools.Hosted()
    if name not in hosted.by_name:
        return error_response(
            "openai", 404, "unknown_tool",
            f"outil «{name}» inconnu ou désactivé ([tools.{name}] dans "
            f"config.toml) ; actifs : {', '.join(hosted.by_name) or 'aucun'}")
    args = parse_json(await request.body())
    if not isinstance(args, dict):
        return error_response("openai", 400, "invalid_request_error",
                              "corps attendu : les arguments de l'outil, "
                              "en objet JSON")
    client = request_owner(request)
    result = await hosted.run(
        name, json.dumps(args, ensure_ascii=False), 0, endpoint="/v1/tools",
        client=client)
    return {"name": name, "result": result.text,
            "is_error": result.error is not None, "error": result.error,
            "sources": [dataclasses.asdict(s) for s in result.sources],
            "meta": dict(result.meta),
            # Les fichiers produits, rangés au magasin : des LIENS.
            "files": [{"name": s.name, "media_type": s.media_type,
                       "size": len(s.data), "url": s.url}
                      for s in files.keep(result.files, client)]}


@app.api_route(files.PATH + "{token}/{name}", methods=["GET", "HEAD"])
async def rendered_file(token: str, name: str):
    """Un fichier rendu par un outil hébergé (files.py). HORS clé du
    proxy (require_proxy_key) : le jeton du chemin, imprévisible, vaut
    droit d'accès. Inconnu, expiré ou mal nommé : le même 404."""
    stored = files.STORE.get(token, name)
    if stored is None:
        return error_response("openai", 404, "file_not_found",
                              "fichier inconnu ou expiré")
    return Response(stored.data, headers=stored.headers())


# ── Serveur MCP ─────────────────────────────────────────────────────────
# Les mêmes outils que /v1/tools, au Model Context Protocol (transport
# streamable HTTP, sans état) : Claude Code, Codex, pi et omp s'y branchent
# comme sur n'importe quel serveur MCP distant. Voir mcp_server.py.

def mcp_disabled() -> JSONResponse:
    return error_response("openai", 404, "unknown_route",
                          "serveur MCP désactivé ([mcp_server].enabled dans config.toml)")


@app.post("/mcp")
async def mcp_post(request: Request):
    """Un message JSON-RPC (ou un lot) → sa réponse en JSON ; 202 sans
    corps pour une notification. Erreur de protocole : statut HTTP 400 et
    erreur JSON-RPC dans le corps."""
    if not mcp_server.ENABLED:
        return mcp_disabled()
    if not mcp_server.origin_allowed(request.headers.get("origin", ""),
                                  request.headers.get("host", "")):
        log.warning("MCP : origine refusée %s → 403",
                    request.headers.get("origin", ""))
        return JSONResponse(mcp_server.error(None, mcp_server.INVALID_REQUEST,
                                          "origin not allowed"), status_code=403)
    body = parse_json(await request.body())
    if body is None:
        return JSONResponse(mcp_server.error(None, mcp_server.PARSE_ERROR,
                                          "invalid JSON"), status_code=400)
    client = request_owner(request)
    out = await mcp_server.handle_body(body, tools.Hosted(), client)
    if out is None:
        return Response(status_code=202)
    bad = isinstance(out, dict) and "error" in out and out.get("id") is None
    return JSONResponse(out, status_code=400 if bad else 200)


@app.api_route("/mcp", methods=["GET", "DELETE"])
async def mcp_other(request: Request):
    """Pas de flux serveur ni de session à fermer : 405, comme le prévoit
    la spécification pour un serveur sans état."""
    if not mcp_server.ENABLED:
        return mcp_disabled()
    return Response(status_code=405, headers={"Allow": "POST"})


@app.get("/v1/audio/voices")
async def audio_voices(request: Request):
    """Voix d'un modèle de synthèse (GET, ?model=<backend>/<modèle>) : routé
    par le préfixe du paramètre, retiré avant relais comme pour un corps. Le
    relais générique ne transmet que des POST."""
    dialect = dialect_of(request)
    model = request.query_params.get("model", "")
    backend, prefixed = route_backend({"model": model} if model else None)
    if backend is None:
        return error_response(dialect, 400, "unknown_backend_prefix",
                              unknown_prefix_message(model))
    params = dict(request.query_params)
    if prefixed:
        params["model"] = model[len(backend.name) + 1:]
    try:
        upstream = await backend.client.get(
            "/v1/audio/voices", params=params,
            headers=clean_headers(request, backend.api_key),
            timeout=backend.meta_timeout)
    except httpx2.RequestError as exc:
        if not backend.quotas:
            return error_response(dialect, 503, "backend_offline",
                                  backend_offline_message(backend, exc))
        return error_response(dialect, 502, "proxy_error", f"upstream unreachable: {exc}")
    return Response(content=upstream.content, status_code=upstream.status_code,
                    headers=response_headers(upstream))


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

    return await forward_post(request, normalized, dialect)
