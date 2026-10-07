"""
Client MCP (Model Context Protocol) : le proxy se connecte aux serveurs
MCP listés dans la configuration, découvre leurs outils, et chacun
devient un outil hébergé (contract.Tool) — présentable sur
/v1/chat/completions (déclaré, ou d'office par [chat].always), exécutable
par `POST /v1/tools/<nom>`. Sans liaison de protocole : les surfaces
Responses et Anthropic ne les présentent pas.

Ce qui est permis, et rien d'autre :
  * HTTP seulement (transport « Streamable HTTP »). Pas de stdio : le
    proxy ne lance aucun processus ;
  * une liste FERMÉE de serveurs, [tools.mcp.<serveur>] de config.toml.
    Jamais d'URL venue d'un client ou du modèle. L'URL est une adresse de
    CONFIGURATION, en général privée : le garde-fou des adresses publiques
    (net.py) ne s'y applique pas, comme pour l'instance SearXNG ;
  * des en-têtes STATIQUES pour l'authentification (secrets par ${VAR}).
    Pas d'OAuth : un serveur qui l'exige répond 401, et le journal le dit.

Le protocole est écrit ici avec httpx seul, sans SDK. Deux ÈRES, que la
spécification du 28/07/2026 distingue (basic/versioning) :
  * « moderne » (2026-07-28) : sans état. Ni `initialize` ni session :
    chaque requête porte sa version et les capacités du client dans
    `params._meta`, recopiées en en-têtes (`MCP-Protocol-Version`,
    `Mcp-Method`, `Mcp-Name`, et `Mcp-Param-<nom>` pour un paramètre que
    le schéma de l'outil marque `x-mcp-header`) ;
  * « héritée » (2025-03-26 → 2025-11-25) : `initialize`, puis
    `notifications/initialized`, puis les requêtes, sous `Mcp-Session-Id`
    si le serveur en a donné un. Session expirée = 404 : le client DOIT
    en rouvrir une — fait UNE fois, puis la requête est rejouée.
L'ère est celle du SERVEUR : sondée une fois par `server/discover`
(requête moderne), gardée, re-sondée après un échec de découverte. Un
400 qui ne porte pas une erreur JSON-RPC moderne (-32020 à -32022), ou
un résultat sans `resultType`, désigne un serveur hérité.
Dans les deux ères : un POST par message ; la réponse est un objet JSON
ou un flux SSE (`text/event-stream`) qui se termine par elle — les deux
sont lus, les notifications du flux sont ignorées, sauf
`notifications/tools/list_changed` qui avance la prochaine découverte.

DÉCOUVERTE. Le registre des outils se remplit à l'import ; celle-ci est
asynchrone, peut échouer, et la liste change. D'où `start()` au
démarrage de l'application et `stop()` à son arrêt : une tâche par
serveur redemande `tools/list` à période fixe (`refresh`, jamais plus
souvent que le `ttlMs` annoncé, avec aléa, et un recul quand le serveur
ne répond pas). Un serveur éteint au démarrage ne bloque rien : ses
outils apparaissent quand il répond. Le registre ne sait pas retirer :
un outil enregistré le reste, et c'est son `enabled` qui suit la liste
du serveur — il garde ainsi sa place parmi les fonctions présentées au
modèle, donc le préfixe d'un backend à cache.

Ce qui entre dans le prompt du modèle vient d'un TIERS : nom, description
et schéma de chaque outil, puis ses résultats. Tout est borné ici ; rien
n'en est interprété. Le choix des serveurs, et de leurs outils (`tools`,
`exclude`), est le seul garde-fou : un outil exposé l'est à TOUS les
clients du proxy, avec le compte que portent les en-têtes configurés.
"""

import asyncio
import base64
import copy
import fnmatch
import hashlib
import itertools
import json
import random
import re
import time
from urllib.parse import urlsplit

import httpx

