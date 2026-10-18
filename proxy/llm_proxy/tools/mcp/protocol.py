"""
Le PROTOCOLE, côté client : les réglages de [tools.mcp], les versions
négociées, et ce qui se traduit d'un serveur MCP vers un outil hébergé —
noms de fonction, en-têtes tirés du schéma, définition, rendu d'un
résultat.
"""

import base64
import hashlib
import json
import re

from ...core import config
from ..contract import Result, Source, ToolError


# Délai (s) d'un appel, tout compris (session rouverte incluse), et de
# chaque requête.
TIMEOUT = config.num("tools.mcp.timeout", 30)


# Ce que l'exécuteur laisse de plus : le temps de rendre l'erreur de délai
# d'ici, qui nomme l'outil et sa durée.
GRACE = 5


# Période (s) entre deux `tools/list` d'un serveur ; 0 = une découverte
# au démarrage, puis seulement quand le serveur signale un changement.
REFRESH = config.num("tools.mcp.refresh", 300)


# Ce que le démarrage du proxy attend, au plus, de la première découverte.
STARTUP_WAIT = config.num("tools.mcp.startup_wait", 5)


# Outils exposés par serveur : chacun pèse dans le prompt de toute requête.
MAX_TOOLS = config.integer("tools.mcp.max_tools", 64)


DESCRIPTION_CHARS = config.integer("tools.mcp.description_chars", 1024)


SCHEMA_CHARS = config.integer("tools.mcp.schema_chars", 8000)


# Octets lus au plus sur une réponse (une image en base64 pèse vite).
MAX_BYTES = config.integer("tools.mcp.max_bytes", 4_000_000)


# Le type qui déclare les outils MCP dans `tools` d'une requête
# chat/completions : `mcp` (tous), `mcp:<serveur>` (ceux d'un serveur).
KIND = "mcp"


MODERN = "2026-07-28"


LEGACY = ("2025-11-25", "2025-06-18", "2025-03-26")


# Erreurs JSON-RPC qu'un serveur moderne seul sait rendre : HeaderMismatch,
# MissingRequiredClientCapability, UnsupportedProtocolVersion.
HEADER_MISMATCH, MISSING_CAPABILITY, UNSUPPORTED_VERSION = -32020, -32021, -32022


METHOD_NOT_FOUND, INVALID_PARAMS = -32601, -32602


CLIENT = {"name": "llm-proxy", "version": "1.0"}


USER_AGENT = "llm-proxy mcp (+https://github.com/c4software/llm-proxy)"


_META = "io.modelcontextprotocol/"


# `structuredContent` rendu au client dans `meta`, s'il tient là-dedans.
STRUCTURED_CHARS = 24_000


MAX_PAGES = 20            # pages de `tools/list` suivies


MIN_GAP = 10              # s entre deux découvertes d'un même serveur


SCHEMA_DEPTH = 64


# Un nom de fonction, chez tous les backends chat/completions.
NAME_CHARS = 64


_NAME = re.compile(r"[A-Za-z0-9_-]{1,32}")


_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


# En-têtes que le client pose lui-même : la configuration n'y touche pas.
RESERVED = ("accept", "content-type", "content-length", "host",
            "transfer-encoding", "connection")


_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")           # RFC 9110, tchar


_PLAIN = re.compile(r"[\x21-\x7e]([\x20\x09\x21-\x7e]*[\x21-\x7e])?")


class Failure(Exception):
    """Un échange qui n'a pas abouti. `code` : celui du contrat
    (contract.ERRORS) ; `message` : une phrase en anglais, que le modèle
    peut lire ; `rpc` : le code de l'erreur JSON-RPC, s'il y en a une."""

    def __init__(self, code: str, message: str, rpc: int | None = None):
        super().__init__(message)
        self.code, self.message, self.rpc = code, message, rpc


