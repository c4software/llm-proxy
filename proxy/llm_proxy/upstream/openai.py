"""
Les réponses chat/completions d'un backend `api = "openai"`, ramenées à la
forme que lisent les clients et les traducteurs du proxy : un `content` en
LISTE de morceaux (Mistral : {"type": "text", "text"}, {"type": "thinking",
"thinking": [morceaux texte]}) devient une chaîne, et la réflexion passe en
`reasoning_content`, comme chez llama.cpp. Le reste passe octet pour octet.

Et l'enveloppe des adaptateurs de réponse (Adapted) : ce que les appelants
de relaying.send_upstream lisent (status_code, headers, aiter_raw, aread,
aclose), la réponse amont transformée en route ; upstream/anthropic.py
l'emploie aussi.
"""

import json
import re

from ..core.settings import log
from ..storage.stats.collector import MAX_LINE_BYTES

# Un `content` en liste : seules ces lignes (ou ce corps) sont relues.
_LIST = re.compile(rb'"content"\s*:\s*\[')


class Adapted:
    """La réponse amont `raw`, lue à travers `_feed` (morceaux d'un flux
    SSE, `_tail` à la fin) ou `_whole` (corps entier)."""

    def __init__(self, raw, sse: bool, headers=None):
        self._raw = raw
        self.status_code = raw.status_code
        self._sse = sse
        self.headers = raw.headers if headers is None else headers

    def _feed(self, chunk: bytes) -> bytes:
        return chunk

    def _tail(self) -> bytes:
        return b""

    def _whole(self, raw: bytes) -> bytes:
        return raw

    async def aiter_raw(self):
        if not self._sse:
            yield await self.aread()
            return
        async for chunk in self._raw.aiter_raw():
            out = self._feed(chunk)
            if out:
                yield out
        tail = self._tail()
        if tail:
            yield tail

    async def aread(self) -> bytes:
        return self._whole(await self._raw.aread())

    async def aclose(self) -> None:
        await self._raw.aclose()


def _thought(value) -> str:
    """Le texte d'un morceau `thinking` : une chaîne, ou ses morceaux texte."""
    if isinstance(value, str):
        return value
    return "".join(str(part.get("text") or "") for part in
                   (value if isinstance(value, list) else ())
                   if isinstance(part, dict) and part.get("type") == "text")


def normalize(doc, key: str) -> bool:
    """Les `choices[].<key>.content` en liste de `doc` (key : « delta » en
    flux, « message » sinon) ramenés à une chaîne, la réflexion en
    `reasoning_content`. Rend vrai si `doc` a changé. Un morceau d'un autre
    type (référence…) est ignoré."""
    changed = False
    choices = doc.get("choices") if isinstance(doc, dict) else None
    for choice in choices if isinstance(choices, list) else ():
        msg = choice.get(key) if isinstance(choice, dict) else None
        if not isinstance(msg, dict) or not isinstance(msg.get("content"), list):
            continue
        text, thought = [], []
        for part in msg["content"]:
            kind = part.get("type") if isinstance(part, dict) else None
            if kind == "text":
                text.append(str(part.get("text") or ""))
            elif kind == "thinking":
                thought.append(_thought(part.get("thinking")))
            else:
                log.debug("morceau de contenu ignoré : %r", kind)
        changed = True
        if "".join(thought):
            msg["reasoning_content"] = str(msg.get("reasoning_content") or "") + "".join(thought)
        if text:
            msg["content"] = "".join(text)
        elif key == "delta":
            del msg["content"]
        else:
            msg["content"] = None
    return changed


def _line(line: bytes) -> bytes:
    """Une ligne SSE, fin de ligne comprise : réécrite si c'est un `data:`
    dont un `content` est une liste, sinon rendue telle quelle."""
    if not line.startswith(b"data:") or not _LIST.search(line):
        return line
    body = line.rstrip(b"\r\n")
    try:
        doc = json.loads(body[5:])
    except ValueError:
        return line
    if not normalize(doc, "delta"):
        return line
    # ASCII échappé : un texte coupé au milieu d'un emoji (substitut isolé)
    # ne s'encode pas autrement.
    return b"data: " + json.dumps(doc).encode() + line[len(body):]


class Upstream(Adapted):
    """Une réponse chat/completions réussie d'un backend OpenAI, `content` en
    liste ramené à une chaîne (normalize), en flux comme en JSON."""

    def __init__(self, raw):
        super().__init__(raw, "text/event-stream" in raw.headers.get("content-type", ""))
        self._buf = b""

    def _feed(self, chunk: bytes) -> bytes:
        if not self._buf and chunk.endswith(b"\n") and not _LIST.search(chunk):
            return chunk
        self._buf += chunk
        end = self._buf.rfind(b"\n") + 1
        if not end:
            # Une ligne démesurée passe telle quelle (borne du collecteur).
            if len(self._buf) > MAX_LINE_BYTES:
                out, self._buf = self._buf, b""
                return out
            return b""
        lines, self._buf = self._buf[:end], self._buf[end:]
        if not _LIST.search(lines):
            return lines
        return b"".join(_line(line) for line in lines.splitlines(keepends=True))

    def _tail(self) -> bytes:
        """La dernière ligne, sans fin de ligne."""
        out, self._buf = self._buf, b""
        return _line(out)

    def _whole(self, raw: bytes) -> bytes:
        if not _LIST.search(raw):
            return raw
        try:
            doc = json.loads(raw)
        except ValueError:
            return raw
        return json.dumps(doc).encode() if normalize(doc, "message") else raw
