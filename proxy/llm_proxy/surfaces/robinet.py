"""
Ce que les trois robinets de traduction (chat_api, responses_api,
anthropic_api : leur `Translator`) ont en commun : la découpe du flux SSE
upstream en lignes `data:`, et la comptabilité des tours — l'usage du
dernier tour (rendu au client), la somme des tours (comptée), les tokens
de la ligne de stats. Chaque robinet garde sa forme de sortie (`_data`)
et peut redéfinir `_total` quand son usage cumulé a une forme propre.

Attendu de la sous-classe : `sse`, `_buf` (bytearray), `usage` (l'usage du
tour en cours), `_past` (ceux des tours faits), `out_chars`, `_finished`
(la réponse au client est close), `_data()`.
"""

# Même approximation que le limiteur (quotas.CHARS_PER_TOKEN).
CHARS_PER_TOKEN = 4


def estimate(chars: int) -> int:
    """Tokens estimés d'un texte de `chars` caractères."""
    return max(chars // CHARS_PER_TOKEN, 1) if chars else 0


def number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def add_usage(a: dict, b: dict) -> dict:
    """Somme de deux `usage`, champ à champ, sous-objets compris
    (`prompt_tokens_details.cached_tokens`…) : chaque tour relit tout le
    préfixe, et c'est bien ce qui a été consommé."""
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict):
            out[k] = add_usage(out[k] if isinstance(out.get(k), dict) else {}, v)
        elif number(v):
            out[k] = (out[k] if number(out.get(k)) else 0) + v
        else:
            out.setdefault(k, v)
    return out


class Robinet:
    # Les sites connus de la conversation (provenance), que la boucle pose
    # et tient à jour ; le robinet du chat de la page les rend à la page.
    known: set | None = None

    @property
    def complete(self) -> bool:
        """La réponse au client est close (fin normale ou erreur dans le
        flux) : un client qui raccroche ensuite n'est pas compté 499."""
        return self._finished

    def feed(self, chunk: bytes) -> bytes:
        """Les octets upstream ; en flux, chaque ligne `data:` complète
        passe par `_data`. Hors flux, le corps s'accumule pour finish()."""
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

    def _turns(self) -> list[dict]:
        return self._past + ([self.usage] if isinstance(self.usage, dict) else [])

    def _last(self) -> dict | None:
        """L'usage rendu au CLIENT, à la forme chat/completions : celui du
        dernier tour upstream. Son prompt relit tous les tours précédents :
        c'est le contexte occupé, ce que mesure la jauge d'un client (la
        somme la ferait déborder)."""
        turns = self._turns()
        return turns[-1] if turns else None

    def _total(self) -> dict | None:
        """L'usage CONSOMMÉ (quotas, statistiques, logs), à la forme
        chat/completions : celui de l'upstream s'il n'y a eu qu'un tour, la
        SOMME sinon — chaque tour relit tout le préfixe."""
        total = None
        for u in self._turns():
            total = u if total is None else add_usage(total, u)
        return total

    def tokens(self, fallback_prompt: int) -> tuple[int, int, bool]:
        """(entrée, sortie, exact ?) pour la ligne de stats : l'usage
        upstream s'il en a dit quelque chose, sinon des estimations."""
        u = self._total() or {}
        p, c = u.get("prompt_tokens"), u.get("completion_tokens")
        if isinstance(p, int) or isinstance(c, int):
            return (p if isinstance(p, int) else fallback_prompt,
                    c if isinstance(c, int) else estimate(self.out_chars), True)
        return fallback_prompt, estimate(self.out_chars), False
