"""
Un backend ANTHROPIC natif (`api = "anthropic"` sur [backends.<nom>]) :
le proxy lui parle Messages (POST /v1/messages), tout le reste du proxy
continue de parler chat/completions.

L'adaptateur se tient au seul point de sortie (relaying.send_upstream) :
  * la requête chat/completions est traduite en requête Messages ;
  * la réponse (JSON, ou flux SSE) est retraduite en chat/completions,
    octet par octet pour un flux — les surfaces (chat, Responses,
    Anthropic, outils hébergés, page) n'en savent rien.

Ce que la traduction ajoute à ce que ferait la couche « OpenAI » d'Anthropic :
  * le CACHE DE PROMPT (cache_control automatique) : un agent renvoie tout
    l'historique à chaque tour, le préfixe est relu au tarif du cache ;
  * la RÉFLEXION : demandée en résumé (`display: "summarized"`), rendue en
    `reasoning_content`, et ses blocs signés gardés en mémoire pour être
    rejoués avec les appels d'outils qui les suivent (le client ne les
    renvoie pas) ;
  * `reasoning_effort` → `output_config.effort` ;
  * le CATALOGUE lu sur /v1/models d'Anthropic : contexte
    (max_input_tokens), sortie maximale, images (capabilities).

Paramètres d'échantillonnage (`temperature`, `top_p`, `top_k`) retirés :
les modèles récents refusent toute valeur autre que celle par défaut.
"""

import copy
import json
import time
import uuid
from collections import OrderedDict
from datetime import datetime

from ..core import efforts
from ..core.settings import log
from .openai import Adapted

VERSION = "2023-06-01"
# Les blocs de réflexion signés sont liés à la conversation qui les a
# produits (système, outils, messages d'avant) : rejoués dans une requête
# dont le système ou les outils ont changé (la page, un client qui ajoute
# un outil), ils donnent un 400. Avec ce bêta et `drop_block`, Anthropic
# les écarte simplement : la requête passe, sans cette réflexion-là.
BINDING_BETA = "thinking-binding-controls-2026-08-01"

# Sortie demandée quand le client n'en dit rien : bornée par celle du
# modèle (catalogue).
DEFAULT_MAX_TOKENS = 32000

FINISH = {"end_turn": "stop", "stop_sequence": "stop", "pause_turn": "stop",
          "max_tokens": "length", "tool_use": "tool_calls",
          "refusal": "content_filter"}

EFFORTS = ("low", "medium", "high", "xhigh", "max")


def headers(api_key: str) -> dict:
    h = {"anthropic-version": VERSION, "content-type": "application/json",
         "accept-encoding": "identity"}
    if api_key:
        h["x-api-key"] = api_key
    return h


# ── les tours à rejouer ─────────────────────────────────────────────────
# Un tour qui appelle des outils doit être rejoué AVEC ses blocs de
# réflexion signés, et DANS SON ORDRE : une réflexion déplacée (après le
# texte qui la suivait…) est écartée par Anthropic (vérifié contre l'API).
# Le client chat/completions ne garde ni l'une ni l'autre : le proxy garde
# le contenu complet du tour, tel qu'Anthropic l'a rendu, indexé par l'id
# de son premier appel d'outil. Mémoire bornée, perdue au redémarrage (la
# requête passe alors sans réflexion).
_TURNS: "OrderedDict[str, list]" = OrderedDict()
_TURNS_MAX = 4096


def _remember(blocks: list) -> None:
    """Garde le contenu d'un tour qui a appelé des outils."""
    calls = [b for b in blocks if b.get("type") == "tool_use" and b.get("id")]
    if not calls:
        return
    key = calls[0]["id"]
    _TURNS[key] = blocks
    _TURNS.move_to_end(key)
    while len(_TURNS) > _TURNS_MAX:
        _TURNS.popitem(last=False)


def _safe_id(value) -> str:
    """Un id d'appel d'outil accepté par Anthropic ([A-Za-z0-9_-]) ; le
    même calcul à l'aller et au retour garde l'appariement."""
    s = "".join(c if c.isalnum() or c in "_-" else "_" for c in str(value or ""))
    return s or "call_" + uuid.uuid4().hex[:12]


# ── requête : chat/completions → Messages ───────────────────────────────

def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content
                         if isinstance(p, dict) and p.get("type") == "text"
                         and isinstance(p.get("text"), str))
    return ""


