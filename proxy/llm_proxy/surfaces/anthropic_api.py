"""
La surface Anthropic du proxy : un client de l'API Messages parle à un
backend OpenAI sans le savoir. Dans ce sens uniquement : le proxy ne
parle pas à un backend Anthropic.

  POST /v1/messages               → traduit en /v1/chat/completions,
                                    réponse retraduite (JSON ou flux SSE)
  POST /v1/messages/count_tokens  → estimation locale, ~4 caractères par
                                    token (même ratio que le limiteur)
  GET  /v1/models                 → le catalogue à la forme Anthropic,
                                    choisi sur l'en-tête
                                    `anthropic-version` (le SDK Anthropic
                                    le pose toujours, le SDK OpenAI jamais)

Le modèle demandé passe d'abord par [anthropic.model_map] : certains
clients (Claude Code) envoient des noms Claude en dur ; la table les
traduit en noms préfixés, routés comme d'habitude.

La réponse passe par un «robinet» (tap) du relais (relaying.forward) :
feed(chunk) rend ce qu'il faut émettre, finish() le reliquat, tokens() ce
que les stats comptent. Translator réécrit un flux OpenAI en événements
Anthropic ; contrairement au relais brut, il désérialise chaque événement.

Outils serveur hébergés (`web_search_…`, `web_fetch_…`, paquet tools/) :
l'outil serveur déclaré par le client devient une fonction présentée au
modèle ; ses appels ne sont pas rendus en `tool_use` mais mis de côté
(`pending`), exécutés par tool_loop.py qui relance le backend, et le
client reçoit `server_tool_use` puis le bloc de résultat. Rien n'est
conservé entre deux requêtes : le bloc de résultat porte tout, son rejeu
rend au modèle le même texte (voir _assistant_messages). Un outil n'est
présenté que si le client le déclare.

Ce module ne fait aucune requête et ne connaît ni FastAPI ni httpx2.
"""

import datetime as _dt
import json
import re
import time
import uuid

from ..core import config
from ..tools.contract import Source

# Noms de modèles Anthropic → noms préfixés du proxy. «default» attrape
# tout nom sans préfixe backend qui n'est pas dans la table.
_raw_map = config.section("anthropic").get("model_map")
MODEL_MAP: dict[str, str] = {
    str(k).strip().lower(): str(v).strip()
    for k, v in (_raw_map.items() if isinstance(_raw_map, dict) else ())
    if str(v).strip()
}
# Absente du TOML = surface inactive : un déploiement existant n'expose
# rien de nouveau sans l'avoir demandé.
ENABLED = config.flag("anthropic.enabled", False)
# Une ligne de log par réponse /v1/messages : stop_reason, outils appelés
# (nom + extrait des arguments), tokens — pour voir ce qu'un agent qui
# s'emballe répète.
TRACE = config.flag("anthropic.trace", False)
# En flux, pendant l'attente du limiteur : un `event: ping` toutes les N
# secondes garde la connexion vivante (certains clients coupent un flux
# muet ; l'API Anthropic envoie aussi ces pings). 0 = attendre AVANT de
# répondre.
PING_INTERVAL = config.num("anthropic.ping_interval", 10)
# `reasoning_content` d'un backend → bloc `thinking` pour le client, sans
# signature (rejoué par le client, il est jeté à la traduction).
REASONING_AS_THINKING = config.flag("anthropic.reasoning_as_thinking", True)
CHARS_PER_TOKEN = 4

# «claude-opus-5[1m]» : le suffixe entre crochets est un choix de
# contexte du client, pas un modèle. Ignoré pour la recherche.
_BRACKET_SUFFIX = re.compile(r"\[[^\]]*\]$")

STOP_REASONS = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "refusal",
}

# Les outils serveur exécutés par le proxy sont ceux de tools/ qui portent
# une liaison `anthropic` (tools.Anthropic) : préfixe du type, toutes
# versions datées confondues, et bloc de résultat — erreur en
# `<bloc>_error`, compteur `<préfixe>_requests` dans
# `usage.server_tool_use`. Ce module ne connaît que le format de ces blocs
# (_CONTENT et _REPLAY, plus bas).

# Codes d'erreur du contrat des outils (tools.ERRORS) → `error_code` des
# outils serveur d'Anthropic. Le modèle lit le texte de l'erreur ; le
# client Anthropic n'en reçoit que ce code.
ERROR_CODES = {
    "invalid_input": "invalid_tool_input",
    "not_allowed": "url_not_allowed",
    "not_accessible": "url_not_accessible",
    "unsupported": "unsupported_content_type",
    "too_many_requests": "too_many_requests",
    "limit": "max_uses_exceeded",
    "timeout": "unavailable",
    # L'outil a tourné et dit avoir échoué : Anthropic n'a pas de code
    # commun pour cela, et seul le modèle en lit le détail.
    "failed": "unavailable",
    "unavailable": "unavailable",
}

# Types d'erreur de l'API Anthropic par statut HTTP.
ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    429: "rate_limit_error",
    500: "api_error",
    529: "overloaded_error",
}


def resolve_model(name: str, backends) -> str | None:
    """Nom tel que le client l'envoie → nom préfixé routable, ou None.
    Ordre : table exacte, table sans suffixe «[…]», nom déjà préfixé
    par un backend connu (passe tel quel), «default»."""
    key = str(name or "").strip().lower()
    if not key:
        return MODEL_MAP.get("default")
    if key in MODEL_MAP:
        return MODEL_MAP[key]
    bare = _BRACKET_SUFFIX.sub("", key)
    if bare in MODEL_MAP:
        return MODEL_MAP[bare]
    if any(key.startswith(b + "/") for b in backends):
        return str(name).strip()
    return MODEL_MAP.get("default")


def error_type(status: int) -> str:
    if status in ERROR_TYPES:
        return ERROR_TYPES[status]
    return "api_error" if status >= 500 else "invalid_request_error"


def error_body(message: str, type_: str) -> dict:
    return {"type": "error", "error": {"type": type_, "message": message}}


