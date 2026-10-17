# API Anthropic Messages

Le proxy peut parler l'API Messages d'Anthropic, celle des SDK Anthropic
et de Claude Code. Activez `[anthropic].enabled = true` dans
`config.toml`. Les requêtes sont traduites vers `/v1/chat/completions`
du backend visé. Le modèle porte le préfixe du backend
(`albert/deepseek-v4-flash`), ou passe par `model_map`.

Réglages (`enabled`, `model_map`, `ping_interval`,
`reasoning_as_thinking`, `trace`) : voir
[Configuration](configuration.md#anthropic).

## Noms de modèles : `[anthropic.model_map]`

Un client Anthropic peut envoyer des noms Claude en dur (Claude Code le
fait pour ses tâches d'arrière-plan). La table les traduit en noms
préfixés.

- Un suffixe `[1m]` est ignoré.
- Un nom déjà préfixé passe tel quel.
- `default` attrape le reste. Sans correspondance : 400.

L'exemple envoie tous les noms connus sur `albert/deepseek-v4-flash`.

## À savoir

- **Coût.** Un client agent renvoie un long prompt système (plusieurs
  milliers de tokens pour Claude Code) à chaque tour, et `cache_control`
  est ignoré. Le quota journalier d'Albert fond vite ; envoyer les noms
  des tâches d'arrière-plan sur un backend local (`model_map`) soulage.
- **Outils serveur `web_search_…` et `web_fetch_…`** : exécutés par le
  proxy si `[tools.web_search]` / `[tools.web_fetch]` sont actifs,
  ignorés sinon. Voir
  [L'outil serveur `web_search`](outils-heberges.md#loutil-serveur-web_search)
  et [L'outil serveur `web_fetch`](outils-heberges.md#loutil-serveur-web_fetch).
- **Hors périmètre** : Batches, Files, PDF, outils serveur autres que
  recherche et lecture de page (`code_execution`…).

## Routes et comportement

- **`POST /v1/messages`** rend un objet `Message`, ou en flux des
  événements SSE (`message_start` → blocs → `message_delta` →
  `message_stop`), outils compris. En flux, le proxy demande
  `stream_options.include_usage` : stats exactes.
- **`POST /v1/messages/count_tokens`** est exact si le backend a un
  `tokenize_path` (llama.cpp : `/tokenize`). Sinon, estimation à ~4
  caractères par token. Un client s'en sert pour mesurer son contexte
  avant de le compacter.
- **`GET /v1/models`** répond à la forme Anthropic si la requête porte
  `anthropic-version` (toujours posé par le SDK Anthropic, jamais par
  celui d'OpenAI).
- **Auth** : `x-api-key` accepté en plus de `Authorization: Bearer`.
  `x-api-key`, `anthropic-version` et `anthropic-beta` ne sont jamais
  relayés.
- **Erreurs** : toutes à la forme Anthropic (401, 400, 429, 503…).
- Limiteur, stats (`endpoint = /v1/messages`), `force_tool_choice` et
  `max_tokens` s'appliquent comme pour tout client.
- **Attente du quota en flux** : le `200` part tout de suite et un
  `event: ping` part toutes les `ping_interval` s (Claude Code coupe un
  flux muet). Un 429 local devient un `event: error`
  (`rate_limit_error`). Un client qui raccroche quitte la file sans
  consommer de quota (499).
- **Images** : transmises si le backend a `images = true` et que le modèle
  est de type `image-text-to-text` à son catalogue. Chez llama.cpp, il
  faut le charger avec son `--mmproj` (type tiré
  d'`architecture.input_modalities`). Le catalogue est lu à la première
  image.

## Référence : traduction Anthropic ↔ OpenAI

| Anthropic | OpenAI |
|---|---|
| `system` (chaîne ou blocs) | message `system` en tête |
| `system` en cours de conversation (rappels) | fondu en tête du `user` suivant (Qwen / Mistral refusent un `system` ailleurs : 500) |
| bloc `text` | texte |
| bloc `image` | `image_url` (data URI) si autorisé ; sinon `[image ignorée : image/png, 12 Ko]` |
| bloc `document` | son texte ; un PDF → `[document ignoré : …]` |
| `tool_use` (assistant) | `tool_calls[]`, arguments sérialisés |
| `tool_result` (user) | un message `tool` par résultat, avant le reste ; une image du résultat suit en message `user` |
| `thinking` / `redacted_thinking` | jetés |
| `tools[{name, input_schema}]` | `tools[{type: function, …parameters}]` |
| outil serveur `web_search_20250305` | fonction `web_search` du proxy si `[tools.web_search].enabled` ; ignoré sinon |
| outil serveur `web_fetch_20250910` | fonction `web_fetch` du proxy si `[tools.web_fetch].enabled` ; ignoré sinon |
| autres outils serveur (`code_execution_…`, `bash`…) | ignorés |
| `server_tool_use` + `web_search_tool_result` / `web_fetch_tool_result` rejoués | appel et message `tool`, si l'outil est encore déclaré ; ignorés sinon |
| `tool_choice` `auto` / `any` / `tool` / `none`, `disable_parallel_tool_use` | `auto` / `required` / `{function}` / `none`, `parallel_tool_calls: false` |
| `stop_sequences`, `metadata.user_id`, `temperature`, `top_p`, `max_tokens` | `stop`, `user`, idem (plafond `max_tokens` du backend) |
| `top_k`, `cache_control`, `thinking`, `output_config`, `context_management`, `?beta=true` | ignorés |
| `stop_reason` `end_turn` / `max_tokens` / `tool_use` | `finish_reason` `stop` / `length` / `tool_calls` |
| bloc `thinking` (`reasoning_as_thinking`) | `reasoning_content` du backend |
| `{"type": "error", "error": {type, message}}` | `{"error": {…}}`, type déduit du statut |
