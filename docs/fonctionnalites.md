# Fonctionnalités

Un aperçu de ce que fait le proxy. Chaque entrée renvoie à la page qui
détaille.

## Relais

- **Un seul fichier de configuration** : `data/config.toml`.
  L'environnement ne sert qu'aux secrets, injectés par `${VAR}`.
  [Configuration](configuration.md)
- **Routage multi-backends** : une table `[backends.<nom>]` par backend.
  Le préfixe du modèle choisit le backend et lui est retiré (llama.cpp
  reçoit `qwen3-32b`, pas `bigchuck/qwen3-32b`).
- **Catalogue unifié** : `GET /v1/models` interroge tous les backends,
  préfixe les ids et uniformise les entrées (type, coûts,
  `max_context_length` tiré du `--ctx-size` ou du `n_ctx_train` de
  llama.cpp). Les noms rendus sont routables. Rien d'interne n'est publié
  (chemins `.gguf`, arguments).
- **Limiteur de quotas** (`quotas = true`, Albert) : retarde les requêtes
  pour rester sous les limites minute et jour du compte, lues sur
  `/v1/me/info`. Au-delà de `quotas.max_queue_seconds` : 429 local avec
  `Retry-After`. Un client qui raccroche quitte la file sans rien
  consommer (compté `499`). Deux comptes Albert ont des limiteurs
  indépendants.
- **Backends locaux à la demande** : jamais sondés, connexion coupée à
  1 s. Éteint, un backend répond 503 `backend_offline` avec `Retry-After`
  et disparaît de `/v1/models`.
- **Clés centralisées** : la clé d'un backend ne vit que dans le proxy.
  L'`Authorization` du client est remplacé. Les clients n'ont rien à
  configurer (une valeur bidon suffit si leur SDK exige une clé).
- **Correctif `tool_choice`** (`force_tool_choice`, par backend, inactif
  par défaut) : injecte `tool_choice: "auto"` quand `tools` est présent
  sans `tool_choice` (Albert déclare `"default": "none"`). Un
  `tool_choice` explicite n'est jamais écrasé.
- **Plafond `max_tokens`** (par backend, optionnel) : la valeur du client
  est ramenée au plafond (Claude Code en demande 32 000).
- **Surface minimale** : seuls `/v1/chat/completions`, `/v1/models` et
  les chemins de `FORWARD_POST_PATHS` sont relayés. Le reste : 404 local
  `unknown_route`. Le streaming SSE passe intact.
- **Audio et images** : synthèse, transcription, génération et édition
  d'image routées au préfixe du modèle, multipart compris.
  `GET /v1/audio/voices?model=<backend>/<modèle>` liste les voix.

## Autres API

- **API Anthropic Messages** (`[anthropic].enabled`) : `/v1/messages`,
  traduite vers chat/completions. [API Anthropic Messages](api-anthropic.md)
- **API OpenAI Responses** (`[responses].enabled`) : `/v1/responses`,
  traduite vers chat/completions. [API OpenAI Responses](api-responses.md)

## Outils hébergés

- **Recherche et lecture web** (`[tools.web_search]`, `[tools.web_fetch]`) :
  le proxy exécute lui-même ces outils, pour les clients Responses,
  Anthropic et chat/completions. [Outils hébergés](outils-heberges.md)
- **OCR, transcription** (`[tools.ocr]`, `[tools.transcribe]`) : texte
  d'une image, d'un PDF scanné ou d'un fichier audio, par un modèle d'un
  backend. [Outils hébergés](outils-heberges.md)
- **Exécution de code** (`[tools.code_execution]`) : un programme du
  modèle dans un bac à sable, par un service à part.
  [Exécution de code](execution-de-code.md)
- **Génération d'images** (`[tools.image_generation]`) : une image par le
  modèle d'images d'un backend, rendue par un lien.
  [Génération d'images](outils-heberges.md#génération-dimages)
- **Mémoire persistante** (`[tools.memory]`) : des fichiers texte sous
  `/memories`, cloisonnés par clé ou par compte.
  [Mémoire persistante](outils-heberges.md#mémoire-persistante-memory)
- **Serveurs MCP** (`[tools.mcp.<serveur>]`) : leurs outils deviennent des
  outils hébergés. [Serveurs MCP](outils.md#serveurs-mcp)
- **Serveur MCP** (`[mcp_server].enabled`) : `POST /mcp` offre les outils
  hébergés à tout client MCP. [Serveur MCP](outils-heberges.md#serveur-mcp--mcp)

## Suivi et interface

- **Tableau de bord** (`/ui`) : statistiques d'usage, chat si
  `[ui] chat = true`, générateurs de configuration.
  [Tableau de bord](tableau-de-bord.md), [Chat](chat.md)
- **Statistiques persistantes** : une ligne SQLite par requête
  (`data/stats.db`), lues par l'Usage API d'OpenAI
  (`/v1/organization/usage/completions`, `/v1/organization/usage/tools`).
  [Statistiques](statistiques.md)
- **Observabilité** : `GET /healthz` donne l'état de chaque backend
  (quotas restants, modèles vus, réglages) et de la surface Anthropic.
  Sans la clé, quand l'auth est active, seulement `{"status": "ok"}`. Un
  résumé périodique est loggé (`quotas.status_interval`), et chaque
  erreur upstream l'est avec le début de son corps.