def prompt_text(payload) -> str:
    """count_tokens : le corps sérialisé (system + messages + tools), ce
    que le limiteur approxime et qu'un /tokenize de backend reçoit."""
    doc = {k: payload.get(k) for k in ("system", "messages", "tools")
           if isinstance(payload, dict) and payload.get(k) is not None}
    return json.dumps(doc, ensure_ascii=False)


def estimate_tokens(payload) -> int:
    """Approximation locale, même ratio que le limiteur."""
    return max(len(prompt_text(payload)) // CHARS_PER_TOKEN, 1)


def models_list(entries: list[dict]) -> dict:
    """Le catalogue fusionné (entrées déjà préfixées, forme OpenAI du
    proxy) à la forme Anthropic."""
    data = []
    for m in entries:
        created = m.get("created") or 0
        data.append({
            "type": "model",
            "id": m["id"],
            "display_name": m["id"],
            "created_at": _iso(created),
        })
    return {"data": data, "has_more": False,
            "first_id": data[0]["id"] if data else None,
            "last_id": data[-1]["id"] if data else None}


def _iso(ts) -> str:
    try:
        return _dt.datetime.fromtimestamp(
            int(ts), _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError, TypeError):
        return "1970-01-01T00:00:00Z"


# ── Requête : Anthropic → OpenAI ────────────────────────────────────────

def _text_of(content) -> str:
    """Texte d'un contenu (chaîne, ou liste de blocs dont on ne garde
    que les `text`)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _image_part(block: dict) -> dict | None:
    src = block.get("source")
    if not isinstance(src, dict):
        return None
    if src.get("type") == "base64" and src.get("data"):
        media = src.get("media_type") or "image/png"
        return {"type": "image_url",
                "image_url": {"url": f"data:{media};base64,{src['data']}"}}
    if src.get("type") == "url" and src.get("url"):
        return {"type": "image_url", "image_url": {"url": src["url"]}}
    return None


def has_images(payload: dict) -> bool:
    """Au moins un bloc image (tool_result compris) ? Évite de charger un
    catalogue pour rien."""
    for m in payload.get("messages") or []:
        content = m.get("content") if isinstance(m, dict) else None
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "image":
                return True
            if b.get("type") == "tool_result":
                inner = b.get("content")
                if isinstance(inner, list) and any(
                        isinstance(x, dict) and x.get("type") == "image"
                        for x in inner):
                    return True
    return False


def _placeholder(block: dict, what: str) -> dict:
    """Ce qu'un backend texte seul reçoit à la place d'un média."""
    src = block.get("source") or {}
    media = src.get("media_type") if isinstance(src, dict) else None
    size = len(src.get("data") or "") * 3 // 4 if isinstance(src, dict) else 0
    detail = media or ""
    if size:
        detail += f", {size // 1024} Ko" if detail else f"{size // 1024} Ko"
    return {"type": "text",
            "text": f"[{what} ignoré{'e' if what == 'image' else ''}"
                    f"{' : ' + detail if detail else ''}]"}


def _blocks_to_parts(blocks, images: bool) -> list[dict]:
    """Blocs de contenu (text / image / document) → parties OpenAI.
    `images` = le backend accepte les `image_url` ; sinon un texte de
    remplacement. Un `document` n'a d'équivalent OpenAI que s'il est du
    texte ; un PDF devient un texte de remplacement."""
    parts = []
    for b in blocks if isinstance(blocks, list) else []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            parts.append({"type": "text", "text": b.get("text", "")})
        elif t == "image":
            part = _image_part(b) if images else None
            parts.append(part or _placeholder(b, "image"))
        elif t == "document":
            src = b.get("source") or {}
            if isinstance(src, dict) and src.get("type") == "text":
                parts.append({"type": "text", "text": str(src.get("data", ""))})
            else:
                parts.append(_placeholder(b, "document"))
    return parts


def _content_of(parts: list[dict]):
    """Une chaîne si tout est texte — la forme que tous les backends
    acceptent —, la liste de parties sinon."""
    if all(p["type"] == "text" for p in parts):
        return "".join(p["text"] for p in parts)
    return parts


def _user_message(content, images: bool) -> list[dict]:
    """Un message user Anthropic peut mêler n tool_result et du contenu
    libre. OpenAI veut un message `tool` PAR résultat, juste après
    l'assistant qui les a demandés — donc avant le reste. Un `tool`
    OpenAI n'a qu'un contenu TEXTE : les images d'un tool_result suivent
    dans un message user à part."""
    if isinstance(content, str):
        return [{"role": "user", "content": content}]
    if not isinstance(content, list):
        return []
    tools, parts = [], []
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "tool_result":
            inner = b.get("content")
            if isinstance(inner, str):
                text, media = inner, []
            else:
                inner_parts = _blocks_to_parts(inner, images)
                text = "".join(p["text"] for p in inner_parts
                               if p["type"] == "text")
                media = [p for p in inner_parts if p["type"] != "text"]
            if b.get("is_error") and text:
                text = f"Error: {text}"
            call_id = str(b.get("tool_use_id", ""))
            tools.append({"role": "tool", "tool_call_id": call_id,
                          "content": text})
            if media:
                parts.append({"type": "text",
                              "text": f"[résultat de l'outil {call_id}]"})
                parts.extend(media)
        else:
            parts.extend(_blocks_to_parts([b], images))
    out = tools
    if parts:
        out.append({"role": "user", "content": _content_of(parts)})
    return out


def _prepend_text(msg: dict, text: str) -> None:
    content = msg.get("content")
    if isinstance(content, list):
        content.insert(0, {"type": "text", "text": text + "\n\n"})
    elif isinstance(content, str) and content:
        msg["content"] = text + "\n\n" + content
    else:
        msg["content"] = text


class Context:
    """Ce que la requête dit des outils hébergés, pour to_openai, le
    robinet et tool_loop.py. Vide (`hosted` = {}) si aucun outil serveur
    déclaré n'est hébergé : ils sont alors ignorés.

    `hosted` : nom de fonction → outil de tools/, déclaré ET hébergé ;
    `limits` : par fonction, appels au plus pour cette réponse (le
               `max_uses` du client, borné par tools.MAX_CALLS) ;
    `cap`    : appels au plus, toutes fonctions confondues ;
    `options`: par fonction, réglages du client que l'exécution respecte
               (`allowed_domains`, `blocked_domains`, `max_chars`).
               Anthropic refuse les deux listes à la fois (400) ; ici
               elles s'appliquent toutes les deux ;
    `order`  : les fonctions dans l'ordre du registre, celui des
               compteurs de `usage.server_tool_use`."""

    def __init__(self, request: dict, hosted=None):
        self.hosted: dict = {}
        self.limits: dict[str, int] = {}
        self.cap: int = hosted.cap() if hosted else 0
        self.options: dict[str, dict] = {}
        tools = request.get("tools")
        tools = tools if isinstance(tools, list) else []
        # Une fonction du client garde son nom : l'outil hébergé homonyme
        # n'est alors pas présenté (même règle que la surface Responses).
        taken = [t.get("name") for t in tools
                 if isinstance(t, dict) and "input_schema" in t]
        for t in tools:
            tool = server_tool(t, hosted)
            name = tool.name if tool else None
            # Déclaré deux fois : le premier fait foi.
            if tool is None or name in taken or name in self.hosted:
                continue
            self.hosted[name] = tool
            self.limits[name] = hosted.cap(t.get("max_uses"))
            options: dict = {k: [str(d) for d in t[k]]
                             for k in ("allowed_domains", "blocked_domains")
                             if isinstance(t.get(k), list) and t[k]}
            # `max_content_tokens` (web fetch), converti en caractères :
            # il ne fait que BAISSER la borne du proxy.
            size = t.get("max_content_tokens")
            if isinstance(size, int) and not isinstance(size, bool) \
                    and size > 0:
                options["max_chars"] = size * CHARS_PER_TOKEN
            if options:
                self.options[name] = options
        self.order: list[str] = [t.name for t in hosted.tools
                                 if t.name in self.hosted] if hosted else []


def server_tool(tool, hosted=None):
    """L'outil hébergé que remplace cet outil du client, ou None : une
    fonction du client (`input_schema`), un outil serveur que le proxy
    n'héberge pas — ou dont ce module ne sait pas écrire le bloc."""
    if not hosted or not isinstance(tool, dict) or "input_schema" in tool:
        return None
    found = hosted.for_server(str(tool.get("type") or ""))
    return found if found and found.anthropic.block in _CONTENT else None


def _replayed_error(tool, code) -> str:
    """Texte rendu au modèle pour un résultat en erreur REJOUÉ par le
    client : le bloc n'en garde que le code."""
    return (f"Error: the {tool.name.replace('_', ' ')} failed "
            f"({code or 'unavailable'}).")


def _search_sources(content) -> tuple:
    """L'inverse de _search_content."""
    return tuple(Source(str(r.get("url") or ""), str(r.get("title") or ""),
                        str(r.get("page_age") or ""),
                        str(r.get("encrypted_content") or ""))
                 for r in content
                 if isinstance(r, dict) and r.get("type") == "web_search_result")


def _search_text(tool, use: dict, result: dict) -> str:
    """Le texte qu'avait lu le modèle, reconstruit sans mémoire depuis les
    deux blocs rejoués : le bloc de résultat porte tout, et Tool.render le
    remet en texte à l'octet près."""
    content = result.get("content")
    if not isinstance(content, list):
        code = content.get("error_code") if isinstance(content, dict) else None
        return _replayed_error(tool, code)
    args = use.get("input") if isinstance(use.get("input"), dict) else {}
    return tool.render(args, _search_sources(content))


def _fetch_text(tool, use: dict, result: dict) -> str:
    """Le texte qu'avait lu le modèle : la `data` du document rejoué, où
    _fetch_content l'a mis ENTIER. Un document qui n'est pas du texte (PDF
    d'une conversation commencée chez Anthropic) rend une lecture en
    échec."""
    content = result.get("content")
    content = content if isinstance(content, dict) else {}
    doc = content.get("content")
    source = doc.get("source") if isinstance(doc, dict) else None
    if content.get("type") == "web_fetch_result" and isinstance(source, dict) \
            and source.get("type") == "text" \
            and isinstance(source.get("data"), str):
        return source["data"]
    return _replayed_error(tool, content.get("error_code"))


# Bloc de résultat → comment relire, d'un bloc rejoué par le client, le
# texte qu'avait lu le modèle : (outil, bloc de l'appel, bloc du résultat).
_REPLAY = {"web_search_tool_result": _search_text,
           "web_fetch_tool_result": _fetch_text}


def _assistant_messages(content, hosted=None, results=None) -> list[dict]:
    """thinking / redacted_thinking sont JETÉS : aucun backend OpenAI ne
    les rejoue, et leur signature n'a de sens que chez Anthropic.

    Un message assistant Anthropic donne UN message OpenAI — sauf s'il
    porte des appels exécutés par le proxy (`hosted`). Chaque paire
    `server_tool_use` + bloc de résultat redevient un appel suivi de son
    message `tool`, et ce qui suit un résultat ouvre un nouveau message
    assistant (un autre tour du backend) : [texte, recherche, résultat,
    texte] rend assistant(texte + appel), tool, assistant(texte). C'est ce
    que le backend a vu pendant la boucle, et tool_loop.py le reconstruit
    par ce même chemin : le préfixe reste stable d'un tour et d'une requête
    à l'autre.

    `results` : id d'appel → texte exact rendu au modèle, pour la réponse
    EN COURS ; un bloc rejoué est relu par _REPLAY. Un `server_tool_use`
    non hébergé ou sans son résultat est ignoré : un appel sans message
    `tool` serait refusé par le backend."""
    if isinstance(content, str):
        return [{"role": "assistant", "content": content}]
    blocks = [b for b in content if isinstance(b, dict)] \
        if isinstance(content, list) else []
    hosted = hosted or {}
    kinds = [t.anthropic.block for t in hosted.values()]
    answers = {str(b.get("tool_use_id")): b for b in blocks
               if b.get("type") in kinds}
    out: list[dict] = []
    text: list[str] = []
    calls: list[dict] = []
    tools: list[dict] = []

    def flush() -> None:
        msg: dict = {"role": "assistant", "content": "".join(text) or None}
        if calls:
            msg["tool_calls"] = list(calls)
        out.append(msg)
        out.extend(tools)
        text.clear(), calls.clear(), tools.clear()

    for b in blocks:
        t = b.get("type")
        name = b.get("name")
        ours = t == "server_tool_use" and isinstance(name, str) \
            and name in hosted and answers.get(
                str(b.get("id")), {}).get("type") == hosted[name].anthropic.block
        if tools and (ours or t in ("text", "tool_use")):
            flush()
        if t == "text":
            text.append(b.get("text", ""))
        elif t == "tool_use" or ours:
            call_id = str(b.get("id") or _tool_id())
            calls.append({
                "id": call_id,
                "type": "function",
                "function": {
                    "name": str(b.get("name", "")),
                    "arguments": json.dumps(b.get("input") or {},
                                            ensure_ascii=False),
                },
            })
            if ours:
                known = (results or {}).get(call_id)
                if not isinstance(known, str):
                    tool = hosted[name]
                    known = _REPLAY[tool.anthropic.block](
                        tool, b, answers[call_id])
                tools.append({"role": "tool", "tool_call_id": call_id,
                              "content": known})
    if text or calls or not out:
        flush()
    return out


def _tool_choice(value, payload: dict) -> None:
    if not isinstance(value, dict):
        return
    t = value.get("type")
    if t == "auto":
        payload["tool_choice"] = "auto"
    elif t == "any":
        payload["tool_choice"] = "required"
    elif t == "none":
        payload["tool_choice"] = "none"
    elif t == "tool" and value.get("name"):
        payload["tool_choice"] = {"type": "function",
                                  "function": {"name": value["name"]}}
    if value.get("disable_parallel_tool_use"):
        payload["parallel_tool_calls"] = False


def to_openai(p: dict, images: bool = False, hosted=None,
              results: dict | None = None) -> dict:
    """Corps /v1/messages → corps /v1/chat/completions. `model` est déjà
    résolu (resolve_model). Ce qui n'a pas d'équivalent (thinking, top_k,
    cache_control, output_config…) est ignoré plutôt que relayé à un
    backend qui le refuserait.
    `images` : le backend accepte les `image_url`. `hosted` : l'annuaire
    des outils exécutés par le proxy (tools.Hosted), ou None. `results` :
    voir _assistant_messages."""
    ctx = Context(p, hosted)
    out: dict = {"model": p.get("model", "")}
    messages: list[dict] = []
    system = _text_of(p.get("system"))
    if system:
        messages.append({"role": "system", "content": system})
    # Un message `system` EN COURS de conversation (rappels de certains
    # clients) : beaucoup de gabarits de chat (Qwen, Mistral…) n'acceptent
    # un system qu'en tête et répondent 500 sinon. Son texte est fondu en
    # tête du message user qui le suit.
    # Suivi d'un assistant, il est posé en message user À SA PLACE, avant
    # lui : c'est là qu'il était au tour précédent, quand il fermait la
    # requête. Le déplacer ferait diverger le préfixe d'un tour à l'autre,
    # et un backend à cache de préfixe (llama.cpp…) recalculerait le
    # dernier échange à chaque tour.
    pending: list[str] = []
    for m in p.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role, content = m.get("role"), m.get("content")
        if role == "assistant":
            if pending:
                messages.append({"role": "user", "content": "\n\n".join(pending)})
                pending = []
            messages.extend(_assistant_messages(content, ctx.hosted, results))
        elif role == "user":
            batch = _user_message(content, images)
            if pending:
                text = "\n\n".join(pending)
                pending = []
                if batch and batch[-1]["role"] == "user":
                    _prepend_text(batch[-1], text)
                else:
                    # Que des tool_result : le texte suit, en message user.
                    batch.append({"role": "user", "content": text})
            messages.extend(batch)
        elif role == "system":
            text = _text_of(content)
            if text:
                pending.append(text)
    if pending:
        messages.append({"role": "user", "content": "\n\n".join(pending)})
    out["messages"] = messages

    if isinstance(p.get("max_tokens"), int):
        out["max_tokens"] = p["max_tokens"]
    for src, dst in (("temperature", "temperature"), ("top_p", "top_p"),
                     ("stop_sequences", "stop")):
        if p.get(src) is not None:
            out[dst] = p[src]
    if p.get("stream"):
        out["stream"] = True
        # Sans lui, un flux SSE OpenAI ne porte aucun `usage` : les stats
        # retomberaient sur l'estimation, et le client ne saurait rien.
        out["stream_options"] = {"include_usage": True}
    meta = p.get("metadata")
    if isinstance(meta, dict) and meta.get("user_id"):
        out["user"] = str(meta["user_id"])

    tools = []
    present = frozenset(ctx.hosted)
    left = dict(ctx.hosted)         # fonctions hébergées restant à présenter
    for t in p.get("tools") or []:
        if not isinstance(t, dict):
            continue
        if t.get("name") and "input_schema" in t:
            tools.append({"type": "function", "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema")
                or {"type": "object", "properties": {}},
            }})
        elif (tool := server_tool(t, hosted)) and tool.name in left:
            # Sa fonction à la place de l'outil serveur, à la même
            # position, une seule fois. Une description ne renvoie à une
            # autre fonction que si elle est présentée aussi (`present`).
            tools.append(left.pop(tool.name).spec(present))
        # Les autres outils serveur Anthropic (code_execution, bash,
        # text_editor…) ont un `type` et pas d'input_schema : rien à
        # traduire.
    if tools:
        out["tools"] = tools
        _tool_choice(p.get("tool_choice"), out)
    return out


