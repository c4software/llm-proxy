# API OpenAI Responses

Le proxy peut parler l'API Responses d'OpenAI, celle de Codex CLI.
Activez `[responses].enabled = true` dans `config.toml`.
`POST /v1/responses` est traduit vers `/v1/chat/completions` : aucun
backend n'a besoin de servir `/v1/responses`.

Le modèle porte le préfixe du backend, sans table de correspondance. La
réponse revient en objet `response` ou en flux d'événements `response.*`.
Les stats comptent ces requêtes sous `/v1/responses`, avec l'`usage`
exact du backend.

Réglages (`enabled`, `reasoning_as_summary`) : voir
[Configuration](configuration.md#responses).

## Traitement de la requête

- **Outils hébergés.** Si `[tools.web_search]` et `[tools.web_fetch]`
  sont actifs, le proxy remplace `web_search` par ses propres fonctions et
  les exécute ([Outils hébergés](outils-heberges.md)). Sinon `web_search`
  est retiré, comme `file_search`, `code_interpreter`, `mcp`,
  `image_generation`…, que seul OpenAI exécute. Une ligne de log les
  nomme (`responses : file_search sans équivalent chat, ignoré(s)`). Les
  outils `function` restent.
- **`namespace` aplatis.** Les fonctions d'un `namespace` (les
  `multi_agent_v1` de Codex) rejoignent la liste sous leur nom simple. Le
  `namespace` est reposé sur l'appel rendu au client. Un nom en double :
  `400`. Un `namespace` peut contenir un `custom` (modèles en mode
  « responses lite »).
- **Un seul `system`.** `instructions` et les messages `developer` du
  début sont réunis dans le message system de tête, car beaucoup de
  gabarits n'en acceptent qu'un. Ceux qui arrivent en cours de
  conversation (Codex, après une compaction ou une interruption) sont
  fondus à leur place dans un message `user`, en bloc `<system-reminder>`,
  comme sur l'[API Anthropic](api-anthropic.md). Le préfixe reste stable :
  le cache du backend en profite (colonne *Cache*).
- **`reasoning`.** Les éléments rejoués sont jetés. Le
  `reasoning_content` du backend devient un élément `reasoning` (résumé)
  dans le client (`reasoning_as_summary`). `reasoning.effort` part en
  `reasoning_effort`.
- **Champs sans effet tolérés** : `include`, `reasoning.summary`,
  `text.verbosity`, `prompt_cache_key`, `client_metadata`, `store`… Le
  corps envoyé au backend est reconstruit : rien d'inconnu ne part.
- **Aucun état.** `previous_response_id`, `conversation`, `background` et
  `item_reference` donnent un `400` explicite. Le client renvoie tout
  l'historique à chaque tour. Seule exception, outils hébergés actifs :
  la [mémoire des résultats](outils-heberges.md#mémoire-des-résultats).

## Outils `custom` et `local_shell`

Ces outils sont exécutés par le client, sous une forme que
chat/completions n'a pas. Le proxy les présente au modèle en fonctions.

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
