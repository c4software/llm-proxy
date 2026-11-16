"""
La LECTURE de l'usage dans un flux relayé : le robinet UsageCollector
(tokens d'entrée, de sortie, en cache), et l'estimation quand le backend
ne les dit pas.
"""

import json
import re

from ...upstream import pricing


# Réponse non streamée : seules la TÊTE et la QUEUE du corps sont
# retenues, TAIL_BYTES chacune — `usage` vit à l'une ou l'autre, jamais au
# milieu d'une réponse volumineuse.
TAIL_BYTES = 64 << 10


# Flux SSE : une ligne sans retour chariot ne peut pas dépasser ceci —
# au-delà ce n'est pas du SSE, on cesse de l'inspecter.
MAX_LINE_BYTES = 1 << 20


# Réponse JSON retenue entière pour y ajouter son prix : au-delà, rendue
# telle quelle, sans `usage.cost`.
MAX_JSON_BYTES = 16 << 20


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

    Avec un tarif (`rate`, posé par le relais quand le modèle en a un), le
    robinet AJOUTE `usage.cost` (le prix, en dollars, forme d'OpenRouter) :
    en SSE, il rend alors les lignes complètes et réécrit le seul bloc qui
    porte `usage` ; une réponse JSON est retenue entière (MAX_JSON_BYTES,
    au-delà rendue telle quelle) et rendue à la fin, complétée.
    """

    def __init__(self, content_type: str):
        ct = (content_type or "").lower()
        self.sse = "text/event-stream" in ct
        self.json = not self.sse and "json" in ct
        self._buf = bytearray()   # SSE : ligne en cours ; JSON : queue
        self._head = bytearray()  # JSON : tête du corps
        self.usage: dict | None = None
        self.out_chars = 0
        self.rate: dict | None = None
        self._out = bytearray()   # avec un tarif : ce qui reste à rendre

    def feed(self, chunk: bytes) -> bytes:
        """Inspecte `chunk` et le rend TEL QUEL : ce robinet est
        l'identité — sauf avec un tarif (`usage.cost` ajouté). (Même
        interface qu'anthropic_api.Translator, qui lui réécrit —
        relaying.forward ne distingue pas les deux.)"""
        if self.rate is not None and (self.sse or self.json):
            return self._feed_priced(chunk)
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

    def _feed_priced(self, chunk: bytes) -> bytes:
        try:
            self._out += chunk
            if self.json:
                if len(self._out) > MAX_JSON_BYTES:    # trop gros : tel quel
                    self.rate = None
                    out, self._out = bytes(self._out), bytearray()
                    self._head += out[:TAIL_BYTES]
                    self._buf += out[-TAIL_BYTES:]
                    return out
                return b""
            nl = self._out.rfind(b"\n")
            if nl < 0:
                if len(self._out) > MAX_LINE_BYTES:      # pas du SSE
                    self.rate, self.sse = None, False
                    out, self._out = bytes(self._out), bytearray()
                    return out
                return b""
            done = bytes(self._out[:nl + 1])
            del self._out[:nl + 1]
            self._feed_sse(done)
            if b'"usage"' not in done:
                return done
            return b"\n".join(self._priced_line(line) for line in done.split(b"\n"))
        except Exception:
            # Un flux au schéma inattendu ne casse jamais le relais.
            self.rate = None
            out, self._out = bytes(self._out), bytearray()
            return out

    def _priced_line(self, line: bytes) -> bytes:
        if not line.startswith(b"data:") or b'"usage"' not in line:
            return line
        try:
            doc = json.loads(line[5:])
        except ValueError:
            return line
        if not self._price(doc):
            return line
        return b"data: " + json.dumps(doc, ensure_ascii=False).encode()

    def _price(self, doc) -> bool:
        """Ajoute `cost` à `doc["usage"]` ; True s'il l'a fait."""
        u = doc.get("usage") if isinstance(doc, dict) else None
        cost = pricing.usage_cost(self.rate, [u]) if isinstance(u, dict) else None
        if cost is None:
            return False
        u["cost"] = cost
        return True

    def finish(self) -> bytes:
        """Fin du flux : sur une réponse JSON, cherche `usage` dans la
        queue puis la tête conservées — dernière occurrence d'abord,
        c'est celle du niveau racine. Rien à émettre : tout est déjà
        parti par feed() — sauf, avec un tarif, ce qui a été retenu."""
        if self.rate is not None and self._out:
            out, self._out = bytes(self._out), bytearray()
            if self.json:
                try:
                    doc = json.loads(out)
                except ValueError:
                    doc = None
                if isinstance(doc, dict):
                    if isinstance(doc.get("usage"), dict):
                        self.usage = dict(doc["usage"])
                    if self._price(doc):
                        out = json.dumps(doc, ensure_ascii=False).encode()
                return out
            self._feed_sse(out)
            return out
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

    def usages(self) -> list[dict]:
        """L'usage de l'upstream (un seul aller-retour), s'il en a rendu un."""
        return [self.usage] if isinstance(self.usage, dict) else []

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