from .. import config
from ..settings import log
from .contract import Call, Result, Source, Tool, ToolError

# Délai (s) d'un appel, tout compris (session rouverte incluse), et de
# chaque requête. C'est le délai de l'outil (McpTool.timeout), à la place
# de [tools].run_timeout.
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


class McpTool(Tool):
    """Un outil d'un serveur MCP, en outil hébergé. L'objet vit autant
    que le proxy : sa définition suit la liste du serveur (`define`), et
    `live` dit s'il y figure encore."""

    def __init__(self, server: "Server", remote: str, name: str):
        self.server, self.remote, self.name = server, remote, name
        self.live = False
        self.description, self.schema, self.params = "", {"type": "object"}, ()

    @property
    def enabled(self) -> bool:
        return self.live and self.server.enabled

    @property
    def timeout(self) -> float:
        """Le délai de l'exécuteur pour cet outil (contrat : Tool.timeout),
        à la place de [tools].run_timeout : celui du serveur, tenu dans
        run(), plus de quoi rendre son erreur."""
        return self.server.timeout + GRACE

    @property
    def kinds(self) -> tuple[str, ...]:
        """Ce qui le déclare dans `tools` d'une requête chat/completions :
        son nom, `mcp:<serveur>` (tous les outils du serveur) ou `mcp`
        (tous ceux de tous les serveurs)."""
        return (self.name, f"{KIND}:{self.server.name}", KIND)

    def define(self, description: str, schema: dict, params: tuple) -> None:
        self.description, self.schema, self.params = description, schema, params

    def spec(self, present) -> dict:
        return {"type": "function", "function": {
            "name": self.name,
            "description": self.description,
            "parameters": copy.deepcopy(self.schema),
        }}

    def _headers(self, args: dict) -> dict:
        """Les `Mcp-Param-<nom>` de cet appel : la valeur lue au chemin
        exact de la propriété annotée ; absente ou nulle, pas d'en-tête."""
        headers = {}
        for path, name in self.params:
            value = args
            for step in path:
                value = value.get(step) if isinstance(value, dict) else None
            if value is None:
                continue
            if not isinstance(value, (str, int)) or (
                    isinstance(value, int) and abs(value) >= 2 ** 53):
                raise ToolError("invalid_input", (
                    f"`{'.'.join(path)}` must be a string, an integer or "
                    f"a boolean."))
            headers[f"Mcp-Param-{name}"] = header_value(value)
        return headers

    async def run(self, args: dict, call: Call) -> Result:
        """Les arguments partent tels que le modèle les a écrits : c'est
        le serveur qui les valide (aucun validateur JSON Schema ici), et
        son refus revient en erreur que le modèle lit."""
        server = self.server
        try:
            async with asyncio.timeout(server.timeout):
                result = await server.request(
                    "tools/call", {"name": self.remote, "arguments": args},
                    self._headers(args))
        except TimeoutError:
            raise ToolError("timeout", f"{self.name} timed out after "
                                       f"{int(server.timeout)} s.")
        except Failure as exc:
            # Outil inconnu, paramètres ou en-têtes refusés : la liste a
            # peut-être changé chez le serveur — la redemander sans
            # attendre la période.
            if exc.rpc in (METHOD_NOT_FOUND, INVALID_PARAMS, HEADER_MISMATCH):
                server.changed()
            raise ToolError(exc.code, exc.message)
        return render(result, server.name, self.remote)


def _setting(table: dict, key: str, default, cast, label: str, name: str):
    value = table.get(key, default)
    try:
        return cast(value)
    except (TypeError, ValueError):
        raise SystemExit(f"{config.CONFIG_PATH} : tools.mcp.{name}.{key} "
                         f"doit être {label} (reçu {value!r})")