def _image(url: str) -> dict | None:
    if url.startswith("data:"):
        head, _, data = url.partition(",")
        media = head[5:].split(";")[0] or "image/png"
        if ";base64" not in head or not data:
            return None
        return {"type": "image", "source": {"type": "base64",
                                            "media_type": media, "data": data}}
    if url.startswith(("http://", "https://")):
        return {"type": "image", "source": {"type": "url", "url": url}}
    return None


def _user_blocks(content) -> list:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content.strip() else []
    blocks = []
    for part in content if isinstance(content, list) else ():
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text" and isinstance(part.get("text"), str) and part["text"].strip():
            blocks.append({"type": "text", "text": part["text"]})
        elif kind == "image_url":
            url = part.get("image_url")
            url = url.get("url") if isinstance(url, dict) else url
            img = _image(url) if isinstance(url, str) else None
            if img:
                blocks.append(img)
    return blocks


def _assistant_blocks(msg: dict) -> list:
    """Le tour de l'assistant. Rendu par Anthropic et revenu inchangé
    (mêmes appels, même texte) : son contenu d'origine, réflexion et ordre
    compris. Sinon (tour d'un autre modèle, modifié par le client) : sa
    réflexion connue en tête, puis le texte et les appels."""
    calls = [c for c in msg.get("tool_calls") or [] if isinstance(c, dict)]
    uses = []
    for c in calls:
        fn = c.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except (TypeError, ValueError):
            args = {}
        uses.append({"type": "tool_use", "id": _safe_id(c.get("id")),
                     "name": str(fn.get("name") or ""),
                     "input": args if isinstance(args, dict) else {"value": args}})
    text = _text(msg.get("content"))
    turn = _TURNS.get(uses[0]["id"]) if uses else None
    if turn is not None and _same(turn, text, uses):
        return copy.deepcopy(turn)
    blocks = [copy.deepcopy(b) for b in turn or ()
              if b.get("type") in ("thinking", "redacted_thinking")]
    if text.strip():
        blocks.append({"type": "text", "text": text})
    return blocks + uses


def _same(turn: list, text: str, uses: list) -> bool:
    """Le tour d'origine est-il celui que le client renvoie ?"""
    mine = [(u["id"], u["name"], u["input"]) for u in uses]
    theirs = [(b.get("id"), b.get("name"), b.get("input") or {})
              for b in turn if b.get("type") == "tool_use"]
    said = "".join(b.get("text") or "" for b in turn if b.get("type") == "text")
    return mine == theirs and said.strip() == text.strip()


def _tool_result(msg: dict) -> dict:
    content = msg.get("content")
    if isinstance(content, list):
        content = _user_blocks(content) or ""
    return {"type": "tool_result", "tool_use_id": _safe_id(msg.get("tool_call_id")),
            "content": content if content else "(vide)"}


def _messages(chat: list) -> tuple[str, list]:
    """(system, messages) : les messages système et développeur remontés
    en tête, les résultats d'outils en blocs `tool_result` d'un tour
    utilisateur, deux tours du même rôle fusionnés."""
    system, out = [], []
    for msg in chat:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role in ("system", "developer"):
            t = _text(msg.get("content"))
            if t.strip():
                system.append(t)
            continue
        if role == "assistant":
            role, blocks = "assistant", _assistant_blocks(msg)
        elif role in ("tool", "function"):
            role, blocks = "user", [_tool_result(msg)]
        else:
            role, blocks = "user", _user_blocks(msg.get("content"))
        if not blocks:
            continue
        if out and out[-1]["role"] == role:
            # Les résultats d'outils d'abord : Anthropic les veut en tête
            # du tour qui suit les appels.
            prev = out[-1]["content"]
            if role == "user" and any(b["type"] == "tool_result" for b in blocks):
                prev[:] = [b for b in prev if b["type"] == "tool_result"] + blocks \
                    + [b for b in prev if b["type"] != "tool_result"]
            else:
                prev += blocks
        else:
            out.append({"role": role, "content": blocks})
    return "\n\n".join(system), out


def _tool_choice(choice, parallel) -> dict | None:
    """`tool_choice` d'OpenAI → celui d'Anthropic. Forcer un appel
    (« required », une fonction nommée → `any`, `tool`) est refusé (400)
    par les modèles récents (Opus 5.5, Sonnet 5.5, Fable 5.1 et après) :
    ramené à `auto`, l'appel reste au choix du modèle."""
    out = None
    if choice in ("auto", "required") or isinstance(choice, dict):
        out = {"type": "auto"}
    elif choice == "none":
        out = {"type": "none"}
    if parallel is False:
        out = (out or {"type": "auto"})
        if out["type"] != "none":
            out["disable_parallel_tool_use"] = True
    return out


