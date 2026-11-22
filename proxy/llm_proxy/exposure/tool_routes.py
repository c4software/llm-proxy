"""
Les outils hébergés exposés tels quels, hors de toute boucle : appel
direct (/v1/tools), serveur MCP (/mcp, voir mcp_server.py), et les
fichiers qu'ils rendent (files.PATH, hors clé du proxy).
"""

import collections
import dataclasses
import json
import time

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from .. import tools
from ..core import config
from ..access.auth import request_owner
from ..core.httputil import error_response, parse_json
from ..core.settings import log
from ..storage import files
from ..tools import provenance
from . import mcp_server

router = APIRouter()


# Appels directs par client et par minute glissante ([tools]
# .direct_calls_per_minute, 0 = sans limite) : sans boucle ni max_calls, un
# client pouvait enchaîner recherches et exécutions de code sans borne.
DIRECT_PER_MINUTE = config.integer("tools.direct_calls_per_minute", 120)
WINDOW = 60.0
_direct: dict[str, collections.deque] = {}


def direct_wait(client: str, calls: int = 1,
                now: float | None = None) -> float:
    """Réserve `calls` appels directs pour `client`. 0 : accepté (et
    compté) ; sinon les secondes avant qu'une place se libère (rien n'est
    compté)."""
    if DIRECT_PER_MINUTE <= 0 or calls <= 0:
        return 0.0
    now = time.monotonic() if now is None else now
    q = _direct.setdefault(client, collections.deque())
    while q and q[0] <= now - WINDOW:
        q.popleft()
    if len(q) + calls > DIRECT_PER_MINUTE:
        if calls > DIRECT_PER_MINUTE or not q:
            return WINDOW
        return max(q[len(q) + calls - DIRECT_PER_MINUTE - 1] + WINDOW - now,
                   0.001)
    q.extend([now] * calls)
    # Les clients inactifs ne s'accumulent pas.
    if len(_direct) > 4096:
        for k in [k for k, v in _direct.items() if not v or v[-1] <= now - WINDOW]:
            del _direct[k]
    return 0.0


def _too_many(delay: float):
    retry = max(int(delay + 0.999), 1)
    return retry, (f"trop d'appels directs d'outils : {DIRECT_PER_MINUTE} par "
                   f"minute au plus ([tools].direct_calls_per_minute) ; "
                   f"réessayer dans ~{retry}s")


# ── Outils hébergés, appelés directement ────────────────────────────────
# Les mêmes outils que la boucle des surfaces, pour un client qui préfère
# les déclarer LUI-MÊME à son modèle et garder l'appel et son résultat
# dans son propre historique. Le proxy n'a alors ni boucle ni mémoire à
# tenir : il exécute, c'est tout. Derrière la clé du proxy comme le reste ;
# mêmes garde-fous (tools/).

@router.get("/v1/tools")
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


@router.post("/v1/tools/{name}")
async def tools_run(name: str, request: Request):
    return await run_direct(request, name, "/v1/tools")


async def run_direct(request: Request, name: str, endpoint: str,
                     body=None, known: set | None = None):
    """Exécute un outil hébergé. Corps : ses arguments (objet JSON) —
    ou `body`, déjà lu (/ui/tools).
    Toujours 200 quand l'outil existe : un échec de l'outil est un texte
    «Error: …» que le modèle doit lire, pas une erreur HTTP — `is_error`
    le signale au client, `error` en donne le code (tools.ERRORS, null
    pour un succès). `sources` et `meta` : ce que le résultat dit en plus
    de son texte (pages citées ; URL lue, titre, plage de caractères…),
    pour l'affichage — voir docs/outils.md. `endpoint` : la route
    (/v1/tools, ou /ui/tools pour le chat de la page), pour les
    statistiques. `known` : les sites connus de la conversation
    (provenance) — None, pas de contrôle ; sinon l'outil s'y tient, la
    réponse rend `hosts`, ces sites et ceux que le résultat fait
    connaître."""
    hosted = tools.Hosted()
    if name not in hosted.by_name and tools.GRANTED.get() is not None \
            and any(t.name == name and t.enabled for t in tools.REGISTRY):
        return error_response(
            "openai", 403, "tool_not_permitted",
            f"outil «{name}» non permis à cette clé (ses droits se règlent "
            f"dans Configuration → Clés) ; permis : "
            f"{', '.join(hosted.by_name) or 'aucun'}")
    if name not in hosted.by_name:
        return error_response(
            "openai", 404, "unknown_tool",
            f"outil «{name}» inconnu ou désactivé ([tools.{name}] dans "
            f"config.toml) ; actifs : {', '.join(hosted.by_name) or 'aucun'}")
    args = parse_json(await request.body()) if body is None else body
    if not isinstance(args, dict):
        return error_response("openai", 400, "invalid_request_error",
                              "corps attendu : les arguments de l'outil, "
                              "en objet JSON")
    client = request_owner(request)
    delay = direct_wait(client)
    if delay:
        retry, message = _too_many(delay)
        log.warning("%s/%s : client au-delà de %d appels/min → 429",
                    endpoint, name, DIRECT_PER_MINUTE)
        return error_response("openai", 429, "rate_limit_exceeded_proxy",
                              message, headers={"Retry-After": str(retry)})
    result = await hosted.run(
        name, json.dumps(args, ensure_ascii=False), 0, endpoint=endpoint,
        client=client, hosts=frozenset(known) if known is not None else None)
    extra = {}
    if known is not None:
        provenance.learn(known, result, name)
        extra["hosts"] = sorted(known)
    return {"name": name, "result": result.text,
            "is_error": result.error is not None, "error": result.error,
            "sources": [dataclasses.asdict(s) for s in result.sources],
            "meta": dict(result.meta),
            # Les fichiers produits, rangés au magasin : des LIENS.
            "files": [{"name": s.name, "media_type": s.media_type,
                       "size": len(s.data), "url": s.url}
                      for s in files.keep(result.files, client)], **extra}


@router.api_route(files.PATH + "{token}/{name}", methods=["GET", "HEAD"])
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
# streamable HTTP, sans état). Voir mcp_server.py.

def mcp_disabled() -> JSONResponse:
    return error_response("openai", 404, "unknown_route",
                          "serveur MCP désactivé ([mcp_server].enabled dans config.toml)")


@router.post("/mcp")
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
    calls = sum(1 for m in (body if isinstance(body, list) else [body])
                if isinstance(m, dict) and m.get("method") == "tools/call")
    delay = direct_wait(client, calls)
    if delay:
        retry, message = _too_many(delay)
        log.warning("MCP : client au-delà de %d appels/min → 429",
                    DIRECT_PER_MINUTE)
        first = body[0] if isinstance(body, list) and body else body
        return JSONResponse(
            mcp_server.error(first.get("id") if isinstance(first, dict)
                             else None, mcp_server.INVALID_REQUEST, message),
            status_code=429, headers={"Retry-After": str(retry)})
    out = await mcp_server.handle_body(body, tools.Hosted(), client)
    if out is None:
        return Response(status_code=202)
    bad = isinstance(out, dict) and "error" in out and out.get("id") is None
    return JSONResponse(out, status_code=400 if bad else 200)


@router.api_route("/mcp", methods=["GET", "DELETE"])
async def mcp_other(request: Request):
    """Pas de flux serveur ni de session à fermer : 405, comme le prévoit
    la spécification pour un serveur sans état."""
    if not mcp_server.ENABLED:
        return mcp_disabled()
    return Response(status_code=405, headers={"Allow": "POST"})
