"""
La surface Responses du proxy : POST /v1/responses traduit en
/v1/chat/completions, réponse retraduite (objet Response, ou flux
d'événements `response.*`). UNIQUEMENT dans ce sens, comme la surface
Anthropic : aucun backend n'a besoin de servir /v1/responses.

La tolérance de la requête reprend celle de gufo (gufo-org/gufo#434),
écrite sur les corps que Codex envoie réellement :
  * les outils que seul OpenAI exécute (`web_search`, `file_search`,
    `code_interpreter`, `mcp`…) sont ignorés, les `function` restent —
    sauf ceux que le proxy HÉBERGE lui-même (voir plus bas) ;
  * un `namespace` n'est PAS un outil hébergé : c'est un groupement côté
    client, ses fonctions sont aplaties dans la liste et appelées par
    leur nom simple — le `namespace` d'origine est reposé sur l'appel
    rendu au client, qui s'en sert pour router ; un nom présent deux fois
    (deux namespaces, ou un namespace et le premier niveau) est refusé ;
  * `custom` et `local_shell` ne sont pas hébergés non plus : ce sont des
    outils que le CLIENT exécute, sous une forme que chat/completions n'a
    pas. Chacun est présenté au modèle comme une fonction, et son appel
    rendu au client dans sa forme d'origine (voir plus bas) ;
  * les champs sans effet ici (`include`, `reasoning.summary`,
    `text.verbosity`, `prompt_cache_key`, `client_metadata`, `store`…)
    sont tolérés : le corps upstream est RECONSTRUIT, rien d'inconnu ne
    part vers un backend qui le refuserait ;
  * les éléments `reasoning` rejoués sont jetés, comme les blocs
    `thinking` d'un client Anthropic : aucun backend OpenAI ne les rejoue.

Ce qui demanderait un état côté serveur est refusé (400) plutôt que
perdu en silence : `previous_response_id`, `conversation`, `background`,
`item_reference`.

Outils hébergés (paquet tools/) : quand le proxy sait exécuter un outil
que le client déclare (`web_search`), il présente au modèle les fonctions
correspondantes ; leurs appels ne sont PAS rendus au client : le robinet
les met de côté (`pending`), tool_loop.py les exécute et relance le
backend, et le client ne voit qu'un élément `web_search_call` terminé. Au
tour suivant le client renvoie cet élément sans son résultat : il est relu
dans la mémoire du paquet tools/ et redevient, à l'identique, un appel
suivi de son résultat. Ce module ne fait aucune requête : `Hosted` lui
sert d'annuaire.

Outils du client hors `function` (formes de Codex CLI et du SDK
openai-python) :
  * `custom` — outil « freeform » dont l'entrée est un TEXTE (Codex :
    `apply_patch`). Devient une fonction à un seul champ `input` ; la
    grammaire rejoint la description. L'appel est rendu en
    `custom_tool_call`, rejoué avec un `custom_tool_call_output` ;
  * `local_shell` — devient une fonction `local_shell`, l'appel est rendu
    en `local_shell_call` avec son `action` `exec`, rejoué avec un
    `local_shell_call_output` (SDK) ou un `function_call_output` (Codex).
La fonction prend le nom de l'outil, suffixé (`_2`…) si une fonction du
client le porte déjà : c'est la fonction du client qui garde le sien.

Même robinet que anthropic_api.Translator : un flux OpenAI (deltas plats,
outils fragmentés par index) devient la séquence d'événements Responses,
éléments ouverts et fermés un à un, numérotés (`sequence_number`).

Ce module ne connaît ni FastAPI ni httpx2.

Rangé en parties :
  common.py      réglages, refus, contexte d'un échange
  request.py     la requête : Responses → chat/completions
  response.py    la réponse d'un seul tenant : chat/completions → Responses
  stream.py      le flux : le robinet Translator
"""

from .common import (  # noqa: F401
    Context,
    ENABLED,
    REASONING_AS_SUMMARY,
    Refused,
)
from .request import (  # noqa: F401
    CUSTOM_NOTE,
    has_images,
    to_chat,
)
from .response import (  # noqa: F401
    error_body,
    from_chat,
)
from .stream import (  # noqa: F401
    Translator,
)