def _clean(text, limit: int, lines: bool = False) -> str:
    """Un texte venu du serveur, avant d'entrer dans le prompt : sans
    caractères de contrôle, borné. `lines` garde les sauts de ligne."""
    text = text if isinstance(text, str) else ""
    keep = "\n\t" if lines else ""
    text = "".join(c if c >= " " and c != "\x7f" or c in keep else " "
                   for c in text)
    text = text.strip() if lines else " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def function_name(prefix: str, remote: str) -> str:
    """Le nom de la fonction présentée au modèle : `<préfixe>_<outil>`.
    Le préfixe (le nom du serveur dans la configuration, sauf `prefix`)
    sépare deux serveurs qui ont chacun un `search` — le `serverInfo` que
    le serveur annonce, lui, n'est pas garanti unique. Un nom MCP admet
    le point et 128 caractères, un nom de fonction ni l'un ni l'autre :
    tout caractère hors [A-Za-z0-9_-] devient «_», et un nom trop long
    est coupé et fini par un condensé du nom entier, pour rester unique."""
    raw = f"{prefix}_{remote}" if prefix else remote
    name = _UNSAFE.sub("_", raw)
    if len(name) > NAME_CHARS:
        digest = hashlib.blake2s(raw.encode("utf-8", "surrogatepass"),
                                 digest_size=4).hexdigest()
        name = f"{name[:NAME_CHARS - 9]}_{digest}"
    return name


def header_value(value) -> str:
    """Une valeur recopiée en en-tête (`Mcp-Name`, `Mcp-Param-…`) : telle
    quelle si c'est de l'ASCII visible, sinon — et si elle ressemble à la
    forme encodée — en `=?base64?…?=` de son UTF-8."""
    text = ("true" if value else "false") if isinstance(value, bool) \
        else str(value)
    if _PLAIN.fullmatch(text) and not (
            text.startswith("=?base64?") and text.endswith("?=")):
        return text
    encoded = base64.b64encode(text.encode("utf-8", "surrogatepass"))
    return f"=?base64?{encoded.decode('ascii')}?="


def param_headers(schema: dict) -> tuple:
    """Les paramètres que le schéma marque `x-mcp-header`, en (chemin de
    propriétés, nom d'en-tête). Lève ValueError pour une annotation que
    la spécification interdit — l'outil entier est alors écarté : nom
    vide ou hors syntaxe d'en-tête, doublon, type non primitif, ou
    propriété qui ne s'atteint pas par une suite de `properties`."""
    found, names = [], set()

    def walk(node, path, static, depth):
        if depth > SCHEMA_DEPTH:
            raise ValueError("schéma trop profond")
        if isinstance(node, list):
            for item in node:
                walk(item, path, False, depth + 1)
            return
        if not isinstance(node, dict):
            return
        if "x-mcp-header" in node:
            name = node["x-mcp-header"]
            if not static or not path:
                raise ValueError("x-mcp-header hors d'une propriété "
                                 "atteignable par `properties`")
            if not isinstance(name, str) or not _TOKEN.fullmatch(name):
                raise ValueError(f"x-mcp-header invalide : {name!r}")
            if node.get("type") not in ("string", "integer", "boolean"):
                raise ValueError(f"x-mcp-header «{name}» sur un type non "
                                 f"primitif")
            if name.lower() in names:
                raise ValueError(f"x-mcp-header «{name}» en double")
            names.add(name.lower())
            found.append((path, name))
        for key, value in node.items():
            if key == "x-mcp-header":
                continue
            # Des tables de sous-schémas : leurs CLÉS sont des noms, pas
            # des mots-clés (une propriété peut s'appeler «x-mcp-header»).
            if key in ("properties", "patternProperties", "$defs",
                       "definitions", "dependentSchemas") \
                    and isinstance(value, dict):
                for prop, sub in value.items():
                    walk(sub, path + (prop,),
                         static and key == "properties", depth + 1)
            else:
                walk(value, path, False, depth + 1)

    walk(schema, (), True, 0)
    return tuple(found)


