"""
La MÉMOIRE des échanges cachés : les tours d'outils hébergés que le
client ne voit pas, rangés pour être réinsérés quand il renvoie la
conversation telle qu'il l'a reçue.
"""

import hashlib
import json
import time
from collections import OrderedDict

from .common import MEMORY_CHARS, MEMORY_ENABLED, MEMORY_ENTRIES, MEMORY_TTL, Context


# ── mémoire des échanges cachés ─────────────────────────────────────────

class Memory:
    """Les échanges cachés, par client et condensé (tête de module) : la
    LRU de tools.Memory plus une borne en caractères, toutes entrées
    confondues. Un échange plus gros que la borne n'est pas rangé."""

    def __init__(self, entries: int, ttl: float, chars: int):
        self.entries, self.ttl, self.chars = entries, ttl, chars
        self.size = 0       # caractères gardés
        self._data: OrderedDict[tuple[str, str], tuple[float, int, dict]] = \
            OrderedDict()

    def store(self, key: str, messages: list[dict], tail: str,
              owner: str = "", session: str = "") -> None:
        """`messages` : l'échange, tel qu'envoyé au backend ; `tail` : le
        texte du dernier tour, celui que le backend a répondu ; `session` :
        l'identifiant de conversation sous lequel les outils ont tourné."""
        size = len(tail) + sum(_weight(m) for m in messages)
        self._drop((owner, key))
        if size > self.chars:
            return
        self._data[(owner, key)] = (time.monotonic(), size, {
            "messages": messages, "tail": tail, "session": session})
        self.size += size
        while len(self._data) > self.entries or self.size > self.chars:
            self._drop(next(iter(self._data)))

    def recall(self, key: str, owner: str = "") -> dict | None:
        entry = self._data.get((owner, key))
        if entry is None:
            return None
        if time.monotonic() - entry[0] > self.ttl:
            self._drop((owner, key))
            return None
        self._data.move_to_end((owner, key))
        return entry[2]

    def _drop(self, key) -> None:
        entry = self._data.pop(key, None)
        if entry is not None:
            self.size -= entry[1]

    def __len__(self) -> int:
        return len(self._data)


def _weight(msg: dict) -> int:
    """Caractères d'un message de l'échange : son contenu, les arguments
    de ses appels."""
    return len(msg.get("content") or "") + sum(
        len(tc["function"]["arguments"]) for tc in msg.get("tool_calls", ()))


MEMORY = Memory(MEMORY_ENTRIES, MEMORY_TTL, MEMORY_CHARS)


def _plain(content) -> str | None:
    """Le texte d'un contenu : une chaîne, ou une liste de parties `text`
    (les deux formes de l'API). None = autre chose que du texte."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list) and all(
            isinstance(p, dict) and p.get("type") == "text"
            and isinstance(p.get("text"), str) for p in content):
        return "".join(p["text"] for p in content)
    return None


def _call_ids(msg: dict) -> list[str]:
    calls = msg.get("tool_calls")
    return [str(tc.get("id") or "") for tc in calls if isinstance(tc, dict)] \
        if isinstance(calls, list) else []


def _bytes(doc) -> bytes:
    return json.dumps(doc, sort_keys=True, ensure_ascii=False,
                      default=str).encode("utf-8", "surrogatepass")


def _canon(msg) -> bytes:
    """Ce qu'un message apporte au condensé (tête de module). Un contenu
    qui n'est pas que du texte (image d'un message user) y entre tel
    quel : seul son condensé est gardé."""
    if isinstance(msg, dict):
        text = _plain(msg.get("content"))
        msg = [msg.get("role"),
               text.strip() if text is not None else msg.get("content"),
               _call_ids(msg), msg.get("tool_call_id")]
    return _bytes(msg)


def _key(state, text: str, ids: list[str]) -> str:
    """La clé d'un échange : `state`, le condensé des messages d'avant,
    prolongé de la réponse — son texte, les identifiants de ses appels
    CLIENT (une réponse régénérée sans texte, aux appels différents, n'est
    pas la même réponse)."""
    h = state.copy()
    h.update(b"=" + _bytes([text.strip(), ids]))
    return h.hexdigest()


def restore(payload: dict, ctx: Context, client: str) -> int:
    """Réinsère, DANS `payload`, les échanges cachés reconnus (tête de
    module) et pose sur `ctx` de quoi ranger celui de cette réponse. Rend
    le nombre d'échanges réinsérés. `client` : tools.owner() de la clé,
    «» pour un proxy ouvert."""
    messages = payload.get("messages")
    if not MEMORY_ENABLED or not isinstance(messages, list):
        return 0
    state = hashlib.blake2b(digest_size=16)
    out: list = []
    found = 0
    session = ""
    for msg in messages:
        role = msg.get("role") if isinstance(msg, dict) else None
        sent = msg
        if role == "assistant":
            text = _plain(msg.get("content"))
            entry = MEMORY.recall(_key(state, text, _call_ids(msg)), client) \
                if text is not None else None
            if entry is not None:
                found += 1
                out += entry["messages"]
                ctx.restored += sum(_weight(m) for m in entry["messages"])
                # Le DERNIER échange reconnu dit la session : c'est le
                # plus proche de la suite que le client demande.
                session = entry.get("session") or session
                # Le texte du dernier tour, pas celui de tous les tours
                # que le client a reçu ; ses autres champs sont les siens.
                sent = {**msg, "content": entry["tail"] or None}
        out.append(sent)
        if role not in ("system", "developer"):
            block = _canon(msg)
            state.update(b"%d:" % len(block) + block)
    payload["messages"] = out
    ctx.memory, ctx.client, ctx.state = MEMORY, client, state
    if session:
        ctx.session = session
    return found