# ── Réponse : OpenAI → Anthropic ────────────────────────────────────────

def _tool_id() -> str:
    return "toolu_" + uuid.uuid4().hex[:24]


def _server_tool_id() -> str:
    return "srvtoolu_" + uuid.uuid4().hex[:24]


def _msg_id() -> str:
    return "msg_" + uuid.uuid4().hex[:24]


def _cached(u) -> int:
    details = u.get("prompt_tokens_details") if isinstance(u, dict) else None
    if isinstance(details, dict) and isinstance(details.get("cached_tokens"), int):
        return max(details["cached_tokens"], 0)
    return 0


def _usage(u) -> dict:
    """Usage Anthropic. Chez Anthropic, input_tokens EXCLUT les tokens
    lus en cache ; chez OpenAI, prompt_tokens les inclut — on soustrait,
    pour que le client additionne juste."""
    u = u if isinstance(u, dict) else {}
    cached = _cached(u)
    prompt = int(u.get("prompt_tokens") or 0)
    return {
        "input_tokens": max(prompt - cached, 0),
        "output_tokens": int(u.get("completion_tokens") or 0),
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": cached,
    }


def _parse_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        obj = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _message_blocks(msg: dict, hosted: dict) -> tuple[list[dict], list[dict]]:
    """Message d'une réponse non streamée → (blocs de `content`, appels
    hébergés à exécuter). Un appel dont le nom est dans `hosted` ne donne
    pas de bloc `tool_use` : il attend d'être exécuté (voir _pending)."""
    content: list[dict] = []
    pending: list[dict] = []
    reasoning = msg.get("reasoning_content") or msg.get("reasoning")
    if REASONING_AS_THINKING and isinstance(reasoning, str) and reasoning:
        content.append({"type": "thinking", "thinking": reasoning,
                        "signature": ""})
    if isinstance(msg.get("content"), str) and msg["content"].strip():
        content.append({"type": "text", "text": msg["content"]})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        name = str(fn.get("name", ""))
        if name in hosted:
            args = fn.get("arguments")
            pending.append(_pending(name, args if isinstance(args, str)
                                    else json.dumps(args or {})))
            continue
        content.append({
            "type": "tool_use",
            "id": str(tc.get("id") or _tool_id()),
            "name": name,
            "input": _parse_args(fn.get("arguments")),
        })
    return content, pending