def _names(table: dict, key: str, name: str) -> list[str]:
    value = table.get(key, [])
    if isinstance(value, str):
        value = [p.strip() for p in value.split(",")]
    if not isinstance(value, list):
        raise SystemExit(f"{config.CONFIG_PATH} : tools.mcp.{name}.{key} "
                         f"doit être une liste de chaînes")
    return [str(v) for v in value if str(v).strip()]


class Server:
    """Un serveur MCP de la configuration : ses réglages, la connexion
    (ère, version, session), et les outils qu'il a apportés au registre."""

    def __init__(self, name: str, table: dict, transport=None):
        where = f"{config.CONFIG_PATH} : [tools.mcp.{name}]"
        if not _NAME.fullmatch(name):
            raise SystemExit(f"{where} : nom de serveur invalide (lettres, "
                             f"chiffres, «_», «-» ; 32 caractères au plus)")
        self.name = name
        self.enabled = bool(table.get("enabled", True))
        self.url = str(table.get("url") or "")
        try:
            parts = urlsplit(self.url)
            valid = parts.scheme in ("http", "https") and bool(parts.hostname)
        except ValueError:
            parts, valid = None, False
        if not valid:
            raise SystemExit(f"{where} : `url` doit être une URL http(s) "
                             f"(reçu {self.url!r})")
        # Pour le journal : sans la requête, qui peut porter une clé.
        self.where = f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}" \
                     f"{parts.path}"
        headers = table.get("headers", {})
        if not isinstance(headers, dict):
            raise SystemExit(f"{where} : `headers` doit être une table")
        # Un secret ${VAR} dont la variable manque rend «» ou «Bearer » :
        # l'en-tête n'est pas envoyé, et le démarrage le signale.
        self.headers, self.missing = {"User-Agent": USER_AGENT}, []
        for key, value in headers.items():
            if key.lower() in RESERVED or key.lower().startswith("mcp-"):
                raise SystemExit(f"{where} : l'en-tête «{key}» est posé par "
                                 f"le proxy, il ne se configure pas")
            value = str(value)
            if not value.strip() or value != value.rstrip():
                self.missing.append(key)
            else:
                self.headers[key] = value
        self.prefix = str(table.get("prefix", name))
        if self.prefix and not _NAME.fullmatch(self.prefix):
            raise SystemExit(f"{where} : `prefix` invalide (lettres, "
                             f"chiffres, «_», «-» ; ou «» pour aucun)")
        # Motifs sur le nom de l'outil CHEZ LE SERVEUR (`get_*`) : `tools`
        # non vide = seuls ceux-là ; `exclude` = jamais ceux-là.
        self.allow = _names(table, "tools", name)
        self.exclude = _names(table, "exclude", name)
        self.timeout = _setting(table, "timeout", TIMEOUT, float,
                                "un nombre", name)
        self.refresh_every = _setting(table, "refresh", REFRESH, float,
                                      "un nombre", name)
        self.max_tools = _setting(table, "max_tools", MAX_TOOLS, int,
                                  "un entier", name)
        self.verify_ssl = bool(table.get("verify_ssl", True))
        self.transport = transport

        self.client: httpx.AsyncClient | None = None
        self.era: str | None = None       # None : pas encore sondée
        self.version = MODERN
        self.session = ""
        self.tools: dict[str, McpTool] = {}     # par nom chez le serveur
        self.up: bool | None = None       # issue de la dernière découverte
        self.error = ""
        self.ttl = 0.0                    # fraîcheur annoncée (`ttlMs`), en s
        self._ids = itertools.count(1)
        self._said: set = set()
        self._lock = self._stale = self.first = None

    # ── connexion ───────────────────────────────────────────────────────

    def open(self, transport=None) -> None:
        """Ouvre le client HTTP. Dans la boucle asyncio qui servira : les
        verrous lui sont liés."""
        self._lock, self._stale = asyncio.Lock(), asyncio.Event()
        self.first = asyncio.Event()
        self.era, self.session = None, ""
        # trust_env=False : une adresse du réseau du proxy, un HTTP_PROXY
        # d'environnement n'a pas à s'en mêler. Pas de redirection suivie :
        # les en-têtes d'authentification partiraient vers un autre hôte.
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout,
                                  connect=min(self.timeout, 10)),
            transport=transport or self.transport, follow_redirects=False,
            trust_env=False, verify=self.verify_ssl)

    async def close(self) -> None:
        """Ferme la session (DELETE, ère héritée : le serveur peut la
        refuser par 405) puis le client."""
        if self.client is None:
            return
        if self.era == "legacy" and self.session:
            try:
                await asyncio.wait_for(self.client.delete(
                    self.url, headers={**self.headers, **self._head("", {})}), 2)
            except (httpx.HTTPError, asyncio.TimeoutError):
                pass
        await self.client.aclose()
        self.client, self.era, self.session = None, None, ""
        for tool in self.tools.values():
            tool.live = False

    def changed(self) -> None:
        """La liste a peut-être changé : avance la prochaine découverte."""
        if self._stale is not None:
            self._stale.set()

    # ── un message ──────────────────────────────────────────────────────

    def _frame(self, method: str, params: dict | None = None) -> dict:
        """La requête JSON-RPC. Ère moderne : la version, le client et
        ses capacités (aucune) voyagent dans chaque requête."""
        params = dict(params or {})
        if self.era != "legacy":
            params["_meta"] = {
                _META + "protocolVersion": self.version,
                _META + "clientInfo": CLIENT,
                _META + "clientCapabilities": {},
            }
        message = {"jsonrpc": "2.0", "id": next(self._ids), "method": method}
        if params:
            message["params"] = params
        return message

    def _head(self, method: str, params: dict, extra=None) -> dict:
        head = {"MCP-Protocol-Version": self.version}
        if self.era == "legacy":
            if self.session:
                head["Mcp-Session-Id"] = self.session
            return head
        head["Mcp-Method"] = method
        if isinstance(params.get("name"), str):
            head["Mcp-Name"] = header_value(params["name"])
        head.update(extra or {})
        return head

    async def _post(self, message: dict, head: dict):
        """Un POST = un message. Rend (statut HTTP, réponse JSON-RPC ou
        None, en-têtes). La réponse est celle qui porte l'`id` de la
        requête : le corps JSON, ou l'événement du flux SSE qui le clôt."""
        want = message.get("id")
        request = self.client.build_request(
            "POST", self.url, json=message, headers={
                **self.headers,
                "Accept": "application/json, text/event-stream", **head})
        try:
            r = await self.client.send(request, stream=True)
            try:
                kind = r.headers.get("content-type", "") \
                    .split(";")[0].strip().lower()
                if kind == "text/event-stream":
                    reply = await self._stream(r, want)
                else:
                    reply = self._json(await self._body(r))
            finally:
                # Fermer le flux, c'est aussi ANNULER la requête (ère
                # moderne) : un appel abandonné au délai ne court plus.
                await r.aclose()
        except httpx.TimeoutException:
            raise Failure("timeout", f"the MCP server {self.name} did not "
                                     f"answer in {int(self.timeout)} s.")
        except httpx.HTTPError as exc:
            raise Failure("unavailable", f"the MCP server {self.name} is "
                          f"unreachable ({type(exc).__name__}).")
        return r.status_code, reply, r.headers

    def _too_large(self) -> Failure:
        return Failure("unavailable", f"the MCP server {self.name} sent a "
                       f"response of more than {MAX_BYTES} bytes.")

    async def _body(self, r) -> bytes:
        body = bytearray()
        async for chunk in r.aiter_bytes():
            body += chunk
            if len(body) > MAX_BYTES:
                raise self._too_large()
        return bytes(body)

    @staticmethod
    def _json(raw):
        try:
            message = json.loads(raw)
        except ValueError:
            return None
        return message if isinstance(message, dict) else None

    async def _stream(self, r, want):
        """Le flux SSE d'une requête : des notifications, puis la réponse.
        Lignes `data:` jointes jusqu'à la ligne vide ; commentaires («:»,
        le maintien de connexion) et autres champs ignorés. Un flux fermé
        avant la réponse n'est PAS repris (`Last-Event-ID`, ère héritée) :
        c'est un échec."""
        async def lines():
            async for line in r.aiter_lines():
                yield line
            yield ""                    # un flux fini sans ligne vide

        size, data = 0, []
        async for line in lines():
            size += len(line) + 1
            if size > MAX_BYTES:
                raise self._too_large()
            if line:
                field, _, value = line.partition(":")
                if field == "data":
                    data.append(value.removeprefix(" "))
                continue
            message, data = self._json("\n".join(data)) if data else None, []
            if message is None:
                continue
            if message.get("method") == "notifications/tools/list_changed":
                self.changed()
            if want is not None and message.get("id") == want and (
                    "result" in message or "error" in message):
                return message
        return None

    def _refused(self, status: int) -> Failure:
        """Un statut HTTP sans réponse JSON-RPC lisible."""
        if status in (401, 403):
            # Le détail (en-têtes, OAuth) regarde l'administrateur : il
            # est au journal, pas chez le modèle.
            return Failure("unavailable", f"the MCP server {self.name} "
                           f"refused the credentials of this proxy (HTTP "
                           f"{status}).")
        if status == 429:
            return Failure("too_many_requests", f"the MCP server "
                           f"{self.name} asks to slow down (HTTP 429).")
        if 200 <= status < 300:
            return Failure("unavailable", f"the MCP server {self.name} "
                           f"sent an unreadable response.")
        return Failure("unavailable", f"the MCP server {self.name} "
                                      f"returned HTTP {status}.")

    def _result(self, status: int, reply) -> dict:
        """Le `result` d'une réponse, ou l'échec qu'elle dit. Une erreur
        JSON-RPC est une erreur de PROTOCOLE (outil inconnu, requête mal
        formée, panne du serveur) — pas celle d'un outil, qui arrive dans
        un résultat (`isError`)."""
        if reply is not None and isinstance(reply.get("result"), dict):
            return reply["result"]
        error = reply.get("error") if reply is not None else None
        if not isinstance(error, dict):
            raise self._refused(status)
        code = error.get("code")
        if code == UNSUPPORTED_VERSION:
            self.era = None             # le serveur a changé : re-sonder
        raise Failure(
            "invalid_input" if code == INVALID_PARAMS else "unavailable",
            f"the MCP server {self.name} answered an error ({code}): "
            f"{_clean(error.get('message'), 300) or 'no message'}",
            code if isinstance(code, int) else None)

    # ── ère, session ────────────────────────────────────────────────────

    async def _connect(self, expired: str | None = None) -> None:
        """Sonde l'ère du serveur au premier échange ; `expired` : la
        session qu'un 404 vient de dire close — rouverte, sauf si un
        appel concurrent l'a déjà fait."""
        async with self._lock:
            if expired is not None:
                if self.session == expired:
                    self.session = ""
                    await self._initialize()
            elif self.era is None:
                await self._detect()

    async def _detect(self) -> None:
        """Une requête moderne d'abord (`server/discover`, que tout
        serveur moderne implémente). Il y répond : moderne. Il rend une
        erreur que seul un serveur moderne connaît : moderne aussi, la
        requête est à corriger. Tout autre refus : serveur hérité, on
        ouvre par `initialize`."""
        self.era, self.version, self.session = None, MODERN, ""
        status, reply, _ = await self._post(
            self._frame("server/discover"), self._head("server/discover", {}))
        result = reply.get("result") if reply else None
        error = reply.get("error") if reply else None
        code = error.get("code") if isinstance(error, dict) else None
        offered = None
        if isinstance(result, dict) and "resultType" in result:
            offered = result.get("supportedVersions")
            if not isinstance(offered, list) or MODERN in offered:
                self.era = "modern"
                return
        elif code == UNSUPPORTED_VERSION:
            data = error.get("data")
            offered = data.get("supported") if isinstance(data, dict) else None
        elif code in (HEADER_MISMATCH, MISSING_CAPABILITY):
            self._result(status, reply)
        elif status in (401, 403, 429) or status >= 500:
            raise self._refused(status)
        if isinstance(offered, list) and not set(offered) & set(LEGACY):
            raise Failure("unavailable", (
                f"the MCP server {self.name} speaks none of the protocol "
                f"versions of this proxy (it offers "
                f"{_clean(', '.join(map(str, offered)), 120)})."))
        await self._initialize()

    async def _initialize(self) -> None:
        """Ère héritée : `initialize` (sans session ni en-tête de
        version), la version retenue par le serveur, sa session s'il en
        donne une, puis `notifications/initialized`."""
        self.session = ""
        message = {"jsonrpc": "2.0", "id": next(self._ids),
                   "method": "initialize", "params": {
                       "protocolVersion": LEGACY[0], "capabilities": {},
                       "clientInfo": CLIENT}}
        status, reply, headers = await self._post(message, {})
        result = self._result(status, reply)
        version = result.get("protocolVersion")
        if version not in LEGACY:
            raise Failure("unavailable", (
                f"the MCP server {self.name} speaks a protocol version "
                f"this proxy does not ({_clean(str(version), 40)})."))
        self.era, self.version = "legacy", version
        self.session = headers.get("mcp-session-id", "")
        status, _, _ = await self._post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            self._head("", {}))
        if status >= 400:
            raise self._refused(status)

    async def request(self, method: str, params: dict | None = None,
                      extra: dict | None = None) -> dict:
        """Une requête, et son `result`. Lève Failure. Session expirée
        (404 sous un `Mcp-Session-Id`) : une nouvelle est ouverte, et la
        requête rejouée UNE fois — un 404 ne l'a pas exécutée."""
        await self._connect()
        for retry in (True, False):
            session = self.session
            status, reply, _ = await self._post(
                self._frame(method, params),
                self._head(method, params or {}, extra))
            if retry and status == 404 and session and self.era == "legacy":
                log.info("MCP %s : session expirée, rouverte", self.name)
                await self._connect(expired=session)
                continue
            return self._result(status, reply)

    # ── découverte ──────────────────────────────────────────────────────

    async def list_tools(self) -> list[dict]:
        """`tools/list`, toutes pages suivies par leur curseur (opaque).
        La fraîcheur retenue est la plus courte des pages."""
        found, cursor, seen, ttl = [], None, set(), None
        for _ in range(MAX_PAGES):
            result = await self.request(
                "tools/list", {"cursor": cursor} if cursor else None)
            page = result.get("tools")
            if not isinstance(page, list):
                raise Failure("unavailable", f"the MCP server {self.name} "
                                             f"sent an unreadable tool list.")
            found += [t for t in page if isinstance(t, dict)]
            fresh = result.get("ttlMs")
            fresh = fresh / 1000 if isinstance(fresh, (int, float)) \
                and not isinstance(fresh, bool) and fresh > 0 else 0.0
            ttl = fresh if ttl is None else min(ttl, fresh)
            cursor = result.get("nextCursor")
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                break
            seen.add(cursor)
        else:
            self._say("pages", "MCP %s : liste d'outils coupée à %d pages",
                      self.name, MAX_PAGES)
        self.ttl = ttl or 0.0
        return found

    def _say(self, key, message: str, *args) -> None:
        """Un avertissement, UNE fois : la découverte repasse à chaque
        période sur les mêmes outils écartés."""
        if key not in self._said:
            self._said.add(key)
            log.warning(message, *args)

    def _wanted(self, remote: str) -> bool:
        match = fnmatch.fnmatchcase
        return (not self.allow or any(match(remote, p) for p in self.allow)) \
            and not any(match(remote, p) for p in self.exclude)

    def publish(self, found: list[dict]) -> None:
        """La liste du serveur → le registre. Un outil nouveau est
        enregistré (à la suite : l'ordre des fonctions déjà présentées ne
        bouge pas), un outil connu reçoit sa définition du jour, un outil
        disparu est désactivé — et retrouve sa place s'il revient."""
        from .. import tools as registry    # à l'usage : pas de cycle

        kept = {}
        for entry in found:
            remote = entry.get("name")
            if not isinstance(remote, str) or not remote or remote in kept \
                    or not self._wanted(remote):
                continue
            if len(kept) >= self.max_tools:
                self._say("max", "MCP %s : plus de %d outils, les suivants "
                          "sont écartés (max_tools, ou `tools` pour choisir)",
                          self.name, self.max_tools)
                break
            try:
                described = definition(entry, self.name, self.era == "modern")
            except ValueError as exc:
                self._say(("def", remote), "MCP %s : outil %r écarté — %s",
                          self.name, remote[:80], exc)
                continue
            tool = self.tools.get(remote)
            if tool is None:
                tool = McpTool(self, remote, function_name(self.prefix, remote))
                try:
                    registry.register(tool)
                except ValueError:
                    self._say(("name", remote), "MCP %s : outil %r écarté — "
                              "le nom de fonction %s est déjà pris",
                              self.name, remote[:80], tool.name)
                    continue
                self.tools[remote] = tool
            tool.define(*described)
            kept[remote] = tool
        for remote, tool in self.tools.items():
            if tool.live != (remote in kept):
                log.info("MCP %s : outil %s %s", self.name, tool.name,
                         "disponible" if remote in kept else "retiré par le "
                         "serveur")
            tool.live = remote in kept

    async def refresh(self) -> bool:
        """Une découverte. En échec, les outils déjà connus RESTENT
        présentés (une liste périmée vaut mieux qu'un prompt qui change à
        chaque coupure) : leur appel rendra l'erreur du moment."""
        try:
            async with asyncio.timeout(self.timeout):
                found = await self.list_tools()
        except (Failure, TimeoutError) as exc:
            error = getattr(exc, "message", "") or "no answer in time."
            if self.up is not False or error != self.error:
                log.warning("MCP %s (%s) injoignable — %s", self.name,
                            self.where, error)
            # L'ère et la session sont à re-sonder : le serveur a pu être
            # relancé, ou remplacé par une autre version.
            self.up, self.error, self.era, self.session = False, error, None, ""
            return False
        self.publish(found)
        if self.up is not True:
            log.info("MCP %s (%s) : protocole %s (%s), %d outil(s)%s",
                     self.name, self.where, self.version,
                     "sans état" if self.era == "modern" else "à session"
                     if self.session else "sans session",
                     len(self.live()), " — " + ", ".join(
                         t.name for t in self.live()) if self.live() else "")
        self.up, self.error = True, ""
        return True

    def live(self) -> list[McpTool]:
        return [t for t in self.tools.values() if t.live]

    def pause(self, failures: int) -> float | None:
        """Secondes avant la prochaine découverte ; None = pas de période
        (attendre un signal). Après un échec : 10 s, 20 s, 40 s… jusqu'à
        la période. Avec un aléa, pour que les proxys d'un même parc ne
        frappent pas le serveur ensemble."""
        if failures:
            pause = min(5 * 2 ** min(failures, 10),
                        max(self.refresh_every, 60))
        elif self.refresh_every <= 0:
            return None
        else:
            pause = max(self.refresh_every, self.ttl)
        return pause * random.uniform(0.9, 1.1)

    async def watch(self) -> None:
        """La tâche du serveur : découvrir, attendre la période ou un
        signal de changement, recommencer."""
        failures = 0
        while True:
            started = time.monotonic()
            try:
                ok = await self.refresh()
            except Exception:           # la tâche ne meurt jamais
                log.exception("MCP %s : découverte en échec", self.name)
                ok = False
            self.first.set()
            failures = 0 if ok else failures + 1
            try:
                await asyncio.wait_for(self._stale.wait(), self.pause(failures))
            except asyncio.TimeoutError:
                pass
            self._stale.clear()
            # Un signal répété (un modèle qui insiste sur un outil refusé)
            # ne fait pas une découverte par appel.
            await asyncio.sleep(max(0.0, started + MIN_GAP - time.monotonic()))


