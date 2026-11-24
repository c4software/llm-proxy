# Fonctionnalités

Inventaire des fonctionnalités du proxy. Les pages liées documentent chaque API,
réglage et outil.

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
  (chemins `.gguf`, arguments). Chaque modèle porte aussi ses modalités à
  la forme d'OpenRouter et de llama.cpp (`architecture` : `modality`,
  `input_modalities`, `output_modalities`) : celles du backend telles
  quelles, sinon déduites du `type` (Albert, Mistral, llama-swap) —
  `image-text-to-text` donne `text+image->text`. Les clients s'en servent
  pour savoir si un modèle lit les images (extensions de pi, omp,
  OpenCode).
- **Limiteur de quotas** (`quotas = true`) : retarde les requêtes pour
  rester sous des limites minute et jour : celles du compte (Albert,
  `/v1/limits`), celles qu'annoncent les en-têtes des réponses (Mistral,
  OpenAI, Groq, Anthropic, usage hors du proxy compris), les règles du
  backend, des familles de modèles. Leur état se lit dans *Configuration →
  Quotas*. Au-delà de `quotas.max_queue_seconds` : 429 local avec
  `Retry-After` (la file derrière les requêtes précédentes compte dans cette
  attente). Un client qui raccroche quitte la file sans rien consommer
  (compté `499`), même avant son tour. L'entrée est estimée à la taille du
  corps, puis la fenêtre est recalée sur l'`usage` réel de la réponse
  (sortie comprise), ou [rendue](configuration.md#quotas) par une requête
  que le backend refuse. Deux backends à quotas ont des limiteurs indépendants.
- **Un seul processus par dossier de données** : quotas, partage
  équitable et mémoires vivent en mémoire du processus. Un second
  processus (`--workers`, `WEB_CONCURRENCY`) sur les mêmes données
  s'arrête au démarrage (verrou `stats.db.lock`).
- **Flux interrompus** : une coupure de l'upstream en plein flux se dit au
  client (chunk d'erreur puis `[DONE]`, `event: error`, `response.failed`)
  et compte `502` ; un client qui raccroche en cours de réponse compte
  `499`. Dans les deux cas ce qui a été généré est compté (quotas,
  partage équitable, statistiques).
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
  `GET /v1/audio/voices?model=<backend>/<modèle>` liste les voix. Synthèse,
  conception et clonage de voix pas à pas : [Voix](voix.md).

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
- **Extensions** (`[extensions].path`) : des outils au contrat de tous les
  outils, un sous-dossier chacune (un `.py`, et un `.js` pour les outils
  du navigateur), chargés au démarrage, allumés ou éteints à chaud dans la
  page ;
  l'exemple `pull_request` ouvre des PR en brouillon au jeton d'un compte
  de service. [Extensions](extensions.md)
- **Serveur MCP** (`[mcp_server].enabled`) : `POST /mcp` offre les outils
  hébergés à tout client MCP. [Serveur MCP](outils-heberges.md#serveur-mcp--mcp)
- **Webhooks** (`[webhooks]`) : chaque compte reçoit des messages au
  format Slack sur une adresse secrète, auxquels un modèle répond dans le
  chat ; les réponses, et l'outil `webhook`, partent par Apprise
  (Slack, Mattermost, Discord, Teams, e-mail…). [Webhooks](webhooks.md)
- **Dépôts** (`[repos]`) : l'outil `repository` lit un projet GitLab,
  GitHub, Gitea ou git (arborescence, fichier, recherche ; rien n'est
  exécuté), avec le jeton d'une source du compte ou de l'admin. Le proxy
  télécharge, un bac de l'exécuteur décompresse et lit. [Dépôts](depots.md)

## Suivi et interface

- **Tableau de bord** (`/ui`) : statistiques d'usage, usage et limites
  de son compte, chat si
  `[ui] chat = true`, générateurs de configuration.
  [Tableau de bord](tableau-de-bord.md), [Chat](chat.md)
- **Statistiques persistantes** : une ligne SQLite par requête
  (`data/stats.db`), lues par l'Usage API d'OpenAI
  (`/v1/organization/usage/completions`, `/v1/organization/usage/tools`),
  et l'usage d'un compte au format du panneau d'agents d'Omarchy
  (`/v1/usage/agent`). [Statistiques](statistiques.md)
- **Observabilité** : `GET /healthz` donne l'état de chaque backend
  (quotas restants, modèles vus, réglages) et de la surface Anthropic.
  Sans la clé, quand l'auth est active, seulement `{"status": "ok"}`. Un
  résumé périodique est loggé (`quotas.status_interval`), et chaque
  erreur upstream l'est avec le début de son corps.
