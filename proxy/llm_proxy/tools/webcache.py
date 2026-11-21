"""
Cache web des outils hébergés : ce qu'une lecture de page ou une recherche
a rapporté, gardé quelques minutes, pour ne pas le redemander.

À ne pas confondre avec les MÉMOIRES de conversation (tools.Memory,
chat_api.Memory), qui rendent au modèle ce qu'il a déjà lu dans UNE
conversation : ici c'est le web lui-même qui est épargné, quel que soit le
client.

Deux raisons :
  * une page longue est lue en plusieurs appels (`offset`) — sans cache,
    chaque morceau la retéléchargerait en entier ;
  * les moteurs de SearXNG bloquent vite une adresse qui les sollicite
    trop (limite de débit, CAPTCHA) : une recherche identique refaite dans
    la minute n'a pas à repartir chez eux.

Durée COURTE par défaut (`[tools].web_cache_ttl`) : au-delà, une page
d'actualité serait servie périmée. En mémoire vive, borné, perdu au
redémarrage. Seuls les succès y entrent — jamais une erreur, une liste
vide ou un moteur indisponible, qu'il faut pouvoir retenter. Commun à tous
les clients : une page publique est la même pour tous, et les garde-fous
(adresses publiques, listes de domaines) sont appliqués AVANT la lecture
du cache.
"""

import asyncio
import time
from collections import OrderedDict

from ..core import config
from ..core.settings import INCOGNITO

TTL = config.num("tools.web_cache_ttl", 600)
ENTRIES = config.integer("tools.web_cache_entries", 256)
MAX_BYTES = config.integer("tools.web_cache_bytes", 64_000_000)


# Tous les caches : la purge (sweep) les parcourt.
CACHES: list["Cache"] = []
# Une purge par minute : une entrée expirée quitte la mémoire au plus tard
# une minute après son échéance, même si personne ne la redemande.
SWEEP_EVERY = 60.0


class Cache:
    """LRU à durée de vie, bornée en entrées et en taille cumulée. `size`
    est donné par l'appelant (octets du corps, caractères des résultats).
    `sliding` : la durée de vie court depuis la DERNIÈRE lecture (le cache
    des dépôts), sinon depuis l'entrée. Une entrée expirée est retirée à
    la lecture, et par la purge (sweep) qui passe toutes les SWEEP_EVERY."""

    def __init__(self, ttl: float, entries: int, max_bytes: int, sliding: bool = False):
        self.ttl, self.entries, self.max_bytes = ttl, entries, max_bytes
        self.sliding = sliding
        self._data: OrderedDict = OrderedDict()   # clé → (instant, taille, valeur)
        self.size = 0
        self.hits = self.misses = 0
        CACHES.append(self)

    def get(self, key):
        entry = self._data.get(key)
        if entry is not None and time.monotonic() - entry[0] > self.ttl:
            self._drop(key)
            entry = None
        if entry is None:
            self.misses += 1
            return None
        self._data.move_to_end(key)
        if self.sliding:
            self._data[key] = (time.monotonic(), entry[1], entry[2])
        self.hits += 1
        return entry[2]

    def put(self, key, value, size: int) -> None:
        # Incognito : rien de la requête ne survit à la réponse.
        if self.ttl <= 0 or size > self.max_bytes or INCOGNITO.get():
            return
        self._drop(key)
        self._data[key] = (time.monotonic(), size, value)
        self.size += size
        while len(self._data) > self.entries or self.size > self.max_bytes:
            self._drop(next(iter(self._data)))

    def _drop(self, key) -> None:
        entry = self._data.pop(key, None)
        if entry is not None:
            self.size -= entry[1]

    def purge(self) -> int:
        """Retire les entrées expirées → leur nombre."""
        now = time.monotonic()
        old = [k for k, (at, _, _) in self._data.items() if now - at > self.ttl]
        for k in old:
            self._drop(k)
        return len(old)

    def forget(self, keep) -> int:
        """Retire les entrées dont la clé ne passe pas `keep(clé)` → leur
        nombre (un compte qui efface ses dépôts)."""
        gone = [k for k in self._data if not keep(k)]
        for k in gone:
            self._drop(k)
        return len(gone)

    def clear(self) -> None:
        self._data.clear()
        self.size = self.hits = self.misses = 0

    def __len__(self) -> int:
        return len(self._data)


CACHE = Cache(TTL, ENTRIES, MAX_BYTES)


async def sweep() -> None:
    """La purge de tous les caches, toutes les SWEEP_EVERY secondes (une
    tâche du démarrage, startup.py)."""
    while True:
        await asyncio.sleep(SWEEP_EVERY)
        for cache in CACHES:
            cache.purge()