def load(table) -> list[Server]:
    """[tools.mcp] → les serveurs : chaque sous-table en est un, sous son
    nom ; les valeurs simples de la table sont les réglages communs."""
    if not isinstance(table, dict):
        return []
    return [Server(name, t) for name, t in table.items() if isinstance(t, dict)]


SERVERS: list[Server] = load(config.get("tools.mcp", {}))
_TASKS: list[asyncio.Task] = []
_RUNNING: list[Server] = []


async def start(servers=None, transport=None) -> None:
    """À appeler au DÉMARRAGE de l'application, avant qu'elle n'annonce
    ses outils. Ouvre les serveurs actifs, lance leur découverte et
    l'attend STARTUP_WAIT secondes au plus : passé ce délai, ou pour un
    serveur éteint, le proxy démarre sans ses outils, qui s'enregistrent
    quand il répond. Ne lève pas pour un serveur injoignable."""
    await stop()
    for server in SERVERS if servers is None else servers:
        if not server.enabled:
            continue
        if server.missing:
            log.warning("MCP %s : en-tête(s) %s sans valeur (variable "
                        "d'environnement absente ?) — non envoyé(s)",
                        server.name, ", ".join(server.missing))
        server.open(transport)
        _RUNNING.append(server)
        _TASKS.append(asyncio.create_task(server.watch(),
                                          name=f"mcp-{server.name}"))
    if not _RUNNING:
        return
    try:
        await asyncio.wait_for(
            asyncio.gather(*(s.first.wait() for s in _RUNNING)), STARTUP_WAIT)
    except asyncio.TimeoutError:
        log.warning("MCP : %s sans réponse après %d s — le proxy démarre, "
                    "leurs outils viendront",
                    ", ".join(s.name for s in _RUNNING if not s.first.is_set()),
                    int(STARTUP_WAIT))


