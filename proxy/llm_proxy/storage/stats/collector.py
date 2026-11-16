"""
La LECTURE de l'usage dans un flux relayé : le robinet UsageCollector
(tokens d'entrée, de sortie, en cache), et l'estimation quand le backend
ne les dit pas.
"""

import json
import re


# Réponse non streamée : seules la TÊTE et la QUEUE du corps sont
# retenues, TAIL_BYTES chacune — `usage` vit à l'une ou l'autre, jamais au
# milieu d'une réponse volumineuse.
TAIL_BYTES = 64 << 10


# Flux SSE : une ligne sans retour chariot ne peut pas dépasser ceci —
# au-delà ce n'est pas du SSE, on cesse de l'inspecter.
MAX_LINE_BYTES = 1 << 20


# Même approximation que le limiteur (albert.CHARS_PER_TOKEN).
CHARS_PER_TOKEN = 4


def _est(chars: int) -> int:
    return max(chars // CHARS_PER_TOKEN, 1) if chars else 0


# Ce qu'on cherche dans le flux, SANS le parser : json.loads sur chaque
# événement SSE pèse sur la boucle d'événements à chaque token.
#   * _CONTENT_RE : la valeur de `content` quand c'est une chaîne — on
#     n'en retient que la longueur. `"content":null` ne matche pas, ni
#     `"reasoning_content"` (le guillemet ouvrant est exigé), ni un
#     `\"content\"` échappé dans un texte ;
#   * _USAGE_RE : `"usage":{` — l'OBJET, pas le `"usage":null` qu'OpenAI
#     répète sur chaque chunk quand include_usage est demandé.
_CONTENT_RE = re.compile(rb'"content"\s*:\s*"((?:[^"\\]|\\.)*)"')


_USAGE_RE = re.compile(rb'"usage"\s*:\s*\{')


_DECODER = json.JSONDecoder()


def _chars(raw: bytes) -> int:
    """Longueur en CARACTÈRES d'une chaîne JSON encore échappée, sans la
    désérialiser : octets décodés, moins un par échappement («\\n»,
    «\\"»…). Un «\\uXXXX» reste compté 5 — rare, l'upstream envoie de
    l'UTF-8 brut — et c'est une estimation à 4 caractères par token."""
    text = raw.decode("utf-8", "ignore")
    return len(text) - text.count("\\")


def _usage_at(buf: bytes, pos: int) -> dict | None:
    """Décode l'objet JSON ouvert en `pos` (un `{`) ; None s'il n'est pas
    un `usage` plausible. raw_decode s'arrête à l'accolade fermante : le
    reste du corps n'est jamais parsé."""
    try:
        obj, _ = _DECODER.raw_decode(
            buf[pos:pos + TAIL_BYTES].decode(errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return None
    if isinstance(obj, dict) and (
        isinstance(obj.get("prompt_tokens"), int)
        or isinstance(obj.get("completion_tokens"), int)
        or isinstance(obj.get("total_tokens"), int)
    ):
        return obj
    return None


class UsageCollector:
    """Lit les tokens dans le flux de réponse upstream, sans le retenir et
    SANS le désérialiser — le relais doit rester le plus léger possible.

    - réponse JSON (non streamée) : seules la tête et la queue du corps
      (TAIL_BYTES chacune) sont conservées ; à la fin, on y cherche
      `"usage":{` et on ne décode que cet objet-là ;
    - flux SSE : sur chaque lot de lignes complètes, une regex accumule
      la longueur des deltas de contenu (filet de sécurité quand
      l'upstream n'envoie aucun `usage`) et une autre repère l'éventuel
      objet `usage`, seul morceau décodé.
    """

    def __init__(self, content_type: str):
        ct = (content_type or "").lower()
        self.sse = "text/event-stream" in ct
        self.json = not self.sse and "json" in ct
        self._buf = bytearray()   # SSE : ligne en cours ; JSON : queue
        self._head = bytearray()  # JSON : tête du corps
        self.usage: dict | None = None
        self.out_chars = 0

    def feed(self, chunk: bytes) -> bytes:
        """Inspecte `chunk` et le rend TEL QUEL : ce robinet est
        l'identité. (Même interface qu'anthropic_api.Translator, qui
        lui réécrit — relaying.forward ne distingue pas les deux.)"""
        try:
            if self.sse:
                self._feed_sse(chunk)
            elif self.json:
                if len(self._head) < TAIL_BYTES:
                    self._head += chunk[:TAIL_BYTES - len(self._head)]
                self._buf += chunk
                # Amorti : on ne tronque qu'une fois sur deux tailles.
                if len(self._buf) > 2 * TAIL_BYTES:
                    del self._buf[:-TAIL_BYTES]
        except Exception:
            # Une réponse au schéma inattendu ne doit jamais casser le relais.
            self.sse = self.json = False
            self._buf.clear()
            self._head.clear()
        return chunk

    def _feed_sse(self, chunk: bytes) -> None:
        # Cas courant (un flush par événement, tampon vide) : le chunk
        # est inspecté tel quel, sans passer par le tampon.
        if not self._buf and chunk.endswith(b"\n"):
            done = chunk
        else:
            self._buf += chunk
            nl = self._buf.rfind(b"\n")
            if nl < 0:
                if len(self._buf) > MAX_LINE_BYTES:
                    self.sse = False
                    self._buf.clear()
                return
            done = bytes(self._buf[:nl + 1])
            del self._buf[:nl + 1]
        for m in _CONTENT_RE.finditer(done):
            self.out_chars += _chars(m.group(1))
        for m in _USAGE_RE.finditer(done):
            usage = _usage_at(done, m.end() - 1)
            if usage is not None:
                self.usage = usage

    def finish(self) -> bytes:
        """Fin du flux : sur une réponse JSON, cherche `usage` dans la
        queue puis la tête conservées — dernière occurrence d'abord,
        c'est celle du niveau racine. Rien à émettre : tout est déjà
        parti par feed()."""
        if self.json:
            for part in (self._buf, self._head):
                if self.usage is not None:
                    break
                buf = bytes(part)
                for m in reversed(list(_USAGE_RE.finditer(buf))):
                    usage = _usage_at(buf, m.end() - 1)
                    if usage is not None:
                        self.usage = usage
                        break
        self._buf.clear()
        self._head.clear()
        return b""

    def tokens(self, fallback_prompt: int) -> tuple[int, int, bool]:
        """(prompt_tokens, completion_tokens, exact)."""
        u = self.usage or {}
        p = u.get("prompt_tokens")
        c = u.get("completion_tokens")
        if isinstance(p, int) or isinstance(c, int):
            return (p if isinstance(p, int) else fallback_prompt,
                    c if isinstance(c, int) else _est(self.out_chars),
                    True)
        return fallback_prompt, _est(self.out_chars), False

    def cached(self) -> int:
        return cached_tokens(self.usage)


def cached_tokens(usage) -> int:
    """Tokens d'entrée servis depuis le cache de préfixe du backend :
    `prompt_tokens_details.cached_tokens` (OpenAI, llama.cpp, vLLM…).
    0 si l'upstream ne le dit pas — jamais une valeur inventée."""
    if not isinstance(usage, dict):
        return 0
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict) and isinstance(details.get("cached_tokens"), int):
        return max(details["cached_tokens"], 0)
    return 0
