"""
La surface Anthropic du proxy : ce qu'il faut pour qu'un client écrit
pour l'API Messages — Claude Code en premier lieu — parle à un backend
OpenAI sans le savoir. UNIQUEMENT dans ce sens : le proxy ne sait pas
parler à un backend Anthropic, et n'en a pas besoin.

Ce qu'un client Anthropic appelle, et ce qu'il reçoit :
  POST /v1/messages               → traduit en /v1/chat/completions,
                                    réponse retraduite (JSON ou flux SSE)
  POST /v1/messages/count_tokens  → estimation locale (aucun équivalent
                                    OpenAI), même approximation que le
                                    limiteur : ~4 caractères par token
  GET  /v1/models                 → le catalogue fusionné, à la forme
                                    Anthropic (discriminé sur l'en-tête
                                    `anthropic-version`, que le SDK
                                    Anthropic pose sur CHAQUE requête et
                                    que le SDK OpenAI ne pose jamais)

Le routage ne change pas : le modèle demandé est d'abord passé par
[anthropic.model_map], parce que Claude Code envoie des noms Claude en
dur (ses tâches d'arrière-plan ignorent ANTHROPIC_MODEL) — la table les
traduit en noms PRÉFIXÉS, que backends.py route comme d'habitude.

La traduction de la RÉPONSE est la seule partie délicate : le relais
(app.forward) ne connaît que des octets, il les fait passer par un
«robinet» (tap) — feed(chunk) rend ce qu'il faut émettre, finish() le
reliquat, tokens() ce que les stats doivent compter. stats.UsageCollector
est le robinet identité ; Translator, ici, celui qui réécrit. Un flux
OpenAI (deltas plats, outils fragmentés par index) devient la séquence
d'événements Anthropic (message_start, blocs ouverts/fermés un à un,
message_delta, message_stop). Contrairement au relais brut, CE chemin
désérialise chaque événement : on ne peut pas réécrire sans lire.

Ce module ne connaît ni FastAPI ni httpx.
"""

import datetime as _dt
import json
import re
import uuid

from . import config

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
# En flux, pendant l'attente du limiteur : un `event: ping` toutes les N
# secondes garde la connexion vivante (Claude Code coupe un flux muet ;
# l'API Anthropic elle-même envoie ces pings). 0 = attendre AVANT de
# répondre, comme pour un client OpenAI.
PING_INTERVAL = config.num("anthropic.ping_interval", 10)
# `reasoning_content` d'un backend → bloc `thinking` pour le client. Le
# bloc part sans signature (Claude Code le renvoie, on le jette à la
# traduction) ; à couper si un client s'en plaint.
REASONING_AS_THINKING = config.flag("anthropic.reasoning_as_thinking", True)
CHARS_PER_TOKEN = 4

# «claude-opus-5[1m]» : le suffixe entre crochets est un choix de
# contexte côté Claude Code, pas un modèle. Ignoré pour la recherche.
_BRACKET_SUFFIX = re.compile(r"\[[^\]]*\]$")

STOP_REASONS = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "refusal",
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
    """count_tokens : ce qui est compté. Le corps sérialisé (system +
    messages + tools) — c'est aussi ce que le limiteur approxime, et ce
    qu'un /tokenize de backend reçoit."""
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
    """Y a-t-il au moins un bloc image dans la requête (messages, y
    compris à l'intérieur des tool_result) ? Évite de charger un
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
    """Ce qu'un backend texte seul reçoit à la place d'un média : un mot
    qui dit qu'il manque quelque chose, plutôt que rien."""
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
    libre. OpenAI veut un message `tool` PAR résultat, placés juste après
    l'assistant qui les a demandés — donc avant le reste. Un `tool`
    OpenAI n'a qu'un contenu TEXTE : les images d'un tool_result (Claude
    Code lisant un .png) suivent dans un message user à part."""
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


def _assistant_message(content) -> dict:
    """thinking / redacted_thinking sont JETÉS : aucun backend OpenAI ne
    les rejoue, et leur signature n'a de sens que chez Anthropic."""
    msg: dict = {"role": "assistant"}
    if isinstance(content, str):
        msg["content"] = content
        return msg
    text, calls = [], []
    for b in content if isinstance(content, list) else []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            text.append(b.get("text", ""))
        elif t == "tool_use":
            calls.append({
                "id": str(b.get("id") or _tool_id()),
                "type": "function",
                "function": {
                    "name": str(b.get("name", "")),
                    "arguments": json.dumps(b.get("input") or {},
                                            ensure_ascii=False),
                },
            })
    msg["content"] = "".join(text) or None
    if calls:
        msg["tool_calls"] = calls
    return msg


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


def to_openai(p: dict, images: bool = False) -> dict:
    """Corps /v1/messages → corps /v1/chat/completions. `model` est
    recopié tel quel : l'appelant l'a déjà résolu (resolve_model).
    `images` : le backend accepte les `image_url` (sinon, texte de
    remplacement). Tout ce qui n'a pas d'équivalent (thinking, top_k,
    cache_control, metadata hors user_id, output_config,
    context_management…) est ignoré plutôt que relayé à un backend qui
    le refuserait."""
    out: dict = {"model": p.get("model", "")}
    messages: list[dict] = []
    system = _text_of(p.get("system"))
    if system:
        messages.append({"role": "system", "content": system})
    for m in p.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role, content = m.get("role"), m.get("content")
        if role == "assistant":
            messages.append(_assistant_message(content))
        elif role == "user":
            messages.extend(_user_message(content, images))
        elif role == "system":
            text = _text_of(content)
            if text:
                messages.append({"role": "system", "content": text})
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

    tools = [
        {"type": "function", "function": {
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": t.get("input_schema")
            or {"type": "object", "properties": {}},
        }}
        for t in p.get("tools") or []
        # Les outils serveur Anthropic (web_search, bash, text_editor…)
        # ont un `type` et pas d'input_schema : rien à traduire.
        if isinstance(t, dict) and t.get("name") and "input_schema" in t
    ]
    if tools:
        out["tools"] = tools
        _tool_choice(p.get("tool_choice"), out)
    return out


