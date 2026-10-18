"""
Les outils hébergés sur /v1/chat/completions, pour un client qui ne parle
que cette API : il déclare un outil que le proxy exécute, ou le reçoit
d'office, sans rien porter d'autre (ni extension, ni boucle). Voir
docs/outils-heberges.md.

DÉCLARATION, dans `tools` : la forme de l'API Responses, telle quelle —
`{"type": "web_search"}` (les types que déclarent les outils de tools/).
chat/completions n'a pas de forme standard pour cela ; un backend ne
connaît que `function` et refuse le reste, donc le proxy la reconnaît sans
ambiguïté. `web_search_options` (champ racine d'OpenAI, le seul que son SDK
sait écrire) en est un SYNONYME ; ses réglages sont ignorés. Le RETOUR suit
la convention commune : des annotations `url_citation`.

Ce module ne s'applique qu'à une requête à qui un outil hébergé est
présenté, sur un proxy où [chat].hosted_tools est vrai ; toute autre
requête est relayée octet pour octet (endpoints.chat_completions).

PRÉSENTATION D'OFFICE ([chat].always, `ALWAYS`, `unasked`) : pour une
interface qui ne déclare jamais rien, des outils du registre présentés à
toute requête, comme si elle les avait déclarés.
  * Par NOM d'outil, à la suite des outils du client ; `mcp:<serveur>` vaut
    tous les outils de ce serveur, ceux qu'il annoncera plus tard compris
    (`offered`). Pas de `mcp` nu : ce qui entre partout se nomme.
  * Un nom inconnu ou désactivé est ignoré, pas refusé : le client n'a rien
    demandé. Relu à chaque requête contre les outils actifs.
  * Rien d'office à `tool_choice: "none"`, à `n` > 1, ni à un modèle que
    le catalogue ne dit pas de conversation (catalog._converses) : relais
    brut.
  * Les autres `tool_choice` du client restent intacts (les outils d'office
    sont présentés quand même : le préfixe ne change pas d'une requête à
    l'autre).
  * Une requête de service (titre, tags) ne se distingue pas à son corps :
    seul son MODÈLE, s'il est dans [chat].always_except (`excepted`), la
    prive de la présentation d'office ; ce qu'on lui déclare reste honoré.

Le client reçoit UNE réponse chat/completions ordinaire, quel que soit le
nombre de tours upstream (`Translator`). Les appels hébergés ne lui
arrivent JAMAIS en `tool_calls` — il tenterait de les exécuter. Les
fichiers produits par un outil sont rangés au magasin (files.py) et leurs
liens ajoutés par le proxy à la fin du contenu.

MÉMOIRE DES ÉCHANGES CACHÉS (`Memory`, `restore`, [chat].memory). Le
client ne renvoie que ses messages et les réponses finales : sans rien de
plus, le modèle perdrait ce qu'il avait lu et le cache de préfixe du
backend ne servirait plus. Rien n'identifie la conversation : elle est
reconnue À SON CONTENU. Une réponse conclue après au moins un tour d'outils
hébergés range l'échange caché (messages assistant à `tool_calls` et
messages `tool`, tels qu'envoyés au backend) sous un condensé des messages
de la requête et de la réponse finale. À la requête suivante, l'échange
est réinséré avant chaque message assistant dont le condensé est connu, et
ce message reprend le texte du DERNIER tour : le backend reçoit à l'octet
près ce qu'il a vu pendant la boucle.
  * Le condensé (`_canon`) ne retient par message que le rôle, le texte
    (chaîne ou parties `text`, espaces de bord retirés), les identifiants
    de `tool_calls` et le `tool_call_id`. Les messages `system` /
    `developer` n'y entrent pas : bien des clients y écrivent l'heure.
  * Dans le doute, rien n'est réinséré — jamais pire que sans mémoire :
    réponse modifiée, historique tronqué, entrée expirée, redémarrage… Un
    condensé ne désigne que l'endroit exact de l'échange.
  * Une réponse que la limite dure a close ne range rien : ses derniers
    résultats n'ont jamais été lus.
  * L'entrée porte la SESSION des outils (tools.Call.session : le bac
    d'exécution de code) : c'est l'identifiant de conversation, repris du
    dernier échange reconnu, sinon neuf. Il ne sort jamais du proxy.
  * Le condensé porte la réponse telle que le client l'a reçue, liens des
    fichiers compris ; le texte rejoué au backend est sans eux.
  * Cloisonnée par client comme tools.Memory, en mémoire vive, bornée en
    entrées, en durée ET en caractères. Rien n'en est journalisé que des
    comptes.
  * Seule une requête à qui un outil hébergé est présenté est relue ainsi ;
    les entrées d'un client qui cesse de déclarer restent et resservent.

TOUR MIXTE (un outil hébergé et un outil du client dans le même tour) :
exécuter puis rendre la main perdrait le résultat, que le client ne peut
pas rejouer. Le PREMIER appel du tour décide :
  * hébergé d'abord : les appels hébergés sont exécutés, ceux du client ne
    lui sont pas transmis, le backend est relancé et les réémettra ;
  * client d'abord : ses appels sont partis au fil de l'eau (les retenir
    priverait le client du flux) ; les appels hébergés qui suivent ne sont
    pas exécutés, le modèle les redemandera.
Les historiques du client et du backend restent cohérents.

Même contrat de robinet que responses_api.Translator et
anthropic_api.Translator : tool_loop.hosted_loop mène la boucle. Ce module
ne connaît ni FastAPI ni httpx2.

Rangé en parties :
  common.py      réglages [chat], refus, contexte d'une réponse
  declare.py     les outils déclarés, présentés d'office, la requête préparée
  memory.py      la mémoire des échanges cachés, et leur réinsertion
  stream.py      le flux : le robinet Translator
"""

from .common import (  # noqa: F401
    ALWAYS,
    ALWAYS_EXCEPT,
    ANNOTATIONS,
    Context,
    ENABLED,
    MEMORY_ENABLED,
    Refused,
    SERVER,
)
from .declare import (  # noqa: F401
    declares,
    excepted,
    offered,
    prepare,
    unasked,
)
from .memory import (  # noqa: F401
    MEMORY,
    Memory,
    restore,
)
from .stream import (  # noqa: F401
    Translator,
)