def _stop_reason(finish, client_tool: bool, searched: bool = False) -> str:
    """`searched` : la réponse a exécuté des appels hébergés. Un
    dernier tour clos sur `tool_calls` sans aucun `tool_use` pour le
    client (le modèle cherchait encore quand la boucle s'est arrêtée)
    n'est pas un `tool_use` : le client n'a rien à exécuter."""
    stop = STOP_REASONS.get(finish, "end_turn")
    if client_tool and finish != "length":
        return "tool_use"
    if searched and stop == "tool_use":
        return "end_turn"
    return stop


def _message(msg_id: str, model: str, content: list[dict], stop: str,
             usage: dict) -> dict:
    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": usage,
    }


def from_openai(doc: dict, model: str) -> dict:
    """Réponse non streamée /v1/chat/completions → objet Message, sans
    outil hébergé (ceux-là passent par Translator.finish)."""
    choice = (doc.get("choices") or [{}])[0]
    content, _ = _message_blocks(choice.get("message") or {}, {})
    stop = _stop_reason(choice.get("finish_reason"),
                        any(b["type"] == "tool_use" for b in content))
    return _message(str(doc.get("id") or _msg_id()), model, content, stop,
                    _usage(doc.get("usage")))


# ── outils hébergés : ce que le client en voit ──