def definition(tool: dict, server: str, modern: bool) -> tuple[str, dict, tuple]:
    """Une entrée de `tools/list` → (description, schéma, en-têtes de
    paramètres) de la fonction présentée au modèle. Lève ValueError pour
    une définition qu'on ne présente pas."""
    schema = tool.get("inputSchema")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError("inputSchema n'est pas un schéma d'objet")
    # `$schema` nomme un dialecte : rien ici ne valide, et des backends
    # refusent le mot-clé. Les `$ref` ne sont jamais suivis.
    schema = {k: v for k, v in schema.items() if k != "$schema"}
    size = len(json.dumps(schema, ensure_ascii=False))
    if size > SCHEMA_CHARS:
        raise ValueError(f"schéma de {size} caractères (plus de "
                         f"{SCHEMA_CHARS})")
    # Parcouru dans les deux ères, pour la borne de profondeur ; les
    # en-têtes ne partent que vers un serveur moderne.
    params = param_headers(schema)
    description = _clean(tool.get("description") or tool.get("title"),
                         DESCRIPTION_CHARS, lines=True) \
        or f"Tool {_clean(tool.get('name'), 128)} of the MCP server {server}."
    return description, schema, params if modern else ()


def render(result: dict, server: str, remote: str) -> Result:
    """Le résultat de `tools/call` → celui du contrat. Le contrat ne porte
    que du TEXTE : un contenu `text` est rendu tel quel, une ressource
    textuelle sous son URI, et tout le reste (image, audio, ressource
    binaire) par une ligne qui dit ce qui a été omis — le modèle sait
    alors qu'il y avait autre chose. `structuredContent` : rendu en JSON
    s'il n'y a aucun texte (un serveur le double d'ordinaire d'un bloc de
    texte), et donné au client dans `meta`."""
    kind = result.get("resultType", "complete")   # absent = serveur hérité
    if kind == "input_required":
        raise ToolError("unsupported", (
            "this tool asks for input from the user (elicitation or "
            "sampling), which this proxy cannot provide."))
    if kind != "complete":
        raise ToolError("unavailable", "the MCP server returned a result "
                                       "of an unknown type.")
    parts, omitted, sources, texts = [], [], [], 0
    content = result.get("content")
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        what = block.get("type")
        if what == "text":
            if isinstance(block.get("text"), str) and block["text"]:
                parts.append(block["text"])
                texts += 1
            continue
        resource = block.get("resource") if what == "resource" else block
        resource = resource if isinstance(resource, dict) else {}
        uri = _clean(resource.get("uri"), 300)
        mime = _clean(resource.get("mimeType"), 80)
        if what == "resource" and isinstance(resource.get("text"), str):
            parts.append(f"[resource {uri}]\n{resource['text']}")
            texts += 1
        elif what == "resource_link":
            name = _clean(block.get("name"), 120)
            about = _clean(block.get("description"), 300)
            parts.append(f"[resource link: {name or uri} — {uri}"
                         + (f" ({mime})" if mime else "") + "]"
                         + (f" {about}" if about else ""))
            if uri.startswith(("http://", "https://")):
                sources.append(Source(uri, name, snippet=about))
        else:
            label = _clean(what, 40) or "unknown"
            data = resource.get("data", resource.get("blob"))
            size = len(data) * 3 // 4 if isinstance(data, str) else 0
            detail = ", ".join(x for x in (
                mime, uri, f"{size} bytes" if size else "") if x)
            parts.append(f"[{label} content omitted"
                         + (f": {detail}" if detail else "")
                         + " — this proxy returns text only]")
            omitted.append(mime or label)
    structured = result.get("structuredContent")
    dumped = "" if structured is None \
        else json.dumps(structured, ensure_ascii=False)
    if dumped and not texts:
        parts.insert(0, dumped)
    meta = {"server": server, "tool": remote}
    if omitted:
        meta["omitted"] = omitted
    if dumped and len(dumped) <= STRUCTURED_CHARS:
        meta["structured"] = structured
    text = "\n\n".join(parts).strip()
    if result.get("isError") is True:
        # Une erreur d'EXÉCUTION de l'outil : il a tourné, et dit lui-même
        # avoir échoué (validation, API tierce, règle métier) — `failed`.
        # Son texte est écrit pour que le modèle corrige et réessaie.
        return Result("Error: " + (text or "the tool reported an error "
                                           "without a message."),
                      "failed", meta=meta)
    return Result(text or "The tool returned no content.",
                  sources=tuple(sources), meta=meta)
