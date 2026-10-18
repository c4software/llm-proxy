"""
Le FLUX : le robinet (Translator) qui traduit, à la volée, le flux SSE
chat/completions en événements Responses.
"""

import json
import time

from .common import CHARS_PER_TOKEN, REASONING_AS_SUMMARY, Context, _id
from .response import (
    CLIENT_CALLS,
    error_body,
    _cached,
    _call_item,
    _chat_items,
    _client_call,
    _final_status,
    _hosted_item,
    _message_item,
    _pending,
    _reasoning_item,
    _response,
    _text_part,
    _usage,
)


class Translator:
    """Le robinet de réponse pour /v1/responses : même interface que
    stats.UsageCollector, mais les octets rendus sont la réponse
    Responses, pas ceux de l'upstream.

    Trois modes, fixés à l'ouverture par le statut et le content-type
    upstream :
      * erreur (statut ≠ 2xx) : le corps est bufferisé, finish() rend
        une erreur à la forme OpenAI ;
      * JSON : bufferisé, finish() rend l'objet Response traduit ;
      * SSE : traduit au fil de l'eau, événement par événement.

    Outils hébergés : UNE réponse peut couvrir PLUSIEURS tours upstream.
    Un appel dont le nom est dans ctx.hosted n'est pas rendu au client :
    il est rangé dans `pending`, et finish() ne clôt alors PAS la réponse
    (pas de `response.completed`, b"" en JSON). L'appelant (tool_loop.py)
    exécute chaque appel et en rend le compte par resolve(), puis soit
    next_turn() et un nouveau flux upstream dans feed()/finish(), soit
    finalize(). `pending` vide après finish() = réponse close. L'identité
    de la réponse (id, `sequence_number`, `output`) traverse les tours ;
    l'usage est CUMULÉ.
    """

    def __init__(self, status: int, content_type: str, ctx: Context):
        ct = (content_type or "").lower()
        self.ok = 200 <= status < 300
        self.status = status
        self.sse = self.ok and "text/event-stream" in ct
        self.ctx = ctx
        self._buf = bytearray()
        self.usage: dict | None = None    # usage upstream du tour en cours
        self._past: list[dict] = []       # usages des tours précédents
        self.out_chars = 0
        # Appels hébergés du tour, à exécuter : voir _pending().
        self.pending: list[dict] = []
        # Appels CLIENT (`function_call`) rendus pendant le tour en cours :
        # s'il y en a, la main revient au client, pas de tour suivant.
        self.client_calls = 0
        self.turns = 1
        self._started = False
        self._finished = False
        self._seq = 0
        self._rid = _id("resp")
        self._created = int(time.time())
        self._output: list[dict] = []     # éléments terminés, dans l'ordre
        self._open: str | None = None     # "text" | "reasoning" | "tool" | "hosted"
        self._item_id = ""
        self._text: list[str] = []        # texte ou arguments de l'élément ouvert
        self._call: tuple[str, str] = ("", "")   # (call_id, name) de l'outil ouvert
        self._tool_index: int | None = None      # index OpenAI de l'outil ouvert
        self._closed_tools: set[int] = set()
        self._finish_reason: str | None = None
        self._blank = ""                  # blancs retenus avant un texte

    # ── interface robinet ──
    def feed(self, chunk: bytes) -> bytes:
        self._buf += chunk
        if not self.sse:
            return b""
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
            return json.dumps(error_body(doc, self.status),
                              ensure_ascii=False).encode()
        if not isinstance(doc, dict):
            if self.turns > 1:
                return self.fail("réponse upstream illisible")
            return json.dumps(error_body("réponse upstream illisible",
                                         self.status)).encode()
        self.usage = doc.get("usage") if isinstance(doc.get("usage"), dict) \
            else None
        choice = (doc.get("choices") or [{}])[0]
        items, pending = _chat_items(choice.get("message") or {}, self.ctx,
                                     len(self._output))
        self._output += items
        self.pending += pending
        self.client_calls = sum(
            1 for item in items if item["type"] in CLIENT_CALLS)
        self._finish_reason = choice.get("finish_reason")
        if self.turns == 1 and isinstance(doc.get("created"), int):
            self._created = doc["created"]
        self.out_chars += sum(
            len(part.get("text", "")) for item in items
            for part in item.get("content") or [])
        if self.pending:
            # Appels hébergés : la réponse n'est pas finie, rien ne part.
            return b""
        return self.finalize()

    # ── outils hébergés : plusieurs tours upstream pour une réponse ──
    @property
    def output(self) -> list[dict]:
        """Les éléments de la réponse, dans l'ordre : c'est avec eux,
        ajoutés à l'`input` d'origine, que le tour suivant se reconstruit
        (to_chat les relit comme il relira ceux que le client rejouera)."""
        return list(self._output)

    def resolve(self, call: dict, result) -> bytes:
        """Le résultat (tools.Result) d'un appel de `pending` : son TEXTE est
        rangé en mémoire sous l'id de l'élément (le client le rejouera sans
        son résultat), et l'élément est clos."""
        tool = self.ctx.hosted[call["name"]]
        self.ctx.memory.store(call["item_id"], call["name"],
                              call["arguments"], result.text,
                              self.ctx.client)
        item = _hosted_item(call["item_id"], tool, call["arguments"], result)
        self._output[call["index"]] = item
        self.pending = [c for c in self.pending if c is not call]
        if not self.sse:
            return b""
        at = {"output_index": call["index"], "item_id": call["item_id"]}
        return (self._event(f"response.{tool.responses.item}.completed", at)
                + self._event("response.output_item.done", {
                    "output_index": call["index"], "item": item}))

    def next_turn(self) -> None:
        """Avant de recevoir le flux upstream suivant : l'état propre au
        tour repart de zéro (les index d'outils OpenAI recommencent à 0),
        l'identité de la réponse reste."""
        if isinstance(self.usage, dict):
            self._past.append(self.usage)
        self.usage = None
        self._buf.clear()
        self._open, self._text = None, []
        self._blank = ""
        self._tool_index = None
        self._closed_tools = set()
        self._finish_reason = None
        self.client_calls = 0
        self.turns += 1

    def finalize(self) -> bytes:
        """Clôt la réponse : `response.completed` / `incomplete` en flux,
        le corps de l'objet Response en JSON. Sans effet si elle l'est déjà."""
        if self._finished:
            return b""
        if self.sse:
            return self._end(force=True)
        self._finished = True
        return json.dumps(self._snapshot(_final_status(self._finish_reason)),
                          ensure_ascii=False).encode()

    def fail(self, message: str, status: int = 500) -> bytes:
        """Échec d'un tour ULTÉRIEUR : la réponse HTTP est déjà partie, il
        ne reste que `response.failed` en flux, ou un objet Response
        `failed` en JSON. `status` est ignoré ici (l'interface est commune
        aux robinets)."""
        if self._finished:
            return b""
        if self.sse:
            return self._fail({"message": message})
        self._finished = True
        self.pending = []
        return json.dumps(self._snapshot("failed", {
            "code": "server_error", "message": message}),
            ensure_ascii=False).encode()

    def _total(self) -> dict | None:
        """L'usage de la réponse, à la forme chat/completions : celui de
        l'upstream s'il n'y a eu qu'un tour, la SOMME sinon — chaque tour
        relit tout le préfixe, et c'est bien ce qui a été consommé."""
        turns = self._past + ([self.usage] if isinstance(self.usage, dict) else [])
        if len(turns) <= 1:
            return turns[0] if turns else None
        usages = [_usage(u) for u in turns]
        return {
            "prompt_tokens": sum(u["input_tokens"] for u in usages),
            "completion_tokens": sum(u["output_tokens"] for u in usages),
            "prompt_tokens_details": {"cached_tokens": sum(
                u["input_tokens_details"]["cached_tokens"] for u in usages)},
            "completion_tokens_details": {"reasoning_tokens": sum(
                u["output_tokens_details"]["reasoning_tokens"] for u in usages)},
        }

    def cached(self) -> int:
        return _cached(self._total())

    def tokens(self, fallback_prompt: int) -> tuple[int, int, bool]:
        u = self._total() or {}
        p, c = u.get("prompt_tokens"), u.get("completion_tokens")
        if isinstance(p, int) or isinstance(c, int):
            return (p if isinstance(p, int) else fallback_prompt,
                    c if isinstance(c, int) else _est(self.out_chars), True)
        return fallback_prompt, _est(self.out_chars), False

    # ── flux ──
    def _event(self, name: str, data: dict) -> bytes:
        data = {"type": name, **data, "sequence_number": self._seq}
        self._seq += 1
        return (f"event: {name}\ndata: "
                + json.dumps(data, ensure_ascii=False) + "\n\n").encode()

    def _snapshot(self, status: str, error: dict | None = None) -> dict:
        done = status != "in_progress"
        return _response(self.ctx, self._rid, self._created, status,
                         list(self._output),
                         _usage(self._total()) if done else None,
                         self._finish_reason, error)

    def _start(self, doc: dict) -> bytes:
        if self._started:
            return b""
        self._started = True
        if isinstance(doc.get("created"), int):
            self._created = doc["created"]
        return (self._event("response.created",
                            {"response": self._snapshot("in_progress")})
                + self._event("response.in_progress",
                              {"response": self._snapshot("in_progress")}))

    def _at(self) -> dict:
        """Où se place l'élément ouvert : son rang dans `output`, son id."""
        return {"output_index": len(self._output), "item_id": self._item_id}

    def _open_item(self, kind: str, item: dict, announce: bool = True) -> bytes:
        """`announce` faux : l'élément s'ouvre sans `output_item.added` —
        un `local_shell_call` n'a pas de forme sans son action, il n'est
        annoncé qu'à sa clôture."""
        out = self._close()
        self._blank = ""
        self._open, self._item_id, self._text = kind, item["id"], []
        if not announce:
            return out
        return out + self._event("response.output_item.added", {
            "output_index": len(self._output), "item": item})

    def _close(self) -> bytes:
        """Ferme l'élément ouvert : ses événements `done`, puis l'élément
        complet, rangé dans `output`."""
        if self._open is None:
            return b""
        at, text = self._at(), "".join(self._text)
        out = bytearray()
        if self._open == "hosted":
            # Appel que le proxy exécute : l'élément prend sa place dans
            # `output` (son index est réservé) mais reste en cours — il
            # sera clos par resolve(), une fois le résultat connu.
            _, name = self._call
            tool = self.ctx.hosted[name]
            self.pending.append(_pending(self._item_id, name, text or "{}",
                                         len(self._output)))
            self._output.append(_hosted_item(self._item_id, tool))
            if self._tool_index is not None:
                self._closed_tools.add(self._tool_index)
            self._tool_index = None
            self._open = None
            return self._event(f"response.{tool.responses.item}.searching", at)
        if self._open == "text":
            item = _message_item(self._item_id, text, "completed")
            out += self._event("response.output_text.done", {
                **at, "content_index": 0, "text": text, "logprobs": []})
            out += self._event("response.content_part.done", {
                **at, "content_index": 0, "part": _text_part(text)})
        elif self._open == "reasoning":
            item = _reasoning_item(self._item_id, text)
            out += self._event("response.reasoning_summary_text.done", {
                **at, "summary_index": 0, "text": text})
            out += self._event("response.reasoning_summary_part.done", {
                **at, "summary_index": 0,
                "part": {"type": "summary_text", "text": text}})
        else:
            call_id, name = self._call
            form = self.ctx.form(name)
            item = _client_call(self.ctx, call_id, name,
                                text or "{}" if form == "function" else text,
                                self._item_id)
            if form == "custom":
                # L'entrée n'est connue qu'une fois les arguments JSON
                # complets : UN delta qui la porte entière, puis `done`.
                # Codex ne lit que le delta (aperçu du patch en cours) et
                # prend l'appel dans `output_item.done`.
                if item["input"]:
                    out += self._event(
                        "response.custom_tool_call_input.delta",
                        {**at, "delta": item["input"]})
                out += self._event("response.custom_tool_call_input.done", {
                    **at, "input": item["input"]})
            elif form == "local_shell":
                out += self._event("response.output_item.added", {
                    "output_index": len(self._output),
                    "item": {**item, "status": "in_progress"}})
            else:
                out += self._event("response.function_call_arguments.done", {
                    **at, "name": name, "arguments": item["arguments"]})
            if self._tool_index is not None:
                self._closed_tools.add(self._tool_index)
            self._tool_index = None
            self.client_calls += 1
        out += self._event("response.output_item.done", {
            "output_index": len(self._output), "item": item})
        self._output.append(item)
        self._open = None
        return bytes(out)

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
            return self._fail(error_body(doc, 500)["error"])
        out = bytearray(self._start(doc))
        if isinstance(doc.get("usage"), dict):
            self.usage = doc["usage"]
        for choice in doc.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or {}
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if REASONING_AS_SUMMARY and isinstance(reasoning, str) and reasoning:
                if self._open != "reasoning":
                    out += self._open_item(
                        "reasoning", _reasoning_item(_id("rs"), None))
                    out += self._event("response.reasoning_summary_part.added", {
                        **self._at(), "summary_index": 0,
                        "part": {"type": "summary_text", "text": ""}})
                self._text.append(reasoning)
                out += self._event("response.reasoning_summary_text.delta", {
                    **self._at(), "summary_index": 0, "delta": reasoning})
            text = delta.get("content")
            if isinstance(text, str) and text and self._open != "text":
                # Des blancs seuls avant un appel d'outil (certains gabarits
                # en émettent) ne font pas un message : retenus jusqu'au
                # premier caractère visible, jetés si rien ne suit.
                self._blank += text
                text = "" if not self._blank.strip() else self._blank
            if isinstance(text, str) and text:
                if self._open != "text":
                    self._blank = ""
                    out += self._open_item(
                        "text", _message_item(_id("msg"), "", "in_progress"))
                    out += self._event("response.content_part.added", {
                        **self._at(), "content_index": 0,
                        "part": _text_part("")})
                self._text.append(text)
                self.out_chars += len(text)
                out += self._event("response.output_text.delta", {
                    **self._at(), "content_index": 0, "delta": text,
                    "logprobs": []})
            for tc in delta.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                idx = tc.get("index", 0)
                fn = tc.get("function") or {}
                if self._open not in ("tool", "hosted") \
                        or self._tool_index != idx:
                    if idx in self._closed_tools:
                        # Fragment tardif d'un outil déjà fermé : impossible
                        # en pratique, ignoré plutôt que de casser le flux.
                        continue
                    call_id = str(tc.get("id") or _id("call"))
                    name = str(fn.get("name") or "")
                    if name in self.ctx.hosted:
                        # Outil hébergé : le client voit un élément
                        # `web_search_call` s'ouvrir, jamais ses arguments.
                        tool = self.ctx.hosted[name]
                        out += self._open_item(
                            "hosted", _hosted_item(_id("ws"), tool))
                        out += self._event(
                            f"response.{tool.responses.item}.in_progress",
                            self._at())
                    elif self.ctx.form(name) == "custom":
                        item = _client_call(self.ctx, call_id, name, "")
                        out += self._open_item(
                            "tool", {**item, "status": "in_progress"})
                    elif self.ctx.form(name) == "local_shell":
                        out += self._open_item(
                            "tool", {"id": _id("lsh")}, announce=False)
                    else:
                        out += self._open_item("tool", _call_item(
                            _id("fc"), call_id, name, "", "in_progress",
                            self.ctx))
                    self._call, self._tool_index = (call_id, name), idx
                args = fn.get("arguments")
                if isinstance(args, str) and args:
                    self._text.append(args)
                    if self._open == "tool" \
                            and self.ctx.form(self._call[1]) == "function":
                        out += self._event(
                            "response.function_call_arguments.delta",
                            {**self._at(), "delta": args})
            if choice.get("finish_reason"):
                self._finish_reason = choice["finish_reason"]
        return bytes(out)

    def _fail(self, error: dict) -> bytes:
        """Erreur en cours de flux : `response.failed`, seule façon de la
        dire une fois le 200 parti."""
        if self._finished:
            return b""
        out = bytearray(self._start({}))
        out += self._close()
        self._finished = True
        self.pending = []
        out += self._event("response.failed", {"response": self._snapshot(
            "failed", {"code": "server_error",
                       "message": str(error.get("message") or "")})})
        return bytes(out)

    def _end(self, force: bool = False) -> bytes:
        """Fin d'un flux upstream. Avec des appels hébergés en attente, ce
        n'est que la fin d'un TOUR : la réponse reste ouverte, c'est
        finalize() (`force`) qui la clora."""
        if self._finished:
            return b""
        out = bytearray(self._start({}))
        out += self._close()
        if self.pending and not force:
            return bytes(out)
        self._finished = True
        status = _final_status(self._finish_reason)
        out += self._event(f"response.{status}",
                           {"response": self._snapshot(status)})
        return bytes(out)


def _est(chars: int) -> int:
    return max(chars // CHARS_PER_TOKEN, 1) if chars else 0
