"""
Client MCP (Model Context Protocol) : le proxy se connecte aux serveurs
MCP listés dans la configuration, découvre leurs outils, et chacun
devient un outil hébergé (contract.Tool) — présentable sur
/v1/chat/completions (déclaré, ou d'office par [chat].always), exécutable
par `POST /v1/tools/<nom>`. Sans liaison de protocole : les surfaces
Responses et Anthropic ne les présentent pas.

Ce qui est permis, et rien d'autre :
  * HTTP seulement (transport « Streamable HTTP »). Pas de stdio : le
    proxy ne lance aucun processus ;
  * une liste FERMÉE de serveurs, [tools.mcp.<serveur>] de config.toml.
    Jamais d'URL venue d'un client ou du modèle. L'URL est une adresse de
    CONFIGURATION, en général privée : le garde-fou des adresses publiques
    (net.py) ne s'y applique pas, comme pour l'instance SearXNG ;
  * des en-têtes STATIQUES pour l'authentification (secrets par ${VAR}).
    Pas d'OAuth : un serveur qui l'exige répond 401, et le journal le dit.

Le protocole est écrit ici avec httpx2 seul, sans SDK. Deux ÈRES, que la
spécification du 28/07/2026 distingue (basic/versioning) :
  * « moderne » (2026-07-28) : sans état. Ni `initialize` ni session :
    chaque requête porte sa version et les capacités du client dans
    `params._meta`, recopiées en en-têtes (`MCP-Protocol-Version`,
    `Mcp-Method`, `Mcp-Name`, et `Mcp-Param-<nom>` pour un paramètre que
    le schéma de l'outil marque `x-mcp-header`) ;
  * « héritée » (2025-03-26 → 2025-11-25) : `initialize`, puis
    `notifications/initialized`, puis les requêtes, sous `Mcp-Session-Id`
    si le serveur en a donné un. Session expirée = 404 : le client DOIT
    en rouvrir une — fait UNE fois, puis la requête est rejouée.
L'ère est celle du SERVEUR : sondée une fois par `server/discover`
(requête moderne), gardée, re-sondée après un échec de découverte. Un
400 qui ne porte pas une erreur JSON-RPC moderne (-32020 à -32022), ou
un résultat sans `resultType`, désigne un serveur hérité.
Dans les deux ères : un POST par message ; la réponse est un objet JSON
ou un flux SSE (`text/event-stream`) qui se termine par elle — les deux
sont lus, les notifications du flux sont ignorées, sauf
`notifications/tools/list_changed` qui avance la prochaine découverte.

DÉCOUVERTE. Asynchrone, elle peut échouer, et la liste change : `start()`
au démarrage, `stop()` à l'arrêt ; une tâche par serveur redemande
`tools/list` à période fixe (voir pause()). Un serveur éteint au démarrage
ne bloque rien : ses outils apparaissent quand il répond. Le registre ne sait pas retirer :
un outil enregistré le reste, et c'est son `enabled` qui suit la liste
du serveur — il garde ainsi sa place parmi les fonctions présentées au
modèle, donc le préfixe d'un backend à cache.

Ce qui entre dans le prompt du modèle vient d'un TIERS : nom, description
et schéma de chaque outil, puis ses résultats. Tout est borné ici ; rien
n'en est interprété. Le choix des serveurs, et de leurs outils (`tools`,
`exclude`), est le seul garde-fou : un outil exposé l'est à TOUS les
clients du proxy, avec le compte que portent les en-têtes configurés.

Rangé en parties :
  protocol.py    réglages, versions, traduction d'un outil et d'un résultat
  tool.py        l'outil hébergé d'un outil MCP (McpTool)
  server.py      un serveur : session, découverte, appels, veille
  registry.py    les serveurs ensemble : chargement, démarrage, état
"""

from .protocol import (  # noqa: F401
    DESCRIPTION_CHARS,
    Failure,
    GRACE,
    KIND,
    LEGACY,
    MAX_BYTES,
    MAX_TOOLS,
    MIN_GAP,
    MODERN,
    REFRESH,
    SCHEMA_CHARS,
    STARTUP_WAIT,
    TIMEOUT,
    definition,
    function_name,
    header_value,
    param_headers,
    render,
)
from .tool import (  # noqa: F401
    McpTool,
)
from .server import (  # noqa: F401
    Server,
)
from .registry import (  # noqa: F401
    SERVERS,
    _TASKS,
    kinds,
    load,
    start,
    status,
    stop,
)