async def stop() -> None:
    """À appeler à l'ARRÊT de l'application : arrête les découvertes,
    ferme les sessions et les clients. Les outils restent au registre,
    désactivés."""
    for task in _TASKS:
        task.cancel()
    await asyncio.gather(*_TASKS, return_exceptions=True)
    for server in _RUNNING:
        await server.close()
    _TASKS.clear()
    _RUNNING.clear()


def kinds() -> frozenset:
    """Les types de déclaration des serveurs de la CONFIGURATION, qu'ils
    aient répondu ou non : `mcp`, et `mcp:<serveur>` pour chacun. Un
    client qui déclare `{"type": "mcp"}` alors qu'aucun serveur n'a encore
    été joint reçoit ainsi le refus du proxy, qui dit pourquoi, et non
    celui d'un backend à qui ce type ne dit rien."""
    if not SERVERS:
        return frozenset()
    return frozenset({KIND, *(f"{KIND}:{s.name}" for s in SERVERS)})


def status() -> list[dict]:
    """L'état des serveurs, pour un tableau de bord ou /healthz : jamais
    les en-têtes, ni la requête de l'URL."""
    return [{"name": s.name, "url": s.where, "enabled": s.enabled,
             "up": s.up, "error": s.error, "protocol": s.version if s.era
             else None, "era": s.era,
             "tools": [t.name for t in s.live()]} for s in SERVERS]
