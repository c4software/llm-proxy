"""
Le FLUX : le robinet (Translator) qui traduit, à la volée, le flux SSE
chat/completions en événements Messages.
"""

import json

from .common import (
    CHARS_PER_TOKEN,
    ERROR_CODES,
    REASONING_AS_THINKING,
    error_body,
    error_type,
)
from .request import Context
from .response import (
    from_openai_error,
    _CONTENT,
    _cached,
    _message,
    _message_blocks,
    _msg_id,
    _parse_args,
    _pending,
    _sse,
    _stop_reason,
    _tool_id,
    _usage,
)


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

    def _last(self) -> dict | None:
        """L'usage rendu au CLIENT, à la forme chat/completions : celui du
        dernier tour upstream. Son prompt relit tous les tours précédents :
        c'est le contexte occupé, ce que mesure la jauge d'un client."""
        turns = self._past + ([self.usage] if isinstance(self.usage, dict) else [])
        return turns[-1] if turns else None

    def _total(self) -> dict | None:
        """L'usage CONSOMMÉ (quotas, statistiques, logs), à la forme
        chat/completions : celui de l'upstream s'il n'y a eu qu'un tour, la
        SOMME sinon — chaque tour relit tout le préfixe."""
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
        usage = _usage(self._last())
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
