"""
La surface Anthropic du proxy : un client de l'API Messages parle à un
backend OpenAI sans le savoir. Dans ce sens uniquement : le proxy ne
parle pas à un backend Anthropic.

  POST /v1/messages               → traduit en /v1/chat/completions,
                                    réponse retraduite (JSON ou flux SSE)
  POST /v1/messages/count_tokens  → estimation locale, ~4 caractères par
                                    token (même ratio que le limiteur)
  GET  /v1/models                 → le catalogue à la forme Anthropic,
                                    choisi sur l'en-tête
                                    `anthropic-version` (le SDK Anthropic
                                    le pose toujours, le SDK OpenAI jamais)

Le modèle demandé passe d'abord par [anthropic.model_map] : certains
clients (Claude Code) envoient des noms Claude en dur ; la table les
traduit en noms préfixés, routés comme d'habitude.

La réponse passe par un «robinet» (tap) du relais (relaying.forward) :
feed(chunk) rend ce qu'il faut émettre, finish() le reliquat, tokens() ce
que les stats comptent. Translator réécrit un flux OpenAI en événements
Anthropic ; contrairement au relais brut, il désérialise chaque événement.

Outils serveur hébergés (`web_search_…`, `web_fetch_…`, paquet tools/) :
l'outil serveur déclaré par le client devient une fonction présentée au
modèle ; ses appels ne sont pas rendus en `tool_use` mais mis de côté
(`pending`), exécutés par tool_loop.py qui relance le backend, et le
client reçoit `server_tool_use` puis le bloc de résultat. Rien n'est
conservé entre deux requêtes : le bloc de résultat porte tout, son rejeu
rend au modèle le même texte (voir _assistant_messages). Un outil n'est
présenté que si le client le déclare.

Ce module ne fait aucune requête et ne connaît ni FastAPI ni httpx2.

Rangé en parties :
  common.py      réglages, erreurs, modèles, estimation des tokens
  request.py     la requête : Messages → chat/completions
  response.py    la réponse d'un seul tenant, outils serveur, erreurs, SSE
  stream.py      le flux : le robinet Translator
"""

from .common import (  # noqa: F401
    ENABLED,
    ERROR_CODES,
    MODEL_MAP,
    PING_INTERVAL,
    REASONING_AS_THINKING,
    TRACE,
    error_body,
    error_type,
    estimate_tokens,
    models_list,
    prompt_text,
    resolve_model,
)
from .request import (  # noqa: F401
    Context,
    has_images,
    to_openai,
)
from .response import (  # noqa: F401
    _fetch_content,
    from_openai,
    ping_event,
    sse_error,
)
from .stream import (  # noqa: F401
    Translator,
)
