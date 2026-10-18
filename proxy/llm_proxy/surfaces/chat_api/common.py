"""
Ce que partagent la déclaration, la mémoire et le flux des outils
hébergés sur /v1/chat/completions : les réglages ([chat]), le refus d'une
requête, le contexte d'une réponse.
"""

import uuid
from typing import TYPE_CHECKING

from ...core import config

if TYPE_CHECKING:          # l'annotation seule : memory.py importe ce module
    from .memory import Memory


# Inactif par défaut : un backend peut avoir SA lecture de
# `{"type": "web_search"}` dans `tools` (Zhipu en a une).
ENABLED = config.flag("chat.hosted_tools", False)


# Annotations `url_citation` en fin de réponse : les URL que les outils
# ont rendues ET que le modèle a écrites dans sa réponse.
ANNOTATIONS = config.flag("chat.annotations", True)


# La mémoire des échanges cachés (tête de module).
MEMORY_ENABLED = config.flag("chat.memory", True)


# Les outils présentés D'OFFICE (tête de module) : des NOMS seulement, résolus
# dans l'annuaire de chaque requête — le registre peut encore grandir.
ALWAYS = list(dict.fromkeys(config.strings("chat.always")))


SERVER = "mcp:"


# Motifs (jokers shell, casse ignorée) sur «<backend>/<modèle>».
ALWAYS_EXCEPT = [p.lower() for p in config.strings("chat.always_except")]


# Bornes en entrées et en durée de la mémoire des résultats ([tools]), plus
# une borne en CARACTÈRES : une entrée d'ici peut porter 8 résultats.
MEMORY_ENTRIES = config.integer("tools.cache_entries", 512)


MEMORY_TTL = config.num("tools.cache_ttl", 24 * 3600)


MEMORY_CHARS = config.integer("chat.memory_chars", 8_000_000)


CHARS_PER_TOKEN = 4


# Entre les textes de deux tours, que le client reçoit comme UN message.
GAP = "\n\n"


# Début du résultat d'un outil annoncé au chat de la page (Translator.start).
PREVIEW_CHARS = 600


class Refused(Exception):
    """Déclaration qu'on ne sait pas honorer : 400, avec ce message."""


class Context:
    """Ce que la réponse doit savoir de la requête."""

    def __init__(self):
        # nom → outil de tools/
        self.hosted: dict = {}
        # Le client a-t-il demandé `stream_options.include_usage` ? Le
        # proxy, lui, le demande toujours au backend (stats exactes).
        self.include_usage = False
        self.annotations = ANNOTATIONS
        # Posés par restore() : où ranger, pour quel client (tools.owner),
        # et le condensé des messages de la requête. `state` None = rien ne
        # sera rangé.
        self.memory: "Memory | None" = None
        self.client = ""
        self.state = None
        # Identifiant de conversation (tools.Call.session) ; restore() le
        # remplace par celui du dernier échange reconnu.
        self.session = uuid.uuid4().hex
        # Annoncer l'exécution des outils hébergés dans le flux (le chat
        # du tableau de bord, ui/dashboard.ui_chat) : voir Translator.start.
        self.progress = False