# ── Réponse : OpenAI → Anthropic ────────────────────────────────────────

def _tool_id() -> str:
    return "toolu_" + uuid.uuid4().hex[:24]


def _msg_id() -> str:
    return "msg_" + uuid.uuid4().hex[:24]


def _usage(u) -> dict:
    u = u if isinstance(u, dict) else {}
    return {
        "input_tokens": int(u.get("prompt_tokens") or 0),
        "output_tokens": int(u.get("completion_tokens") or 0),
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


def _parse_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        obj = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def from_openai(doc: dict, model: str) -> dict:
    """Réponse non streamée /v1/chat/completions → objet Message."""
    choice = (doc.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content: list[dict] = []
    reasoning = msg.get("reasoning_content") or msg.get("reasoning")
    if REASONING_AS_THINKING and isinstance(reasoning, str) and reasoning:
        content.append({"type": "thinking", "thinking": reasoning,
                        "signature": ""})
    if isinstance(msg.get("content"), str) and msg["content"]:
        content.append({"type": "text", "text": msg["content"]})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        content.append({
            "type": "tool_use",
            "id": str(tc.get("id") or _tool_id()),
            "name": str(fn.get("name", "")),
            "input": _parse_args(fn.get("arguments")),
        })
    finish = choice.get("finish_reason")
    stop = STOP_REASONS.get(finish, "end_turn")
    if any(b["type"] == "tool_use" for b in content) and finish != "length":
        stop = "tool_use"
    return {
        "id": str(doc.get("id") or _msg_id()),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": _usage(doc.get("usage")),
    }


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
    rendus sont la réponse Anthropic, pas ceux de l'upstream.

    Trois modes, fixés à l'ouverture par le statut et le content-type
    upstream :
      * erreur (statut ≠ 2xx) : le corps est bufferisé, finish() rend
        une erreur Anthropic ;
      * JSON : bufferisé, finish() rend le Message traduit ;
      * SSE : traduit au fil de l'eau, événement par événement.
    """

    def __init__(self, status: int, content_type: str, model: str):
        ct = (content_type or "").lower()
        self.ok = 200 <= status < 300
        self.status = status
        self.sse = self.ok and "text/event-stream" in ct
        self.model = model
        self._buf = bytearray()
        self.usage: dict | None = None
        self.out_chars = 0
        # État du flux.
        self._started = False
        self._finished = False
        self._msg_id = ""
        self._next_block = 0
        self._open: str | None = None     # "text" | "thinking" | "tool"
        self._open_index = -1
        self._tools: dict[int, int] = {}  # index OpenAI → index de bloc
        self._finish_reason: str | None = None
        self._saw_tool = False

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
            return json.dumps(error_body("réponse upstream illisible",
                                         "api_error")).encode()
        self.usage = doc.get("usage") if isinstance(doc.get("usage"), dict) \
            else None
        msg = from_openai(doc, self.model)
        self.out_chars = sum(len(b.get("text", "")) for b in msg["content"])
        return json.dumps(msg, ensure_ascii=False).encode()

    def tokens(self, fallback_prompt: int) -> tuple[int, int, bool]:
        u = self.usage or {}
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
        if self._open is None:
            return b""
        out = _sse("content_block_stop",
                   {"type": "content_block_stop", "index": self._open_index})
        self._open = None
        return out

    def _open_block(self, kind: str, block: dict) -> bytes:
        out = self._close()
        self._open, self._open_index = kind, self._next_block
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
            # Erreur en cours de flux : l'événement `error`, puis on
            # clôt proprement ce qui était ouvert.
            err = from_openai_error(doc, 500)["error"]
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
                out += self._delta({"type": "thinking_delta",
                                    "thinking": reasoning})
            text = delta.get("content")
            if isinstance(text, str) and text:
                if self._open != "text":
                    out += self._open_block("text", {"type": "text", "text": ""})
                self.out_chars += len(text)
                out += self._delta({"type": "text_delta", "text": text})
            for tc in delta.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                idx = tc.get("index", 0)
                fn = tc.get("function") or {}
                if idx not in self._tools:
                    self._saw_tool = True
                    out += self._open_block("tool", {
                        "type": "tool_use",
                        "id": str(tc.get("id") or _tool_id()),
                        "name": str(fn.get("name") or ""),
                        "input": {},
                    })
                    self._tools[idx] = self._open_index
                elif self._open != "tool" or self._open_index != self._tools[idx]:
                    # Fragment tardif d'un outil déjà fermé : impossible
                    # en pratique, ignoré plutôt que de casser le flux.
                    continue
                args = fn.get("arguments")
                if isinstance(args, str) and args:
                    out += self._delta({"type": "input_json_delta",
                                        "partial_json": args})
            if choice.get("finish_reason"):
                self._finish_reason = choice["finish_reason"]
        return bytes(out)

    def _end(self) -> bytes:
        if self._finished:
            return b""
        self._finished = True
        out = bytearray(self._start({}))
        out += self._close()
        stop = STOP_REASONS.get(self._finish_reason, "end_turn")
        if self._saw_tool and self._finish_reason != "length":
            stop = "tool_use"
        usage = _usage(self.usage)
        out += _sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None},
            "usage": usage,
        })
        out += _sse("message_stop", {"type": "message_stop"})
        return bytes(out)


def _est(chars: int) -> int:
    return max(chars // CHARS_PER_TOKEN, 1) if chars else 0