def to_messages(payload: dict, caps: dict | None = None,
                max_out: int | None = None) -> dict:
    """La requête Messages d'une requête chat/completions (modèle déjà
    sans préfixe). `caps` : les `capabilities` du modèle au catalogue
    d'Anthropic (réflexion, effort), `max_out` : sa sortie maximale."""
    caps = caps or {}
    system, messages = _messages(payload.get("messages") or [])
    limit = payload.get("max_completion_tokens") or payload.get("max_tokens")
    if not isinstance(limit, int) or limit <= 0:
        limit = min(DEFAULT_MAX_TOKENS, max_out or DEFAULT_MAX_TOKENS)
    body = {"model": payload.get("model"), "max_tokens": limit,
            "messages": messages,
            # Le préfixe (outils, système, historique) relu au tarif du
            # cache au tour suivant.
            "cache_control": {"type": "ephemeral"}}
    if system:
        body["system"] = system
    if payload.get("stream"):
        body["stream"] = True
    stop = payload.get("stop")
    stop = [stop] if isinstance(stop, str) else stop
    if isinstance(stop, list):
        stop = [s for s in stop if isinstance(s, str) and s.strip()]
        if stop:
            body["stop_sequences"] = stop
    tools = []
    for t in payload.get("tools") or []:
        fn = t.get("function") if isinstance(t, dict) else None
        if isinstance(fn, dict) and fn.get("name"):
            params = fn.get("parameters")
            tools.append({"name": fn["name"],
                          "description": str(fn.get("description") or ""),
                          "input_schema": params if isinstance(params, dict)
                          else {"type": "object", "properties": {}}})
    if tools:
        body["tools"] = tools
        choice = _tool_choice(payload.get("tool_choice"),
                              payload.get("parallel_tool_calls"))
        if choice:
            body["tool_choice"] = choice
    output = {}
    # L'effort : `reasoning_effort`, ou celui que les clients de llama.cpp
    # passent au gabarit (`chat_template_kwargs` : pi, omp ; « non » de la
    # page). La réflexion ne se coupe pas sur les modèles récents :
    # « sans » devient l'effort le plus bas.
    effort = (str(payload.get("reasoning_effort") or "").lower()
              or efforts.template_effort(payload.get("chat_template_kwargs")))
    effort = {"minimal": "low", "none": "low", "off": "low"}.get(effort, effort)
    supported = caps.get("effort") if isinstance(caps.get("effort"), dict) else {}
    if effort in EFFORTS and (supported.get(effort) or {}).get("supported"):
        output["effort"] = effort
    fmt = payload.get("response_format")
    if isinstance(fmt, dict) and fmt.get("type") == "json_schema":
        schema = (fmt.get("json_schema") or {}).get("schema")
        if isinstance(schema, dict):
            output["format"] = {"type": "json_schema", "schema": schema}
    if output:
        body["output_config"] = output
    thinking = caps.get("thinking") if isinstance(caps.get("thinking"), dict) else {}
    adaptive = ((thinking.get("types") or {}).get("adaptive") or {}).get("supported")
    if adaptive:
        body["thinking"] = {"type": "adaptive", "display": "summarized",
                            "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
    return body


# ── réponse : Messages → chat/completions ───────────────────────────────

def _usage(u: dict) -> dict:
    u = u if isinstance(u, dict) else {}
    cached = int(u.get("cache_read_input_tokens") or 0)
    prompt = int(u.get("input_tokens") or 0) + cached \
        + int(u.get("cache_creation_input_tokens") or 0)
    completion = int(u.get("output_tokens") or 0)
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "prompt_tokens_details": {"cached_tokens": cached}}


def from_message(msg: dict) -> dict:
    """Une réponse Messages complète → chat.completion."""
    text, reasoning, calls = [], [], []
    for b in msg.get("content") or []:
        kind = b.get("type")
        if kind == "text":
            text.append(b.get("text") or "")
        elif kind == "thinking":
            if b.get("thinking"):
                reasoning.append(b["thinking"])
        elif kind == "tool_use":
            calls.append({"id": b.get("id"), "type": "function",
                          "function": {"name": b.get("name"),
                                       "arguments": json.dumps(b.get("input") or {},
                                                               ensure_ascii=False)}})
    _remember(list(msg.get("content") or []))
    message = {"role": "assistant", "content": "".join(text) or None}
    if reasoning:
        message["reasoning_content"] = "\n\n".join(reasoning)
    if calls:
        message["tool_calls"] = calls
    return {"id": msg.get("id") or "chatcmpl-" + uuid.uuid4().hex,
            "object": "chat.completion", "created": int(time.time()),
            "model": msg.get("model"),
            "choices": [{"index": 0, "message": message,
                         "finish_reason": FINISH.get(msg.get("stop_reason"), "stop")}],
            "usage": _usage(msg.get("usage"))}


