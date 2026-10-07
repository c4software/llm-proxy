"""
Cache web des outils hébergés : ce qu'une lecture de page ou une recherche
a rapporté, gardé quelques minutes, pour ne pas le redemander.

À ne pas confondre avec les MÉMOIRES de conversation (tools.Memory,
chat_api.Memory), qui rendent au modèle ce qu'il a déjà lu dans UNE
conversation : ici c'est le web lui-même qui est épargné, quel que soit le
client.

Deux raisons, vues le 05/10/2026 :
  * une page longue est lue en plusieurs appels (`offset`) — sans cache,
    chaque morceau la retéléchargeait en entier ;
  * les moteurs de SearXNG bloquent vite une adresse qui les sollicite
    trop (limite de débit, CAPTCHA) : une recherche identique refaite dans
    la minute n'a pas à repartir chez eux.

Durée COURTE par défaut (`[tools].web_cache_ttl`, 10 minutes ; 0 = pas de
cache) : au-delà, une page d'actualité ou une liste de releases serait
servie périmée. En mémoire vive, borné en entrées et en octets, perdu au
redémarrage. Seuls les succès y entrent — jamais une erreur, une liste
vide ou un moteur indisponible, qu'il faut pouvoir retenter. Commun à tous
les clients : une page publique est la même pour tous, et les garde-fous
(adresses publiques, listes de domaines) sont appliqués AVANT la lecture
du cache.
"""

import time
from collections import OrderedDict

from .. import config

TTL = config.num("tools.web_cache_ttl", 600)
ENTRIES = config.integer("tools.web_cache_entries", 256)
MAX_BYTES = config.integer("tools.web_cache_bytes", 64_000_000)


class Cache:
    """LRU à durée de vie, bornée en entrées et en taille cumulée. `size`
    est donné par l'appelant (octets du corps, caractères des résultats)."""

    def __init__(self, ttl: float, entries: int, max_bytes: int):
        self.ttl, self.entries, self.max_bytes = ttl, entries, max_bytes
        self._data: OrderedDict = OrderedDict()   # clé → (instant, taille, valeur)
        self.size = 0
        self.hits = self.misses = 0

    def get(self, key):
        entry = self._data.get(key)
        if entry is not None and time.monotonic() - entry[0] > self.ttl:
            self._drop(key)
            entry = None
        if entry is None:
            self.misses += 1
            return None
        self._data.move_to_end(key)
        self.hits += 1
        return entry[2]

    def put(self, key, value, size: int) -> None:
        if self.ttl <= 0 or size > self.max_bytes:
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

    def clear(self) -> None:
        self._data.clear()
        self.size = self.hits = self.misses = 0

    def __len__(self) -> int:
        return len(self._data)


CACHE = Cache(TTL, ENTRIES, MAX_BYTES)
