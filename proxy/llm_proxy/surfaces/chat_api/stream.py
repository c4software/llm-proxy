"""
Le FLUX : le robinet (Translator) qui rend UNE réponse chat/completions
pour plusieurs tours upstream, outils hébergés exécutés entre eux.
"""

import json
import re
import uuid

from ...core.settings import log
from ...storage import files

from .common import CHARS_PER_TOKEN, GAP, PREVIEW_CHARS, Context
from .memory import _key


# Ce qui, juste après une URL trouvée dans le texte, dit qu'elle CONTINUE :
# le modèle en a écrit une autre, plus longue. Une ponctuation de fin de
# phrase (ou un `/` final) n'en fait partie que suivie d'un caractère d'URL.
_MORE = re.compile(r"[.,;:!?)\]}'\"*>/]*[\w%~=&#+@-]")


def _id(prefix: str) -> str:
    return f"{prefix}_" + uuid.uuid4().hex[:24]


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _sum(a: dict, b: dict) -> dict:
    """Somme de deux `usage`, champ à champ, sous-objets compris
    (`prompt_tokens_details.cached_tokens`…) : chaque tour relit tout le
    préfixe, et c'est bien ce qui a été consommé."""
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict):
            out[k] = _sum(out[k] if isinstance(out.get(k), dict) else {}, v)
        elif _number(v):
            out[k] = (out[k] if _number(out.get(k)) else 0) + v
        else:
            out.setdefault(k, v)
    return out


def _sse(doc: dict) -> bytes:
    return b"data: " + json.dumps(doc, ensure_ascii=False).encode() + b"\n\n"


DONE = b"data: [DONE]\n\n"


