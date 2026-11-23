# API OpenAI Responses

Le proxy expose l'API OpenAI Responses utilisée par Codex CLI et l'application
Codex ([réglages du client](tableau-de-bord.md#longlet-configuration)). Activez
`[responses].enabled = true` dans `config.toml`. Le proxy traduit
`POST /v1/responses` en `POST /v1/chat/completions` ; le backend n'a pas besoin
d'implémenter l'API Responses.

Le nom du modèle doit inclure le préfixe du backend. Le proxy renvoie un objet
`response` ou un flux d'événements `response.*`. Les statistiques identifient
les requêtes par `/v1/responses` et utilisent l'`usage` du backend.

Réglages (`enabled`, `reasoning_as_summary`) : voir
[Configuration](configuration.md#responses).

## Traitement de la requête

- **Outils hébergés.** Lorsque `[tools.web_search]` et `[tools.web_fetch]` sont
  actifs, le proxy remplace `web_search` par ses fonctions et les exécute
  ([Outils hébergés](outils-heberges.md)). Sinon, il retire `web_search` et les
  outils pris en charge uniquement par OpenAI (`file_search`, `code_interpreter`,
  `mcp`, `image_generation`…). Il consigne les outils retirés (`responses :
  file_search sans équivalent chat, ignoré(s)`). Les outils `function` sont
  conservés.
- **`namespace` aplatis.** Les fonctions des espaces de noms (notamment
  `multi_agent_v1` dans Codex) sont ajoutées à la liste sous leur nom simple.
  Le proxy rétablit le `namespace` dans l'appel renvoyé au client. Les noms en
  double produisent une erreur `400`. Un espace de noms peut contenir un outil
  `custom` utilisé par les modèles en mode « responses lite ».
- **Message `system` unique.** Le proxy regroupe `instructions` et les messages
  `developer` initiaux dans le message `system` placé en tête. Cette forme est
  requise par les gabarits qui n'acceptent qu'un seul message `system` et
  stabilise le préfixe pour le cache du backend.
- **`reasoning`.** Les éléments `reasoning` rejoués sont supprimés. Le proxy
  convertit le `reasoning_content` du backend en élément `reasoning` (résumé)
  lorsque `reasoning_as_summary` est actif. `reasoning.effort` devient
  `reasoning_effort`.
- **Champs sans effet tolérés** : `include`, `reasoning.summary`,
  `text.verbosity`, `prompt_cache_key`, `client_metadata`, `store`… Le
  corps envoyé au backend est reconstruit : rien d'inconnu ne part.
- **Aucun état.** `previous_response_id`, `conversation`, `background` et
  `item_reference` donnent un `400` explicite. Le client renvoie tout
  l'historique à chaque tour. Seule exception, outils hébergés actifs :
  la [mémoire des résultats](outils-heberges.md#mémoire-des-résultats).

## Outils `custom` et `local_shell`

Responses définit `custom` et `local_shell` comme des outils exécutés par le
client ; chat/completions ne propose pas ces types. Le proxy les déclare au
modèle sous forme de fonctions.

| | `custom` (« freeform ») | `local_shell` |
|---|---|---|
| Usage | Entrée en texte libre (`apply_patch` de Codex, avec une grammaire lark) | Commande shell |
| Présenté au modèle | Fonction à un champ `input`. Description reprise, suivie d'une consigne et de la grammaire, **non** imposée au décodage | Fonction `local_shell` : `command` (tableau), `working_directory`, `timeout_ms`, `env` |
| Rendu au client | `custom_tool_call` (`call_id`, `name`, `input` texte). En flux, `response.custom_tool_call_input.delta` (un seul, l'entrée entière) puis `.done` | `local_shell_call`, action `exec` |
| Arguments mal écrits | Texte reçu rendu tel quel | Rendu `incomplete` |

Au tour suivant, l'appel et sa sortie (`custom_tool_call_output`,
`local_shell_call_output`, `function_call_output`) redeviennent appel
assistant et message `tool`. Si une fonction du client porte déjà le nom,
elle le garde : l'outil est présenté suffixé (`apply_patch_2`) et son
appel revient sous son vrai nom.

Codex CLI n'envoie par défaut que des `function`, un `namespace` et
`web_search` ; ces traductions servent aux clients qui déclarent `custom`
ou `local_shell`. OpenAI annonce la fin de `local_shell`, avec
`codex-mini-latest`, au 12/02/2026.
