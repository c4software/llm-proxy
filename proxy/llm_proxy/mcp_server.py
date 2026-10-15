"""
Le proxy en SERVEUR MCP : ses outils hébergés actifs (web_search,
web_fetch, web_grep, ocr, code_execution, memory… et ceux des serveurs MCP
de [tools.mcp], qu'il relaie) offerts à tout client qui parle le Model
Context Protocol — Claude Code, Codex, et tout agent qui sait joindre un
serveur MCP distant — par une seule route, `POST /mcp`, au transport
« streamable HTTP ». C'est l'INVERSE de tools/mcp.py, où le proxy est
CLIENT de serveurs MCP.

Pourquoi : chaque client a sa façon de recevoir un outil du proxy. Codex
et les clients chat/completions déclarent `web_search` (ou le reçoivent
d'office, [chat].always) et le proxy fait la boucle ; Claude Code ne passe
par le proxy que pour la sous-requête de son WebSearch, et lit les pages
sur son poste — sa boucle principale n'a aucun outil du proxy. MCP, tous
le parlent : un outil ajouté au registre y paraît, sans rien de plus.

Ce que ce module sert, et seulement cela :
  initialize                 version négociée, capacité `tools`
  notifications/initialized  et toute notification : acceptée, sans réponse
  ping                       {}
  tools/list                 les outils hébergés actifs (ceux de
                             GET /v1/tools), en `inputSchema`
  tools/call                 Hosted.run : le texte du Result en contenu
                             `text`, ses fichiers en `resource_link`,
                             `isError` pour un échec
Le reste → erreur JSON-RPC « méthode inconnue ».

SANS ÉTAT : pas de `Mcp-Session-Id`, pas de flux serveur (GET /mcp → 405,
ce que la spécification prévoit), toujours une réponse JSON — le client
annonce accepter JSON et SSE, et un appel d'outil du proxy ne pousse rien
en cours de route. Chaque POST se suffit : un redémarrage du proxy ne
casse aucune session.

L'authentification est celle de tout le proxy (middleware de app.py) :
le client pose `Authorization: Bearer <clé du proxy>` dans la
configuration de son serveur MCP. La clé dit aussi QUI appelle
(`call.client`, tools.owner) — ce qui cloisonne la mémoire persistante,
le bac de code_execution, les fichiers rendus.

L'appel ne connaît ni réponse en cours ni modèle : comme /v1/tools, il
n'a pas de limite d'appels par réponse (`used` = 0), et sa ligne de
statistiques porte la route `/mcp`, sans modèle.
"""

import json

from . import config, files

ENABLED = config.flag("mcp_server.enabled", False)
# Origines (en-tête `Origin`, celui d'une page web) admises en plus de
# celle du proxy lui-même. La spécification demande de les contrôler : un
# serveur MCP joignable d'un navigateur serait sinon appelable depuis
# n'importe quelle page qu'ouvre l'utilisateur (rebinding DNS). Les
# clients en ligne de commande n'envoient pas d'Origin.
ALLOWED_ORIGINS = config.strings("mcp_server.allowed_origins")

# Versions du protocole que ce serveur parle, la plus récente d'abord. Le
# client propose la sienne ; connue, elle est reprise, sinon la plus
# récente d'ici (au client de raccrocher s'il ne la parle pas).
VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "llm-proxy", "title": "llm-proxy",
               "version": "1.0.0"}
INSTRUCTIONS = (
    "Tools executed by the llm-proxy gateway on behalf of the model: web "
    "search through the proxy's own search engine, reading web pages and "
    "PDF documents, searching inside a page, and whatever else this proxy "
    "has enabled (OCR, code execution, image generation, a persistent "
    "memory private to the caller...). Each tool's description says when "
    "to use it.")

# Codes d'erreur JSON-RPC.
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = \
    -32700, -32600, -32601, -32602


def error(id_, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_,
            "error": {"code": code, "message": message}}


def result(id_, value: dict) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": value}