def _pending(name: str, arguments: str) -> dict:
    """Un appel hébergé à exécuter (Translator.pending). `id` : celui des
    deux blocs ; `arguments` : tels que le modèle les a écrits, c'est ce
    texte que l'exécution reçoit."""
    return {"id": _server_tool_id(), "name": name,
            "arguments": arguments or "{}", "announced": False}


def _search_content(call: dict, result) -> list[dict]:
    """Résultat d'une recherche → contenu du bloc `web_search_tool_result`.
    `encrypted_content` : chez Anthropic, un blob opaque que le client
    renvoie tel quel. Ici on y met l'EXTRAIT, en clair : le bloc porte
    tout ce que le modèle a lu, et son rejeu n'a besoin d'aucune mémoire.
    `page_age` : la date du résultat (AAAA-MM-JJ), ou null."""
    return [{"type": "web_search_result", "title": s.title,
             "url": s.url, "encrypted_content": s.snippet,
             "page_age": s.date or None}
            for s in result.sources]


def _fetch_content(call: dict, result) -> dict:
    """Résultat d'une lecture → contenu du bloc `web_fetch_tool_result`.
    `data` : le texte ENTIER que le modèle a lu, en-tête compris, pas le
    seul corps de la page — ce qui rend le rejeu exact sans mémoire.
    `url` : celle réellement lue, après redirections."""
    return {"type": "web_fetch_result",
            "url": str(result.meta.get("url") or "")
            or str(_parse_args(call["arguments"]).get("url") or ""),
            "content": {"type": "document",
                        "source": {"type": "text", "media_type": "text/plain",
                                   "data": result.text},
                        "title": result.meta.get("title") or None},
            "retrieved_at": _iso(time.time())}


# Bloc de résultat → comment en écrire le contenu, pour un appel abouti :
# (appel, résultat). Les blocs que ce module sait rendre : un outil lié à
# un autre bloc n'est pas présenté (server_tool).
_CONTENT = {"web_search_tool_result": _search_content,
            "web_fetch_tool_result": _fetch_content}


def from_openai_error(doc, status: int) -> dict:
    """Erreur OpenAI {"error": {"message", "type"}} → erreur Anthropic.
    Un corps qui n'est pas de cette forme est relayé en message."""
    message = ""
    if isinstance(doc, dict):
        err = doc.get("error")
        if isinstance(err, dict):
            message = str(err.get("message") or "")
        elif isinstance(err, str):
            message = err
        else:
            message = str(doc.get("message") or doc.get("detail") or "")
    elif isinstance(doc, str):
        message = doc
    return error_body(message or f"upstream HTTP {status}", error_type(status))