def error_body(raw: bytes, status: int) -> bytes:
    """Une erreur Anthropic ({"type": "error", "error": {…}}) à la forme
    OpenAI ; un corps illisible garde son texte."""
    try:
        err = json.loads(raw).get("error") or {}
    except (ValueError, AttributeError):
        err = {"message": raw.decode("utf-8", "replace")[:600]}
    return json.dumps({"error": {
        "message": str(err.get("message") or f"upstream {status}"),
        "type": str(err.get("type") or "upstream_error"),
        "code": err.get("type")}}, ensure_ascii=False).encode()


def _chunk(base: dict, delta: dict, finish=None, usage=None) -> bytes:
    doc = {**base, "choices": [{"index": 0, "delta": delta,
                                "finish_reason": finish}]}
    if usage is not None:
        doc["choices"] = []
        doc["usage"] = usage
    return b"data: " + json.dumps(doc, ensure_ascii=False).encode() + b"\n\n"


class StreamTranslator:
    """Le flux SSE de Messages → le flux chat.completion.chunk."""

    def __init__(self):
        self._buf = b""
        self._base = {"id": "chatcmpl-" + uuid.uuid4().hex,
                      "object": "chat.completion.chunk",
                      "created": int(time.time()), "model": None}
        self._usage: dict = {}
        self._tools: dict[int, int] = {}     # index du bloc → index de l'appel
        # Le contenu du tour, reconstitué bloc par bloc (index → bloc), à
        # garder pour le rejouer (_remember) ; les arguments des appels
        # arrivent en morceaux de JSON.
        self._blocks: dict[int, dict] = {}
        self._args: dict[int, str] = {}
        self._finish = None

    def feed(self, chunk: bytes) -> bytes:
        # Les fins de ligne CRLF (une passerelle) : ramenées à LF, la
        # paire pouvant arriver coupée entre deux morceaux.
        self._buf = (self._buf + chunk).replace(b"\r\n", b"\n")
        out = b""
        while b"\n\n" in self._buf:
            event, self._buf = self._buf.split(b"\n\n", 1)
            data = b"".join(line[5:].strip() for line in event.split(b"\n")
                            if line.startswith(b"data:"))
            if data:
                try:
                    out += self._event(json.loads(data))
                except (ValueError, AttributeError, TypeError) as exc:
                    log.warning("[anthropic] événement illisible : %s", exc)
        return out

    def finish(self) -> bytes:
        return b""

    def _event(self, ev: dict) -> bytes:
        kind = ev.get("type")
        if kind == "message_start":
            msg = ev.get("message") or {}
            self._base["id"] = msg.get("id") or self._base["id"]
            self._base["model"] = msg.get("model")
            self._usage.update(msg.get("usage") or {})
            return _chunk(self._base, {"role": "assistant", "content": ""})
        if kind == "content_block_start":
            i, block = ev.get("index", 0), ev.get("content_block") or {}
            self._blocks[i] = dict(block)
            if block.get("type") == "tool_use":
                n = self._tools[i] = len(self._tools)
                self._args[i] = ""
                return _chunk(self._base, {"tool_calls": [{
                    "index": n, "id": block.get("id"), "type": "function",
                    "function": {"name": block.get("name"), "arguments": ""}}]})
            if block.get("type") == "text" and block.get("text"):
                return _chunk(self._base, {"content": block["text"]})
            return b""
        if kind == "content_block_delta":
            i, d = ev.get("index", 0), ev.get("delta") or {}
            dt = d.get("type")
            block = self._blocks.setdefault(i, {})
            if dt == "text_delta":
                block["text"] = block.get("text", "") + d.get("text", "")
                return _chunk(self._base, {"content": d.get("text", "")})
            if dt == "input_json_delta" and i in self._tools:
                self._args[i] += d.get("partial_json", "")
                return _chunk(self._base, {"tool_calls": [{
                    "index": self._tools[i],
                    "function": {"arguments": d.get("partial_json", "")}}]})
            if dt == "thinking_delta":
                block.setdefault("type", "thinking")
                block["thinking"] = block.get("thinking", "") + d.get("thinking", "")
                if d.get("thinking"):
                    return _chunk(self._base, {"reasoning_content": d["thinking"]})
            if dt == "signature_delta":
                block.setdefault("type", "thinking")
                block["signature"] = block.get("signature", "") + d.get("signature", "")
            return b""
        if kind == "message_delta":
            self._usage.update((ev.get("usage") or {}))
            self._finish = FINISH.get((ev.get("delta") or {}).get("stop_reason"), "stop")
            return _chunk(self._base, {}, finish=self._finish)
        if kind == "message_stop":
            for i, raw in self._args.items():
                try:
                    self._blocks[i]["input"] = json.loads(raw) if raw else {}
                except ValueError:
                    self._blocks[i]["input"] = {}
            _remember([self._blocks[i] for i in sorted(self._blocks)])
            return _chunk(self._base, {}, usage=_usage(self._usage)) + b"data: [DONE]\n\n"
        if kind == "error":
            err = ev.get("error") or {}
            return (b"data: " + json.dumps({"error": {
                "message": str(err.get("message") or "upstream error"),
                "type": str(err.get("type") or "upstream_error")}},
                ensure_ascii=False).encode() + b"\n\n" + b"data: [DONE]\n\n")
        return b""


