"""
Une borne d'événements par clé sur une FENÊTRE GLISSANTE : les appels
directs d'outils par client (exposure/tool_routes.py), les messages d'un
webhook entrant (ui/webhook_routes.py), les pull requests d'un compte
(tools/pull_request.py). La borne est passée à chaque appel : elle reste un
réglage du module qui l'emploie.
"""

import collections
import time

# Au-delà, les clés sans événement récent sont oubliées.
MAX_KEYS = 4096


class Window:
    def __init__(self, seconds: float):
        self.seconds = seconds
        self._events: dict[str, collections.deque] = {}

    def wait(self, key: str, limit: int, n: int = 1, now: float | None = None) -> float:
        """Réserve `n` événements pour `key`, `limit` au plus par fenêtre
        (0 ou moins : sans borne). 0 : acceptés (et comptés) ; sinon les
        secondes avant qu'une place se libère (rien n'est compté)."""
        if limit <= 0 or n <= 0:
            return 0.0
        now = time.monotonic() if now is None else now
        q = self._events.setdefault(key, collections.deque())
        while q and q[0] <= now - self.seconds:
            q.popleft()
        if len(q) + n > limit:
            if n > limit or not q:
                return self.seconds
            return max(q[len(q) + n - limit - 1] + self.seconds - now, 0.001)
        q.extend([now] * n)
        if len(self._events) > MAX_KEYS:
            for k in [k for k, v in self._events.items()
                      if not v or v[-1] <= now - self.seconds]:
                del self._events[k]
        return 0.0

    def forget(self, key: str) -> None:
        self._events.pop(key, None)

    def clear(self) -> None:
        self._events.clear()