def _sse(event: str, data: dict) -> bytes:
    return (f"event: {event}\ndata: "
            + json.dumps(data, ensure_ascii=False) + "\n\n").encode()


def ping_event() -> bytes:
    return _sse("ping", {"type": "ping"})


def sse_error(body: dict) -> bytes:
    """Une erreur (forme error_body) émise DANS un flux déjà ouvert —
    la seule façon de la dire une fois le 200 parti."""
    return _sse("error", body)


class Translator:
    """Le robinet de réponse pour /v1/messages : même interface que
    stats.UsageCollector (feed / finish / tokens / sse), mais les octets
    rendus sont la réponse Anthropic.

    Trois modes, fixés par le statut et le content-type upstream : erreur
    (bufferisé, finish() rend une erreur Anthropic), JSON (bufferisé,
    finish() rend le Message), SSE (traduit événement par événement).

    Outils hébergés : UNE réponse peut couvrir PLUSIEURS tours upstream —
    même contrat que responses_api.Translator, piloté par tool_loop.py.
    Un appel hébergé est rangé dans `pending`, et finish() ne clôt alors
    PAS le message. L'appelant exécute chaque appel, le rend par
    resolve(), puis next_turn() et un nouveau flux, ou finalize().
    `pending` vide après finish() = message clos. L'identité du message
    traverse les tours ; l'usage est CUMULÉ.

    Pendant le tour du backend, un appel hébergé ne produit rien ; à la
    fin du tour, les blocs sortent par paires (`server_tool_use` juste
    avant l'exécution, bloc de résultat dès qu'il est connu), chaque
    résultat à la suite de son appel.
    """

    def __init__(self, status: int, content_type: str, model: str,
                 ctx: Context | None = None):
        ct = (content_type or "").lower()
        self.ok = 200 <= status < 300
        self.status = status
        self.sse = self.ok and "text/event-stream" in ct
        self.model = model
        self.ctx = ctx
        self.hosted: dict = ctx.hosted if ctx else {}
        self._buf = bytearray()
        self.usage: dict | None = None    # usage upstream du tour en cours
        self._past: list[dict] = []       # usages des tours précédents
        self.out_chars = 0
        # Appels hébergés à exécuter : voir _pending().
        self.pending: list[dict] = []
        # Appels CLIENT (`tool_use`) rendus pendant le tour en cours :
        # s'il y en a, la main revient au client, pas de tour suivant.
        self.client_calls = 0
        self.turns = 1
        # Texte exact rendu au modèle pour chaque appel hébergé de CETTE
        # réponse, par id d'appel : le tour suivant le reprend tel quel
        # (to_openai, `results`), erreurs et troncature comprises.
        self.results: dict[str, str] = {}
        # Par fonction : appels hébergés qui ont abouti (usage).
        self._done: dict[str, int] = {}
        self._started = False
        self._finished = False
        self._msg_id = ""
        self._content: list[dict] = []    # blocs terminés, dans l'ordre
        self._next_block = 0
        self._open: str | None = None     # "text" | "thinking" | "tool"
        self._open_index = -1
        self._block: dict = {}            # le bloc ouvert, et ce qu'il a reçu
        self._parts: list[str] = []
        self._tools: dict[int, int] = {}  # index OpenAI → index de bloc
        # Appels hébergés du tour, retenus : index OpenAI → [nom, fragments].
        self._held: dict[int, list] = {}
        self._finish_reason: str | None = None
        self._blank = ""                  # blancs retenus avant un texte
        self._saw_tool = False
        # Pour la trace : les appels de la réponse, [nom, fragments
        # d'arguments], dans l'ordre ; `_trace_open` : ceux du tour en
        # cours, par index OpenAI.
        self._trace: list[list] = []
        self._trace_open: dict[int, list] = {}

    # ── interface robinet ──
    def feed(self, chunk: bytes) -> bytes:
        if not self.sse:
            self._buf += chunk
            return b""
        self._buf += chunk
        out = bytearray()
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                break
            line = bytes(self._buf[:nl]).rstrip(b"\r")
            del self._buf[:nl + 1]
            if line.startswith(b"data:"):
                out += self._data(line[5:].strip())
        return bytes(out)

    def finish(self) -> bytes:
        if self.sse:
            return self._end()
        body = bytes(self._buf)
        self._buf.clear()
        try:
            doc = json.loads(body) if body else {}
        except ValueError:
            doc = body.decode("utf-8", "replace")
        if not self.ok:
            return json.dumps(from_openai_error(doc, self.status),
                              ensure_ascii=False).encode()
        if not isinstance(doc, dict):
            if self.turns > 1:
                return self.fail("réponse upstream illisible")
            return json.dumps(error_body("réponse upstream illisible",
                                         "api_error")).encode()
        self.usage = doc.get("usage") if isinstance(doc.get("usage"), dict) \
            else None
        choice = (doc.get("choices") or [{}])[0]
        blocks, pending = _message_blocks(choice.get("message") or {},
                                          self.hosted)
        if self.turns == 1:
            self._msg_id = str(doc.get("id") or _msg_id())
        self._content += blocks
        self.pending += pending
        self.out_chars += sum(len(b.get("text", "")) for b in blocks)
        self._finish_reason = choice.get("finish_reason")
        for b in blocks:
            if b["type"] == "tool_use":
                self._saw_tool = True
                self.client_calls += 1
                self._trace.append(
                    [b["name"], [json.dumps(b["input"], ensure_ascii=False)]])
        if self.pending:
            # Recherches à exécuter : le message n'est pas fini, rien ne part.
            self._announce()
            return b""
        return self.finalize()

    # ── recherche hébergée : plusieurs tours upstream pour un message ──
    @property
    def content(self) -> list[dict]:
        """Les blocs du message : en message assistant ajouté aux
        `messages` d'origine, ils reconstruisent le tour suivant (to_openai
        les relit comme ceux qu'un client rejoue)."""
        return list(self._content)

    def _announce(self) -> bytes:
        """Le bloc `server_tool_use` du prochain appel à exécuter, rangé
        dans `content` et émis. Un seul à la fois, pour que chaque
        résultat suive son appel.
        `input` : les arguments réels du modèle (même ceux que les outils
        d'Anthropic ne connaissent pas : un rejeu doit les redonner),
        illisibles → {}, en UN `input_json_delta` de JSON valide."""
        if not self.pending or self.pending[0]["announced"]:
            return b""
        call = self.pending[0]
        call["announced"] = True
        block = {"type": "server_tool_use", "id": call["id"],
                 "name": call["name"], "input": _parse_args(call["arguments"])}
        self._content.append(block)
        args = json.dumps(block["input"], ensure_ascii=False)
        self._trace.append([call["name"], [args]])
        if not self.sse:
            return b""
        return self._whole({**block, "input": {}},
                           {"type": "input_json_delta", "partial_json": args})

    def resolve(self, call: dict, result) -> bytes:
        """Le résultat (tools.Result) d'un appel de `pending` : son bloc
        suit celui de l'appel. En erreur, le client n'en reçoit que le code
        (ERROR_CODES), le modèle lit le texte. Le texte n'est gardé que
        pour le tour suivant de CETTE réponse."""
        name = call["name"]
        kind = self.hosted[name].anthropic.block
        self.results[call["id"]] = result.text
        if result.error is None:
            self._done[name] = self._done.get(name, 0) + 1
            content = _CONTENT[kind](call, result)
        else:
            content = {"type": kind + "_error",
                       "error_code": ERROR_CODES.get(result.error,
                                                     "unavailable")}
        block = {"type": kind, "tool_use_id": call["id"], "content": content}
        self._content.append(block)
        self.pending = [c for c in self.pending if c is not call]
        out = self._whole(block) if self.sse else b""
        return out + self._announce()

    def next_turn(self) -> None:
        """Avant de recevoir le flux upstream suivant : l'état propre au
        tour repart de zéro (les index d'outils OpenAI recommencent à 0),
        l'identité du message reste."""
        if isinstance(self.usage, dict):
            self._past.append(self.usage)
        self.usage = None
        self._buf.clear()
        self._open = None
        self._tools = {}
        self._held = {}
        self._trace_open = {}
        self._finish_reason = None
        self._blank = ""
        self.client_calls = 0
        self.turns += 1

    def finalize(self) -> bytes:
        """Clôt le message : `message_delta` + `message_stop` en flux, le
        corps de l'objet Message en JSON. Sans effet s'il l'est déjà."""
        if self._finished:
            return b""
        if self.sse:
            return self._end(force=True)
        self._finished = True
        return json.dumps(
            _message(self._msg_id or _msg_id(), self.model,
                     list(self._content), self._stop(), self._final_usage()),
            ensure_ascii=False).encode()

    def fail(self, message: str, status: int = 500) -> bytes:
        """Échec d'un tour ULTÉRIEUR : en flux le 200 est parti, il ne
        reste que l'`event: error` ; en JSON c'est le corps d'erreur, dont
        tool_loop.py fixe le statut HTTP."""
        if self._finished:
            return b""
        out = (self._start({}) + self._close()) if self.sse else b""
        self._finished = True
        self.pending, self._held = [], {}
        body = error_body(message, error_type(status))
        if self.sse:
            return out + _sse("error", body)
        return json.dumps(body, ensure_ascii=False).encode()

    def _total(self) -> dict | None:
        """L'usage de la réponse, à la forme chat/completions : celui de
        l'upstream s'il n'y a eu qu'un tour, la SOMME sinon — chaque tour
        relit tout le préfixe, et c'est bien ce qui a été consommé."""
        turns = self._past + ([self.usage] if isinstance(self.usage, dict) else [])
        if len(turns) <= 1:
            return turns[0] if turns else None
        return {
            "prompt_tokens": sum(int(u.get("prompt_tokens") or 0) for u in turns),
            "completion_tokens": sum(
                int(u.get("completion_tokens") or 0) for u in turns),
            "prompt_tokens_details": {
                "cached_tokens": sum(_cached(u) for u in turns)},
        }

    def _final_usage(self) -> dict:
        """L'usage Anthropic du message. `server_tool_use` ne compte que
        les appels hébergés aboutis (comme chez Anthropic)."""
        usage = _usage(self._total())
        if self._done:
            usage["server_tool_use"] = {
                self.hosted[name].anthropic.prefix + "_requests":
                    self._done[name]
                for name in self.ctx.order if name in self._done}
        return usage

    def _stop(self) -> str:
        return _stop_reason(self._finish_reason, self._saw_tool,
                            bool(self.results))

    def cached(self) -> int:
        return _cached(self._total())

    def summary(self) -> str:
        """Une ligne : ce que le modèle a répondu, pour les logs."""
        u = self._total() or {}
        parts = [f"stop={self._stop()}",
                 f"in={u.get('prompt_tokens', '?')} out={u.get('completion_tokens', '?')}"]
        if self._trace:
            calls = []
            for name, fragments in self._trace:
                args = "".join(fragments)
                calls.append(f"{name}({args[:120]}{'…' if len(args) > 120 else ''})")
            parts.append("tools: " + " ; ".join(calls))
        elif self.out_chars:
            parts.append(f"text: {self.out_chars} car.")
        if not self.ok:
            parts.append(f"HTTP {self.status}")
        return " | ".join(parts)

    def tokens(self, fallback_prompt: int) -> tuple[int, int, bool]:
        u = self._total() or {}
        p, c = u.get("prompt_tokens"), u.get("completion_tokens")
        if isinstance(p, int) or isinstance(c, int):
            return (p if isinstance(p, int) else fallback_prompt,
                    c if isinstance(c, int) else _est(self.out_chars), True)
        return fallback_prompt, _est(self.out_chars), False

    # ── flux ──
    def _start(self, doc: dict) -> bytes:
        if self._started:
            return b""
        self._started = True
        self._msg_id = str(doc.get("id") or _msg_id())
        return _sse("message_start", {
            "type": "message_start",
            "message": {
                "id": self._msg_id, "type": "message", "role": "assistant",
                "model": self.model, "content": [],
                "stop_reason": None, "stop_sequence": None,
                "usage": _usage(None),
            },
        })

    def _close(self) -> bytes:
        """Ferme le bloc ouvert, et le range, complet, dans `content`."""
        if self._open is None:
            return b""
        out = _sse("content_block_stop",
                   {"type": "content_block_stop", "index": self._open_index})
        received = "".join(self._parts)
        if self._open == "tool":
            self._block["input"] = _parse_args(received)
        else:
            self._block[self._open] = received     # `text` ou `thinking`
        self._content.append(self._block)
        self._open = None
        return out

    def _open_block(self, kind: str, block: dict) -> bytes:
        out = self._close()
        self._blank = ""
        self._open, self._open_index = kind, self._next_block
        self._block, self._parts = dict(block), []
        self._next_block += 1
        return out + _sse("content_block_start", {
            "type": "content_block_start", "index": self._open_index,
            "content_block": block,
        })

    def _delta(self, delta: dict) -> bytes:
        return _sse("content_block_delta", {
            "type": "content_block_delta", "index": self._open_index,
            "delta": delta,
        })

    def _whole(self, block: dict, delta: dict | None = None) -> bytes:
        """Un bloc entier d'un coup (blocs d'un appel hébergé). Aucun bloc
        n'est ouvert à ce moment-là : le tour upstream est fini."""
        index = self._next_block
        self._next_block += 1
        out = _sse("content_block_start", {
            "type": "content_block_start", "index": index,
            "content_block": block})
        if delta:
            out += _sse("content_block_delta", {
                "type": "content_block_delta", "index": index, "delta": delta})
        return out + _sse("content_block_stop",
                          {"type": "content_block_stop", "index": index})

    def _data(self, payload: bytes) -> bytes:
        if payload == b"[DONE]":
            return self._end()
        try:
            doc = json.loads(payload)
        except ValueError:
            return b""
        if not isinstance(doc, dict):
            return b""
        if "error" in doc and "choices" not in doc:
            # Erreur en cours de flux : on clôt ce qui était ouvert ; les
            # appels hébergés du tour ne seront pas exécutés.
            err = from_openai_error(doc, 500)["error"]
            self._held = {}
            return (self._start(doc) + self._close()
                    + _sse("error", {"type": "error", "error": err}))
        out = bytearray(self._start(doc))
        if isinstance(doc.get("usage"), dict):
            self.usage = doc["usage"]
        for choice in doc.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or {}
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if REASONING_AS_THINKING and isinstance(reasoning, str) and reasoning:
                if self._open != "thinking":
                    out += self._open_block("thinking", {
                        "type": "thinking", "thinking": "", "signature": ""})
                self._parts.append(reasoning)
                out += self._delta({"type": "thinking_delta",
                                    "thinking": reasoning})
            text = delta.get("content")
            if isinstance(text, str) and text and self._open != "text":
                # Des blancs seuls avant un appel d'outil ne font pas un
                # bloc de texte (un bloc vide, que l'API Anthropic refuse
                # au rejeu) : retenus jusqu'au premier caractère visible,
                # jetés si rien ne suit.
                self._blank += text
                text = "" if not self._blank.strip() else self._blank
            if isinstance(text, str) and text:
                if self._open != "text":
                    self._blank = ""
                    out += self._open_block("text", {"type": "text", "text": ""})
                self._parts.append(text)
                self.out_chars += len(text)
                out += self._delta({"type": "text_delta", "text": text})
            for tc in delta.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                idx = tc.get("index", 0)
                fn = tc.get("function") or {}
                args = fn.get("arguments")
                if idx in self._held or (idx not in self._tools
                                         and str(fn.get("name") or "") in self.hosted):
                    # Fonction hébergée : rien ne part au client pendant
                    # le tour, les arguments sont retenus (voir _hold).
                    held = self._held.setdefault(idx, [str(fn.get("name")), []])
                    if isinstance(args, str) and args:
                        held[1].append(args)
                    continue
                if idx not in self._tools:
                    self._saw_tool = True
                    self.client_calls += 1
                    out += self._open_block("tool", {
                        "type": "tool_use",
                        "id": str(tc.get("id") or _tool_id()),
                        "name": str(fn.get("name") or ""),
                        "input": {},
                    })
                    self._tools[idx] = self._open_index
                    self._trace_open[idx] = [str(fn.get("name") or "?"), []]
                    self._trace.append(self._trace_open[idx])
                elif self._open != "tool" or self._open_index != self._tools[idx]:
                    # Fragment tardif d'un outil déjà fermé : impossible
                    # en pratique, ignoré plutôt que de casser le flux.
                    continue
                if isinstance(args, str) and args:
                    self._parts.append(args)
                    self._trace_open[idx][1].append(args)
                    out += self._delta({"type": "input_json_delta",
                                        "partial_json": args})
            if choice.get("finish_reason"):
                self._finish_reason = choice["finish_reason"]
        return bytes(out)

    def _hold(self) -> bytes:
        """Fin d'un tour upstream : les appels hébergés retenus passent
        dans `pending`, dans l'ordre où le modèle les a écrits, et le
        premier est annoncé au client."""
        for idx in sorted(self._held):
            name, fragments = self._held[idx]
            self.pending.append(_pending(name, "".join(fragments)))
        self._held = {}
        return self._announce()

    def _end(self, force: bool = False) -> bytes:
        """Fin d'un flux upstream. Avec des recherches en attente, ce
        n'est que la fin d'un TOUR : le message reste ouvert, c'est
        finalize() (`force`) qui le clora."""
        if self._finished:
            return b""
        out = bytearray(self._start({}))
        out += self._close()
        out += self._hold()
        if self.pending and not force:
            return bytes(out)
        self._finished = True
        out += _sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": self._stop(), "stop_sequence": None},
            "usage": self._final_usage(),
        })
        out += _sse("message_stop", {"type": "message_stop"})
        return bytes(out)


def _est(chars: int) -> int:
    return max(chars // CHARS_PER_TOKEN, 1) if chars else 0
