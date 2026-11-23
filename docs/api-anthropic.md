# API Anthropic Messages

Le proxy expose l'API Messages d'Anthropic, utilisée par les SDK Anthropic et
Claude Code. Activez `[anthropic].enabled = true` dans `config.toml`. Les
requêtes sont traduites vers `/v1/chat/completions` du backend sélectionné. Le
modèle doit porter le préfixe du backend (`albert/deepseek-v4-flash`) ou être
associé à un modèle préfixé dans `model_map`.

Réglages (`enabled`, `model_map`, `ping_interval`,
`reasoning_as_thinking`, `trace`) : voir
[Configuration](configuration.md#anthropic).

## Noms de modèles : `[anthropic.model_map]`

Certains clients envoient des noms Claude fixes, notamment pour les tâches
d'arrière-plan de Claude Code. `model_map` les associe à des noms de modèle
préfixés.

- Un suffixe `[1m]` est ignoré.
- Un nom déjà préfixé passe tel quel.
- `default` attrape le reste. Sans correspondance : 400.

L'exemple associe tous les noms reconnus à
`albert/deepseek-v4-flash`.

## À savoir

- **Coût.** Les clients agent renvoient leur prompt système à chaque tour ;
  Claude Code peut envoyer plusieurs milliers de tokens. `cache_control` est
  ignoré. Ces requêtes consomment rapidement le quota journalier d'Albert.
  `model_map` peut router les tâches d'arrière-plan vers un backend local.
- **Outils serveur `web_search_…` et `web_fetch_…`** : le proxy exécute
  chaque outil si sa table est active (`[tools.web_search]` pour
  `web_search_…`, `[tools.web_fetch]` pour `web_fetch_…`). Sinon, il l'ignore.
  Voir [L'outil serveur `web_search`](outils-heberges.md#loutil-serveur-web_search)
  et [L'outil serveur `web_fetch`](outils-heberges.md#loutil-serveur-web_fetch).
- **Hors périmètre** : Batches, Files, PDF et outils serveur autres que la
  recherche et la lecture de pages (`code_execution`…).

## Routes et comportement

- **`POST /v1/messages`** rend un objet `Message`, ou en flux des
  événements SSE (`message_start` → blocs → `message_delta` →
  `message_stop`), outils compris. En flux, le proxy demande
  `stream_options.include_usage` : stats exactes.
- **`POST /v1/messages/count_tokens`** : exact si le backend configure
  `tokenize_path` (par exemple `/tokenize` pour llama.cpp). Sinon, le proxy
  estime le nombre de tokens à environ un token pour quatre caractères.
- **`GET /v1/models`** : renvoie le format Anthropic lorsque la requête contient
  `anthropic-version`. Le SDK Anthropic ajoute cet en-tête ; le SDK OpenAI ne
  l'ajoute pas.
- **Auth** : `x-api-key` accepté en plus de `Authorization: Bearer`.
  `x-api-key`, `anthropic-version` et `anthropic-beta` ne sont jamais
  relayés.
- **Erreurs** : toutes à la forme Anthropic (401, 400, 429, 503…).
- Limiteur, stats (`endpoint = /v1/messages`), `force_tool_choice` et
  `max_tokens` s'appliquent comme pour tout client.
- **Attente du quota en flux** : le proxy ouvre immédiatement la réponse HTTP
  200 et envoie un `event: ping` toutes les `ping_interval` secondes pour
  maintenir la connexion Claude Code. Un 429 local produit un `event: error`
  (`rate_limit_error`). Si le client se déconnecte, sa requête quitte la file
  et ne consomme pas de quota (499).
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

**Rappels `system` et cache de préfixe.** Claude Code ajoute à chaque
tour un message `role: "system"` dans `messages` et le garde dans les
requêtes suivantes. Servi en direct par gufo sur `/v1/messages`, ce
texte est déplacé dans le system de tête (le gabarit de Qwen n'en
accepte qu'un) : le préfixe change à chaque requête et tout ce qui suit
est recalculé (gufo-org/gufo#522 ; gufo déclare Claude Code non pris en
charge avec Qwen, #672). Le proxy le laisse à sa place, en message
`user` : le préfixe ne bouge plus. Mesuré le 20/10/2026 (Claude Code
2.1.302, gufo 0.18.0, Qwen3.8-Flash-Next, même tâche de quatre ou cinq
tours) : en direct, le cache plafonne à 15 779 tokens et chaque tour
recalcule 1 091 à 2 363 tokens, un écart qui grandit avec l'historique ;
par le proxy, il suit l'historique et chaque tour n'en recalcule que 127
à 141 (premier token en 0,42 s au lieu de 0,9 à 2,1 s).