class Translator:
    """Le robinet de réponse de /v1/chat/completions avec outils hébergés :
    même interface que responses_api.Translator, menée par
    tool_loop.hosted_loop.

    UNE réponse pour PLUSIEURS tours upstream. En flux :
      * l'`id` et le `created` du premier tour sur tous les blocs ;
      * les deltas de contenu (et de raisonnement) des tours successifs à
        la suite, GAP entre deux textes ; `role` une seule fois ;
      * les appels hébergés retirés des deltas — jamais de `tool_calls`
        pour eux ; ceux du client passent, renumérotés (voir le tour
        mixte, en tête de module) ;
      * `finish_reason`, le bloc `usage` et `[DONE]` RETENUS à chaque
        tour, émis une fois à la fin : annotations éventuelles, UN
        `finish_reason` (`stop` quand le dernier tour s'arrête sur des
        appels hébergés que la limite dure a coupés), UN bloc `usage`
        cumulé si le client a demandé `include_usage`, `[DONE]`.
    En JSON, rien ne part avant la fin : le dernier corps upstream, avec
    le contenu de tous les tours, l'usage cumulé, `annotations`.

    Un échec à un tour ultérieur (fail) : en flux, un bloc
    `{"error": …}` puis `[DONE]` — le 200 est parti ; en JSON, le corps
    d'erreur OpenAI, que la route rend avec `status`, le vrai statut."""

    def __init__(self, status: int, content_type: str, ctx: Context):
        ct = (content_type or "").lower()
        self.ok = 200 <= status < 300
        self.sse = self.ok and "text/event-stream" in ct
        self.ctx = ctx
        # Statut HTTP de l'issue, pour la route en JSON.
        self.status = 200
        self.usage: dict | None = None    # usage upstream du tour en cours
        self._past: list[dict] = []       # usages des tours précédents
        self.out_chars = 0
        # Appels hébergés du tour, à exécuter : {id, name, arguments}.
        self.pending: list[dict] = []
        # Appels CLIENT transmis pendant le tour. Toujours 0 quand
        # `pending` n'est pas vide (le premier appel du tour décide) :
        # la boucle ne rend donc jamais la main sur un tour mixte.
        self.client_calls = 0
        self.turns = 1
        self._buf = bytearray()
        self._finished = False
        self._head: dict | None = None    # id / created du premier tour
        self._tail: dict | None = None    # le bloc de fin retenu, sans choix
        self._finish: str | None = None
        self._lead: str | None = None     # "hosted" | "client"
        # Index upstream d'un appel → son appel hébergé (dict), son
        # nouvel index côté client (int), ou None s'il est écarté.
        self._slots: dict = {}
        self._text: list[str] = []        # texte du tour en cours
        self._all: list[str] = []         # tout le contenu rendu, GAP compris
        self._gap = ""
        self._done: list[tuple[dict, str]] = []   # (appel, résultat) du tour
        self._history: list[dict] = []    # messages des tours clos
        self._ids: list[str] = []         # id des appels client du tour
        self._sources: dict[str, str] = {}        # URL → titre
        # Liens des fichiers rendus par les outils, écrits à la fin.
        self._links: list[str] = []
        # JSON : dernier corps upstream, appels client et raisonnement.
        self._doc: dict = {}
        self._client: list[dict] = []
        self._reasoning: dict[str, list[str]] = {}

    # ── interface robinet ──
    def start(self, call: dict) -> bytes:
        """L'appel hébergé que la boucle va exécuter. Rien pour un client
        ordinaire (il n'a pas à savoir qu'un outil tourne) ; avec
        `ctx.progress` — le chat de la page, qui l'affiche —, un bloc
        dont le delta porte `x_hosted_tool` : un champ hors schéma, que
        seule la page demande et lit."""
        return self._progress(call, "running")

    def _progress(self, call: dict, status: str, result=None) -> bytes:
        if not (self.ctx.progress and self.sse) or self._finished:
            return b""
        event = {"id": call["id"], "name": call["name"],
                 "arguments": call["arguments"] or "{}", "status": status}
        if result is not None:
            event["chars"] = len(result.text)
            event["preview"] = result.text[:PREVIEW_CHARS]
            if result.error:
                # Un refus par limite d'appels n'est pas une panne.
                event["status"] = "limit" if result.error == "limit" \
                    else "error"
        return self._block({"x_hosted_tool": event})

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
        if self._finished:
            return b""
        if self.sse:
            self._buf.clear()
            self._dropped()
            # Appels hébergés en attente : ce n'est que la fin d'un TOUR.
            return b"" if self.pending else self._end()
        body = bytes(self._buf)
        self._buf.clear()
        try:
            doc = json.loads(body) if body else None
        except ValueError:
            doc = None
        choices = doc.get("choices") if isinstance(doc, dict) else None
        if not isinstance(choices, list) or not choices \
                or not isinstance(choices[0], dict):
            if self.turns > 1:
                return self.fail("réponse upstream illisible", 502)
            # Premier tour, forme inattendue : rendue telle quelle.
            self._finished = True
            return body
        if isinstance(doc.get("usage"), dict):
            self.usage = doc["usage"]
        if self._head is None:
            self._head = {k: doc[k] for k in ("id", "created", "model")
                          if k in doc}
        self._doc = doc
        msg = choices[0].get("message")
        msg = msg if isinstance(msg, dict) else {}
        self._content(msg.get("content"))
        for key in ("reasoning_content", "reasoning"):
            if isinstance(msg.get(key), str) and msg[key]:
                self._reasoning.setdefault(key, []).append(msg[key])
        calls = msg.get("tool_calls")
        self._client = [kept for kept in (
            self._tool({**tc, "index": i}, whole=True)
            for i, tc in enumerate(calls if isinstance(calls, list) else [])
            if isinstance(tc, dict)) if kept is not None]
        self._finish = choices[0].get("finish_reason")
        self._dropped()
        return b"" if self.pending else self.finalize()

    # ── outils hébergés : plusieurs tours upstream pour une réponse ──
    @property
    def history(self) -> list[dict]:
        """Ce que la boucle ajoute aux messages d'origine pour le tour
        suivant : par tour, le message assistant (son texte, ses appels
        HÉBERGÉS — ceux du client écartés d'un tour mixte n'y sont pas :
        le modèle les réémettra) puis un message `tool` par résultat."""
        return self._history + self._turn_messages()

    def _turn_messages(self) -> list[dict]:
        if not self._done:
            return []
        out = [{"role": "assistant", "content": "".join(self._text) or None,
                "tool_calls": [
                    {"id": call["id"], "type": "function", "function": {
                        "name": call["name"],
                        "arguments": call["arguments"] or "{}"}}
                    for call, _ in self._done]}]
        return out + [{"role": "tool", "tool_call_id": call["id"],
                       "content": result} for call, result in self._done]

    @property
    def session(self) -> str:
        """tools.Call.session"""
        return self.ctx.session

    def resolve(self, call: dict, result) -> bytes:
        """Le résultat (tools.Result) d'un appel de `pending` : texte gardé
        pour le tour suivant, sources pour les annotations, fichiers rangés
        au magasin. Le client n'en voit rien, sauf le chat de la page."""
        self.pending = [c for c in self.pending if c is not call]
        self._done.append((call, result.text))
        self._links += [stored.markdown for stored in
                        files.keep(result.files, self.ctx.client)]
        for source in result.sources:
            if source.url:
                self._sources.setdefault(source.url, source.title)
        return self._progress(call, "done", result)

    def next_turn(self) -> None:
        """Avant de recevoir le flux upstream suivant : l'état propre au
        tour repart de zéro (les index d'outils recommencent à 0),
        l'identité de la réponse reste."""
        if isinstance(self.usage, dict):
            self._past.append(self.usage)
        self.usage = None
        self._history += self._turn_messages()
        self._done, self._text = [], []
        self._buf.clear()
        self._slots, self._lead = {}, None
        self._finish, self._tail = None, None
        self._client = []
        self._ids = []
        self.client_calls = 0
        if self._all and not self._all[-1].endswith("\n"):
            self._gap = GAP
        self.turns += 1

    def finalize(self) -> bytes:
        if self._finished:
            return b""
        if self.sse:
            return self._end()
        self._finished = True
        links = self._attachments()
        self._remember()
        doc = {**self._doc, **(self._head or {})}
        choice = dict(doc["choices"][0])
        msg = choice.get("message")
        msg = dict(msg) if isinstance(msg, dict) else {"role": "assistant"}
        if self.turns > 1 or links:
            msg["content"] = "".join(self._all) or None
            for key, texts in self._reasoning.items():
                msg[key] = GAP.join(texts)
        if self._client:
            msg["tool_calls"] = self._client
        else:
            msg.pop("tool_calls", None)
        annotations = self._annotations()
        if annotations:
            msg["annotations"] = annotations
        choice["message"] = msg
        choice["finish_reason"] = self._final_reason()
        doc["choices"] = [choice]
        total = self._total()
        if total is not None:
            doc["usage"] = total
        return json.dumps(doc, ensure_ascii=False).encode()

    def fail(self, message: str, status: int = 500) -> bytes:
        """Échec d'un tour ULTÉRIEUR (quota, backend injoignable, statut
        d'erreur), ou erreur dite dans le flux par l'upstream."""
        if self._finished:
            return b""
        self._finished = True
        self.pending = []
        self.status = status
        body = {"error": {"message": message, "type": "upstream_error",
                          "code": status}}
        if self.sse:
            return _sse(body) + DONE
        return json.dumps(body, ensure_ascii=False).encode()

    def _total(self) -> dict | None:
        """L'usage de la réponse : celui de l'upstream s'il n'y a eu
        qu'un tour, la SOMME sinon."""
        turns = self._past + ([self.usage] if isinstance(self.usage, dict) else [])
        total = None
        for u in turns:
            total = u if total is None else _sum(total, u)
        return total

    def cached(self) -> int:
        details = (self._total() or {}).get("prompt_tokens_details")
        if isinstance(details, dict) and _number(details.get("cached_tokens")):
            return max(int(details["cached_tokens"]), 0)
        return 0

    def tokens(self, fallback_prompt: int) -> tuple[int, int, bool]:
        u = self._total() or {}
        p, c = u.get("prompt_tokens"), u.get("completion_tokens")
        if isinstance(p, int) or isinstance(c, int):
            return (p if isinstance(p, int) else fallback_prompt,
                    c if isinstance(c, int) else _est(self.out_chars), True)
        return fallback_prompt, _est(self.out_chars), False

    # ── commun aux deux modes ──
    def _content(self, text) -> str:
        """Un morceau de contenu du tour : noté, et rendu tel que le
        client doit le lire (GAP devant le premier d'un tour suivant)."""
        if not isinstance(text, str) or not text:
            return ""
        self._text.append(text)
        text, self._gap = self._gap + text, ""
        self._all.append(text)
        self.out_chars += len(text)
        return text

    def _tool(self, tc: dict, whole: bool = False) -> dict | None:
        """Un appel (ou un fragment d'appel, en flux) : rendu s'il va au
        client, None s'il est hébergé — rangé dans `pending` — ou écarté.
        Le premier appel du tour décide de qui l'emporte (tête de module)."""
        idx = tc.get("index", 0)
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        if idx not in self._slots:
            name = str(fn.get("name") or "")
            slot = None
            if name in self.ctx.hosted:
                if self._lead != "client":
                    self._lead = "hosted"
                    slot = {"id": str(tc.get("id") or _id("call")),
                            "name": name, "arguments": ""}
                    self.pending.append(slot)
            elif self._lead != "hosted":
                self._lead = "client"
                slot = self.client_calls
                self.client_calls += 1
                self._ids.append(str(tc.get("id") or ""))
            self._slots[idx] = slot
        slot = self._slots[idx]
        if slot is None:
            return None
        if isinstance(slot, dict):
            if isinstance(fn.get("arguments"), str):
                slot["arguments"] += fn["arguments"]
            return None
        if whole:       # JSON : un appel entier n'a pas d'`index`
            return {k: v for k, v in tc.items() if k != "index"}
        tc["index"] = slot
        return tc

    def _dropped(self) -> None:
        dropped = sum(1 for slot in self._slots.values() if slot is None)
        if dropped:
            log.info(
                "chat/completions : tour mixte, %d appel(s) %s écarté(s) — "
                "le modèle les réémettra", dropped,
                "du client" if self._lead == "hosted" else "hébergé(s)")

    def _final_reason(self) -> str:
        # `tool_calls` sans appel pour le client (appels hébergés que la
        # limite dure a coupés) : il attendrait des appels qui n'existent pas.
        if self._finish == "tool_calls" and not self.client_calls:
            return "stop"
        return self._finish or "stop"

    def _attachments(self) -> str:
        """Les liens des fichiers rendus, à ajouter au contenu — une
        fois, à la clôture. Ils entrent dans `_all`, donc dans le condensé
        de la mémoire (le client renverra la réponse AVEC eux) ; pas dans
        `_text` : au rejeu le backend retrouve le texte qu'il a écrit,
        sans eux (`tail`)."""
        if not self._links:
            return ""
        whole = "".join(self._all)
        text = ("" if not whole or whole.endswith("\n\n")
                else "\n" if whole.endswith("\n") else GAP) \
            + GAP.join(self._links)
        self._links = []
        self._all.append(text)
        self.out_chars += len(text)
        return text

    def _remember(self) -> None:
        """Range l'échange caché de cette réponse (tête de module), à sa
        CONCLUSION : au moins un tour d'outils hébergés, puis un tour sans
        eux. Pas après fail(), ni quand la limite dure a clos la réponse
        (`_done` non vide : des résultats que le modèle n'a pas lus), ni
        pour un dernier tour vide (ni texte, ni appel client)."""
        ctx = self.ctx
        tail = "".join(self._text)
        if ctx.memory is None or ctx.state is None or not self._history \
                or self._done or not (tail or self._ids):
            return
        ctx.memory.store(_key(ctx.state, "".join(self._all), self._ids),
                         list(self._history), tail, ctx.client, ctx.session)

    def _annotations(self) -> list[dict]:
        """Annotations `url_citation` : une par OCCURRENCE, dans le contenu
        rendu (tous les tours, GAP compris), d'une URL rendue par un outil.
        Une source que le modèle n'a pas écrite n'est pas citée.

        Une URL n'est citée qu'écrite EN ENTIER (`…/llama.cpp` ne doit pas
        matcher au début de `…/llama.cpp/releases`) : les plus longues
        d'abord, une occurrence ne servant qu'une fois, et une occurrence
        qui continue (`_MORE`) est une autre URL."""
        if not self.ctx.annotations or not self._sources:
            return []
        text = "".join(self._all)
        spans: list[tuple[int, int, str]] = []
        for url in sorted(self._sources, key=len, reverse=True):
            at = text.find(url)
            while at >= 0:
                end = at + len(url)
                if not _MORE.match(text, end) \
                        and not any(s < end and at < e for s, e, _ in spans):
                    spans.append((at, end, url))
                at = text.find(url, end)
        return [{"type": "url_citation", "url_citation": {
            "start_index": at, "end_index": end, "url": url,
            "title": self._sources[url]}} for at, end, url in sorted(spans)]

    # ── flux ──
    def _block(self, delta: dict, finish: str | None = None) -> bytes:
        """Un bloc du proxy lui-même, à l'identité de la réponse."""
        base = self._tail or {"object": "chat.completion.chunk",
                              **(self._head or {"id": _id("chatcmpl")})}
        return _sse({**base, "choices": [
            {"index": 0, "delta": delta, "finish_reason": finish}]})

    def _data(self, payload: bytes) -> bytes:
        if self._finished or payload == b"[DONE]":
            return b""      # la fin se décide dans finish()
        try:
            doc = json.loads(payload)
        except ValueError:
            return b""
        if not isinstance(doc, dict):
            return b""
        if "error" in doc and "choices" not in doc:
            err = doc["error"]
            return self.fail(str(err.get("message") if isinstance(err, dict)
                                 else err) or "erreur upstream")
        if isinstance(doc.get("usage"), dict):
            self.usage = doc["usage"]
        if "usage" in doc:
            # Retenu : un seul bloc `usage`, cumulé, à la fin. OpenAI pose
            # `usage: null` sur les autres blocs quand il est demandé.
            if self.ctx.include_usage:
                doc["usage"] = None
            else:
                del doc["usage"]
        if self._head is None:
            self._head = {k: doc[k] for k in ("id", "created", "model")
                          if k in doc}
        doc.update(self._head)
        choices = doc.get("choices")
        keep = False
        for choice in choices if isinstance(choices, list) else []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                delta = choice["delta"] = {}
            if self.turns > 1:
                delta.pop("role", None)
            text = self._content(delta.get("content"))
            if text:
                delta["content"] = text
            if isinstance(delta.get("tool_calls"), list):
                calls = [kept for kept in (
                    self._tool(tc) for tc in delta["tool_calls"]
                    if isinstance(tc, dict)) if kept is not None]
                if calls:
                    delta["tool_calls"] = calls
                else:
                    del delta["tool_calls"]
            if choice.get("finish_reason"):
                # Retenu avec son bloc (un backend y joint parfois des
                # mesures, `timings` chez llama.cpp) : il clora la réponse
                # si ce tour est le dernier.
                self._finish = choice["finish_reason"]
                choice["finish_reason"] = None
                self._tail = {k: v for k, v in doc.items() if k != "choices"}
            keep = keep or any(delta.values())
        return _sse(doc) if keep else b""

    def _end(self) -> bytes:
        self._finished = True
        # AVANT la mémoire : son condensé porte la réponse telle que le
        # client la reçoit, liens compris.
        links = self._attachments()
        self._remember()
        out = bytearray()
        if links:
            out += self._block({"content": links})
        annotations = self._annotations()
        if annotations:
            out += self._block({"annotations": annotations})
        out += self._block({}, self._final_reason())
        total = self._total()
        if self.ctx.include_usage and total is not None:
            out += _sse({"object": "chat.completion.chunk",
                         **(self._head or {}), "choices": [], "usage": total})
        return bytes(out) + DONE


def _est(chars: int) -> int:
    return max(chars // CHARS_PER_TOKEN, 1) if chars else 0
