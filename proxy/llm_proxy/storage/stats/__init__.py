"""
Statistiques d'usage du proxy, agrégées PAR MODÈLE tel que le client l'a
demandé (nom préfixé : « albert/openweight-large », « bigchuck/qwen3 »).

Une ligne SQLite par requête servie (table `requests`) : toute fenêtre
temporelle est calculable après coup. La lecture suit la forme de
l'Usage API d'OpenAI (`/v1/organization/usage/completions`) : le tableau
de bord consomme la même chose qu'un client tiers.

L'écriture ne touche jamais la boucle d'événements : record() dépose la
ligne dans une file et rend la main ; un unique thread écrivain vide la
file par lots et purge les lignes trop vieilles (stats.retention_days).
La base est en WAL, les lecteurs ouvrent leur propre connexion et ne
bloquent donc pas l'écrivain.

Les exécutions d'outils hébergés ont leur table, `tool_calls`, déposée
par tools.Hosted.run dans la même file. Elle ne porte QUE des mesures :
ni requête de recherche, ni URL, ni résultat — ce que les utilisateurs
cherchent ou lisent n'a pas à survivre dans une base de statistiques.
Une réponse qui a utilisé des outils garde UNE ligne dans `requests`,
avec son usage de tokens cumulé.

Comptage des tokens, par ordre de préférence :
  1. le bloc `usage` renvoyé par l'upstream (exact) — présent sur les
     réponses non streamées, et sur les flux SSE quand le client a
     demandé `stream_options.include_usage` ;
  2. à défaut, une estimation (CHARS_PER_TOKEN caractères par token) :
     corps de la requête pour l'entrée, deltas de contenu accumulés pour
     la sortie.
Les deux sont comptés séparément (`num_estimated_requests` dans la
réponse) pour que le chiffre affiché reste honnête.

Chaque ligne porte `api_key_id`, le condensé de la clé présentée
(jamais la clé elle-même), «» pour un proxy ouvert. Il n'est passé à
aucune fonction : l'auth le pose dans CLIENT (variable de contexte),
record() et record_tool() l'y lisent, loin de la route.

Ce module ne connaît ni FastAPI ni httpx2.

Rangé en parties :
  collector.py   l'usage lu dans un flux relayé (UsageCollector)
  db.py          la base SQLite : schéma, file d'écriture, enregistrement
  usage.py       l'Usage API (requêtes, outils hébergés)
"""

from .collector import (  # noqa: F401
    UsageCollector,
    cached_tokens,
)
from .db import (  # noqa: F401
    _pending,
    CLIENT,
    DB_PATH,
    INSERT,
    INSERT_TOOL,
    RETENTION_DAYS,
    SCHEMA,
    TABLES,
    TOOLS_SCHEMA,
    TOOL_OUTCOMES,
    _purge,
    _reader,
    close,
    init,
    record,
    record_tool,
    reset,
)
from .usage import (  # noqa: F401
    BUCKET_WIDTHS,
    DEFAULT_LIMITS,
    GROUP_FIELDS,
    MAX_LIMIT,
    TOOL_GROUP_FIELDS,
    default_limit,
    parse_group_by,
    recent,
    recording_since,
    usage_completions,
    usage_tools,
)