class Upstream(Adapted):
    """La réponse d'Anthropic, vue comme une réponse chat/completions."""

    def __init__(self, raw, stream: bool):
        sse = stream and raw.status_code < 400 \
            and "text/event-stream" in raw.headers.get("content-type", "")
        headers = {k: v for k, v in raw.headers.items()
                   if k.lower() in ("request-id", "retry-after")
                   or k.lower().startswith("anthropic-ratelimit")}
        headers["content-type"] = ("text/event-stream; charset=utf-8"
                                   if sse else "application/json")
        super().__init__(raw, sse, headers)
        self._translator = StreamTranslator()

    def _feed(self, chunk: bytes) -> bytes:
        return self._translator.feed(chunk)

    def _whole(self, raw: bytes) -> bytes:
        if self.status_code >= 400:
            return error_body(raw, self.status_code)
        try:
            return json.dumps(from_message(json.loads(raw)), ensure_ascii=False).encode()
        except (ValueError, AttributeError) as exc:
            log.warning("[anthropic] réponse illisible : %s", exc)
            return error_body(raw, self.status_code)


# ── catalogue ───────────────────────────────────────────────────────────

def _created(value) -> int:
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return 0


def catalog_entry(m: dict) -> dict:
    """Un modèle de /v1/models d'Anthropic, à la forme que le catalogue du
    proxy normalise : type et `architecture` tirés des capabilities,
    contexte de max_input_tokens ; `api` (catalogue multi-protocoles,
    OpenCode Zen) gardé, le catalogue y lit si Messages le sert."""
    caps = m.get("capabilities") if isinstance(m.get("capabilities"), dict) else {}
    images = bool((caps.get("image_input") or {}).get("supported", True))
    # Les efforts que le modèle accepte (capabilities.effort) : ceux que
    # le catalogue annonce (reasoning_efforts).
    effort = caps.get("effort") if isinstance(caps.get("effort"), dict) else {}
    efforts = [e for e in EFFORTS if (effort.get(e) or {}).get("supported")]
    return {"id": m.get("id"), "created": _created(m.get("created_at")),
            "owned_by": "anthropic",
            "type": "image-text-to-text" if images else "text-generation",
            "architecture": {"input_modalities": ["text", "image"] if images else ["text"],
                             "output_modalities": ["text"]},
            "max_context_length": m.get("max_input_tokens"),
            **({"reasoning_efforts": efforts} if efforts else {}),
            **({"api": m["api"]} if isinstance(m.get("api"), dict) else {})}


async def read_models(b, hdrs: dict) -> list:
    """Tous les modèles du compte (pagination par after_id). Lève sur
    une réponse en erreur, comme le lecteur générique du catalogue."""
    out, after = [], None
    for _ in range(20):
        params = {"limit": 1000, **({"after_id": after} if after else {})}
        r = await b.client.get("/v1/models", headers=hdrs, params=params,
                               timeout=b.meta_timeout)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        doc = r.json()
        for m in doc.get("data") or []:
            if isinstance(m, dict) and m.get("id"):
                b.model_caps[str(m["id"]).lower()] = m.get("capabilities") or {}
                if isinstance(m.get("max_tokens"), int):
                    b.model_max_out[str(m["id"]).lower()] = m["max_tokens"]
                out.append(catalog_entry(m))
        if not doc.get("has_more") or not doc.get("last_id"):
            break
        after = doc["last_id"]
    return out