def tool_list(hosted) -> list[dict]:
    """tools/list : chaque outil actif à la forme MCP, par la même fabrique
    que les surfaces (Tool.spec) — son prompt tel qu'en vigueur, sachant
    quels autres outils sont offerts avec lui."""
    present = frozenset(hosted.by_name)
    out = []
    for tool in hosted.tools:
        fn = tool.spec(present)["function"]
        out.append({"name": tool.name, "description": fn.get("description", ""),
                    "inputSchema": fn.get("parameters")
                    or {"type": "object", "properties": {}}})
    return out


async def call(hosted, params: dict, owner: str = "") -> dict:
    """tools/call → le résultat MCP. Un outil inconnu ou désactivé est une
    erreur d'OUTIL (`isError`), pas de protocole : le modèle la lit et
    s'adapte. Les fichiers produits (code_execution, image_generation)
    sont rangés au magasin (files.py) et rendus en `resource_link` — leur
    texte, lui, les nomme déjà. `owner` : le client (tools.owner)."""
    name = params.get("name")
    args = params.get("arguments")
    if args is None:
        args = {}
    if not isinstance(name, str) or name not in hosted.by_name:
        text = (f"Error: unknown tool {name}; available: "
                f"{', '.join(hosted.by_name) or 'none'}.")
        return {"content": [{"type": "text", "text": text}], "isError": True}
    if not isinstance(args, dict):
        return {"content": [{"type": "text", "text":
                             "Error: the tool arguments are not a JSON object."}],
                "isError": True}
    result = await hosted.run(name, json.dumps(args, ensure_ascii=False), 0,
                              endpoint="/mcp", client=owner)
    content = [{"type": "text", "text": result.text}]
    for stored in files.keep(result.files, owner):
        content.append({"type": "resource_link", "uri": stored.url,
                        "name": stored.name, "mimeType": stored.media_type})
    return {"content": content, "isError": result.error is not None}


async def handle(message, hosted, owner: str = "") -> dict | None:
    """UN message JSON-RPC → sa réponse, ou None (notification, ou
    réponse du client à une requête que ce serveur n'envoie jamais)."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return error(None, INVALID_REQUEST, "not a JSON-RPC 2.0 message")
    method = message.get("method")
    if "id" not in message:
        # Notification (initialized, cancelled…), ou réponse : rien à rendre.
        return None
    id_ = message["id"]
    if not isinstance(method, str):
        return error(id_, INVALID_REQUEST, "missing method")
    params = message.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return error(id_, INVALID_PARAMS, "params must be an object")
    if method == "initialize":
        asked = params.get("protocolVersion")
        return result(id_, {
            "protocolVersion": asked if asked in VERSIONS else VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return result(id_, {})
    if method == "tools/list":
        # Une seule page : le curseur éventuel du client est ignoré.
        return result(id_, {"tools": tool_list(hosted)})
    if method == "tools/call":
        return result(id_, await call(hosted, params, owner))
    return error(id_, METHOD_NOT_FOUND, f"method not found: {method}")


async def handle_body(body, hosted, owner: str = ""):
    """Le corps d'un POST /mcp (déjà décodé) → ce qu'il faut rendre : une
    réponse, une liste de réponses (lot, versions antérieures à
    2025-06-18), ou None si le corps n'appelle aucune réponse (202)."""
    if isinstance(body, list):
        if not body:
            return error(None, INVALID_REQUEST, "empty batch")
        out = [r for r in [await handle(m, hosted, owner) for m in body]
               if r is not None]
        return out or None
    return await handle(body, hosted, owner)


def origin_allowed(origin: str, host: str) -> bool:
    """L'en-tête Origin d'une requête : absent (client en ligne de
    commande), celui du proxy lui-même, ou l'une des origines admises."""
    if not origin:
        return True
    if origin in ALLOWED_ORIGINS:
        return True
    bare = origin.split("://", 1)[-1].rstrip("/")
    return bool(host) and bare.lower() == host.lower()
