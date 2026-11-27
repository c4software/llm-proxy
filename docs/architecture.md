# Architecture

Structure du dépôt, dépendances entre paquets et traitement d'une requête.

## Le dépôt

La racine contient les fichiers d'orchestration et les dossiers des
composants. Le proxy et ses services sont séparés par domaine, avec leurs
images et leurs tests.

```text
llm-proxy/
├── docker-compose.yml                    pile de base : le proxy et SearXNG
├── docker-compose.override.example.yml   le facultatif : exécuteur de code, adaptations à la machine
├── .env.example                          les secrets
├── data/       configuration et statistiques du déploiement (non versionnées),
│               config.example.toml, le modèle
├── docs/       la documentation et ses captures
├── proxy/      le proxy : Dockerfile, requirements*.txt, main.py, llm_proxy/, tests/,
│               wasm/ (l'outillage Node de la page : build Vite, tests, types, runtimes des bacs),
│               linux/ (la VM Linux du navigateur : invité Alpine, pilote, état démarré)
├── services/   les services tiers de la pile
│   ├── executor/   l'exécuteur de code : Dockerfile, executor/, sandbox/, tests/
│   └── searxng/    settings.yml, monté dans l'image officielle
└── bench/      les bancs de vrais clients, en conteneurs jetables
```

Commandes de développement :

- `proxy/` : `pytest`, `uvicorn main:app` ;
- `proxy/wasm/` :
  - `npm ci && npm run build` écrit les runtimes des bacs dans
    `llm_proxy/ui/web/static/vendor/`, puis construit la page (Vite) dans
    `llm_proxy/ui/web/dist/`, que le proxy sert ; tous deux non versionnés,
    l'image les construit elle-même ;
  - `npm run dev` sert la page sur `http://localhost:5173/ui/static/`, avec
    rechargement à chaud des `.vue`, et relaie le reste (`/ui/…`, `/v1/…`,
    `/hooks/…`, `/healthz`) au proxy local (`LLM_PROXY_URL`,
    `http://127.0.0.1:8000` par défaut) ;
  - `npm run check` vérifie les types du code de la page (vue-tsc, fichiers
    en `// @ts-check`), le registre de ses outils, ses tests (Vitest,
    `wasm/tests/`) et son build, dans un dossier temporaire — aussi lancé
    par pytest ;
- `proxy/linux/build.sh` : la VM Linux du navigateur, par Docker, dans
  `vendor/linux/` (voir [La VM Linux](linux.md)) ;
- `services/executor/` : `pytest` ;
- `bench/`, qui a son propre compose : `docker compose run --rm <client>`.

`docker compose` se lance depuis la racine. L'image du proxy s'y
construit aussi (`context: .`, `dockerfile: proxy/Dockerfile`), car elle
embarque `data/config.example.toml`.

`data/` est le seul dossier écrit à l'exécution (`config.toml`,
`stats.db`, bases SQLite). C'est le volume à monter.

## Le relais

Le relais traite les requêtes selon les étapes suivantes.

1. **`Call`** porte le backend, le modèle demandé, l'endpoint et le dialecte
   du client. Il écrit une seule ligne de statistiques, quelle que soit
   l'issue, et construit les erreurs au format attendu : `{"error": {…}}` ou
   `{"type": "error", …}` pour Anthropic.
2. **Porte (`gate`).** `share_gate` applique d'abord le partage équitable :
   un client qui dépasse sa part attend qu'elle se libère. Pour un backend
   `quotas = true`, `QuotaState` (`quotas.py`) réserve ensuite une place et la
   range dans `Call.tickets`. Après la réponse, la fenêtre est recalée sur
   l'`usage` réel. Le flux de `/v1/messages` (`pinged_stream`) ne renvoie pas
   une réponse HTTP par `gate` ; il applique les mêmes attentes et envoie des
   événements `event: ping`.
3. **Relais (`forward`).** Le proxy transmet la requête au backend. Le flux
   de réponse est traité par des composants qui l'observent ou le
   transforment :
   - `stats.UsageCollector` laisse passer le flux et lit l'`usage` ;
   - `chat_api.Translator` traduit les outils hébergés pour `/v1/chat/completions` ;
   - `anthropic_api.Translator` convertit le flux pour un client Anthropic ;
   - `responses_api.Translator` convertit le flux pour un client Responses.

   Les trois traducteurs héritent de `surfaces/robinet.py`, qui gère le
   découpage SSE, l'usage du dernier tour et l'agrégation des tours. Si
   l'upstream interrompt le flux, leur méthode `fail` (ou un chunk d'erreur
   en relais direct) renvoie une erreur et compte `502`. Si le client ferme
   la connexion pendant la réponse, le code `499` est compté avec les tokens
   déjà générés. Certains clients ferment la connexion dès la fin de la
   réponse, avant que l'upstream ait clos son flux : ce n'est pas compté
   `499` (`complete` du robinet : `[DONE]` vu par `UsageCollector`, réponse
   close par un traducteur).
4. **Outils hébergés.** Les traducteurs partagent le contrat `pending`,
   `resolve`, `next_turn`, `finalize`, `fail`. `hosted_loop` exécute les
   outils et relance le backend pour les trois surfaces.
   `endpoints.start_hosted` ouvre le premier tour et sélectionne le relais
   d'erreur, le flux ou la réponse JSON complète (`relaying.drain` arrête la
   boucle si le client se déconnecte).

`gate` et `forward` servent toutes les routes ; seul `pinged_stream` a
son propre chemin, sur les mêmes attentes.

### Sens des dépendances

`core/` ne dépend de rien d'autre du proxy. Puis, de bas en haut :
`access/` et `storage/` ; `upstream/` (les backends) ; `tools/` (qui
appellent des backends) ; `surfaces/` et `exposure/` ; `ui/`. Un paquet
n'importe que des paquets plus bas. Deux écarts assumés : `storage/backup`
lit `access/accounts` (il sauvegarde sa base), et `surfaces/endpoints`
lit `ui/page_tools`, une feuille qui ne dépend que de `core/` (les outils
de la page refusés par la configuration).

### Un seul processus

Les fenêtres du partage équitable et des limiteurs, les mémoires de résultats,
les fichiers rendus et les caches résident dans la mémoire du processus. Un
répertoire de données ne peut donc être utilisé que par un processus.
`startup.single_process` prend un verrou exclusif sur `stats.db.lock` ; un
second processus (`--workers`, `WEB_CONCURRENCY`) s'arrête au démarrage. La
configuration est lue à l'import et stockée dans des constantes de module.
`core/reload.py` recharge à chaud une liste blanche (`SAFE`) par signal SIGHUP
ou `POST /ui/config/reload`. Le rechargement réévalue les expressions
d'origine dans le code source et met à jour leurs copies (`from … import`).
Les autres réglages nécessitent un redémarrage.

## Fichiers

### Racine, données, services

| Fichier | Rôle |
|---|---|
| `AGENTS.md` | Les règles pour qui modifie le dépôt (agent ou personne) : code, documentation, tests (`CLAUDE.md` y renvoie) |
| `docker-compose.yml` | La pile de base, versionnée : le proxy et SearXNG |
| `docker-compose.override.example.yml` | Modèle de `docker-compose.override.yml` (non versionné, fusionné par Compose) : l'exécuteur et son réseau interne, les adaptations à la machine. Voir [Le compose de base et son override](execution-de-code.md#le-compose-de-base-et-son-override) |
| `data/config.example.toml` | Modèle de configuration documenté : chaque réglage, sa valeur par défaut |
| `docs/outils.md` | Le contrat des outils hébergés, et comment en écrire un |
| `services/executor/` | L'exécuteur de code : `entrypoint.sh`, `containers/` (podman sans root), `executor/` (`sandbox.py` : un bac par conversation, `server.py` : l'API, `validate.py`, `egress.py` : la sortie réseau, CONNECT:443 sur liste), `sandbox/` (contenu d'un bac : `check.py`, `forward.py`), `tests/` (faux podman) |
| `services/searxng/settings.yml` | Réglages de SearXNG : ses défauts plus le format JSON. Monté en lecture seule |
| `bench/` | Validation avec de vrais clients en conteneurs jetables, scénarios PASS/FAIL. Voir [bench/README.md](../bench/README.md) |

### Le proxy : l'image et l'assemblage

| Fichier | Rôle |
|---|---|
| `proxy/Dockerfile` | L'image du proxy. Contexte de build à la racine (`docker build -f proxy/Dockerfile .`), borné par `.dockerignore` |
| `proxy/requirements.txt`, `requirements-dev.txt` | Dépendances du proxy ; celles de ses tests |
| `proxy/main.py` | Point d'entrée (`uvicorn main:app`), trois lignes |
| `proxy/tests/` | Tests du proxy, sans réseau (`cd proxy && pytest`, `pytest.ini`) |
| `extension.py` | Le contrat public des extensions : réexporte `Tool`, `BrowserTool`, `Family`, `Result`, `Call`, `ToolError`, `Offer`, `Runs`… et `net.download`, `net.post` ; `settings(nom)`, `logger(nom)`, `API` |
| `app.py` | L'application FastAPI : routes et middlewares, catch-all en dernier |
| `startup.py` | Démarrage et arrêt : stats, clients upstream, partage équitable, serveurs MCP, journal, limites des backends à quotas |

Le code applicatif se trouve dans `proxy/llm_proxy/`, réparti en paquets par
domaine. Chaque paquet possède un `__init__.py` qui décrit son rôle. Les
chemins ci-dessous sont relatifs à `proxy/llm_proxy/`.

Les modules les plus volumineux sont divisés en sous-modules. Leur
`__init__.py` réexporte les symboles utilisés par le reste du proxy ; les
appels passent par le paquet, par exemple `responses_api.to_chat`. Les tests
qui remplacent un réglage de paquet utilisent `fakes.everywhere()` pour
remplacer aussi les copies importées dans ses sous-modules.

### `core/` : le noyau

| Fichier | Rôle |
|---|---|
| `core/config.py` | Lecture de `data/config.toml` : `${VAR}`, accès typés ; absent, le mode installation (`SETUP`) |
| `core/settings.py` | La table `[proxy]` en constantes typées |
| `core/httputil.py` | Forme des erreurs (OpenAI ou Anthropic), lecture des corps JSON, en-têtes propres d'un backend (`backend_headers`, `merge_headers`) |
| `core/identity.py` | Le client : condensé stable de sa clé (`owner`), secret `data/client.secret` |
| `core/limits.py` | La forme des règles `limits` d'un backend à quotas, du TOML comme d'un compte |
| `core/efforts.py` | Les niveaux de réflexion : celui de `chat_template_kwargs` (`template_effort`), la table `effort_map` d'un backend (`effort_map`, `remap`), celle de ses `model_efforts` (`model_efforts`) |
| `core/reload.py` | Le rechargement à chaud : réglages rechargeables (`SAFE`), réévalués et propagés à leurs copies, tout ou rien |
| `core/config_file.py` | L'écriture de `data/config.toml` par la page : `diff` (le fichier de la page contre le fichier brut, secrets masqués et défauts écartés), `update` (tomlkit, valeur par valeur, sous verrou, empreinte `base`, copie dans `config-history/`), `restore`, `migrate` (les anciens `prompts.json` et `extensions.json`, une fois) |
| `core/atomic.py` | Écrire un fichier d'un coup (temporaire, puis `os.replace`) |
| `core/sqlite_store.py` | Une base de magasin (comptes, mémoire, conversations) : une connexion par processus, schéma posé une fois, opérations sous verrou |
| `core/config_docs.py` | La description de chaque réglage, lue dans `config.example.toml` (montrée par Configuration → Proxy, exigée par un test) |
| `core/multipart.py` | Champ `model` d'un corps multipart : lu pour router, préfixe retiré |

### `access/` : qui parle au proxy

| Fichier | Rôle |
|---|---|
| `access/auth.py` | Clés (TOML, comptes), session signée de la page, middleware d'auth |
| `access/accounts.py` | Comptes et clés gérées (`data/accounts.db`) : création, révocation, rôles, part de chacun, modèles permis (`MODELS`, posés par `auth.identify`) |
| `access/oidc.py` | La connexion de la page par OIDC (Authlib) |
| `access/fairshare.py` | Partage équitable : une part de tokens par client sur une heure glissante, puis attente |

### `upstream/` : les backends

| Fichier | Rôle |
|---|---|
| `upstream/backends.py` | Backends, clients HTTP, routage au préfixe de modèle ; les backends des comptes (`refresh`, `visible` ; `served` : ceux du TOML et les partagés, pour les outils hébergés et le partage équitable ; `watch_quotas` : la tâche de fond d'un backend à quotas, du TOML comme d'un compte) ; les modèles exposés d'un backend (`Backend.exposes`, réglage `models`), les modèles permis du compte (`permitted`, `no_route` : `404 model_not_found`, `403 model_not_allowed`) |
| `upstream/quotas.py` | Le limiteur des backends à quotas : fenêtres minute et jour, buckets (compte, règles `limits`, en-têtes, familles, générique), `settle`, `observe` (plafonds annoncés ; restant : `Window.announce`) |
| `upstream/albert.py` | Les limites du compte Albert : `/v1/limits`, un bucket par routeur, alias compris (`refresh`) |
| `upstream/mistral.py` | Le forfait mensuel d'un compte Mistral : `/v1/billing/budget` (`refresh`), lu par `backends.read_for_page` pour *Configuration → Quotas* |
| `upstream/rate_headers.py` | Les limites annoncées dans les en-têtes d'une réponse (Mistral, OpenAI, Groq, Anthropic) : `parse`, appliquées par `QuotaState.observe` depuis `send_upstream`, sur le bucket de la requête |
| `upstream/anthropic.py` | Le backend Anthropic natif (`api = "anthropic"`) : chat/completions ↔ Messages, flux compris |
| `upstream/openai.py` | La réponse de chat d'un backend `openai` dont le `content` est une liste de morceaux (Mistral) ramenée à une chaîne et à `reasoning_content` (`normalize`, `Upstream`) ; l'enveloppe des adaptateurs de réponse (`Adapted`) |
| `upstream/relaying.py` | Le relais : `Call`, porte (`gate`, `share_gate`), le corps ajusté au backend (`inject_tool_choice`, `cap_max_tokens`, `fit_fields`), `forward`, `relay` (et la durée d'un flux, `generation` des statistiques), `drain` |
| `upstream/catalog.py` | Catalogue fusionné et préfixé (servi par `/v1/models`), type, contexte et niveaux de réflexion annoncés (`_announced_efforts`) de chaque modèle |

### `surfaces/` : les API de conversation

| Fichier | Rôle |
|---|---|
| `surfaces/endpoints.py` | `/v1/chat/completions`, `/v1/messages` (et `count_tokens`), `/v1/responses`, `/v1/models` ; `start_hosted`, `pinged_stream` |
| `surfaces/robinet.py` | La base commune des trois `Translator` |
| `surfaces/chat_api/` | Outils hébergés sur `/v1/chat/completions` : une seule réponse pour plusieurs tours upstream. Paquet : `common`, `declare`, `memory` (échanges cachés), `stream` (le `Translator`) |
| `surfaces/responses_api/` | Traduction Responses ↔ chat/completions, flux compris ; outils hébergés ; `namespace` aplatis. Paquet : `common`, `request`, `response`, `stream` (le `Translator`) |
| `surfaces/anthropic_api/` | Traduction Messages ↔ chat/completions, flux compris ; `model_map` ; outils serveur `web_search` et `web_fetch`. Paquet : `common`, `request`, `response`, `stream` (le `Translator`) |
| `surfaces/tool_loop.py` | La boucle des outils hébergés, commune aux trois surfaces |
| `surfaces/omarchy.py` | Les modèles vus par `omarchy models` : la liste `/local-ai/models.json`, `/local-ai/<id>/v1/chat/completions` relayé par `endpoints.chat` (`recipe_id` : nom préfixé → identifiant de recette) ; la vitesse de génération publiée (`decode_tok_s`), relue au plus une fois par `SPEEDS_TTL` |
| `surfaces/relay_routes.py` | `GET /v1/audio/voices` et le catch-all (`forward_post_paths`, sinon `404`) ; l'audio pas à pas : [Voix](voix.md) |

### `tools/` : les outils hébergés

| Fichier | Rôle |
|---|---|
| `tools/__init__.py` | Le commun : registre, exécution bornée (délai, taille, appels par réponse), stats, mémoire des résultats |
| `tools/contract.py` | Le contrat de tous les outils : `Tool` (`runs` : proxy ou navigateur ; `offered` : api ou web), `BrowserTool`, `Family`, `Result`, `Call`, `ToolError`, codes d'erreur, liaisons aux protocoles, `ToolName` et `FamilyKey`. Voir [outils.md](outils.md) |
| `tools/net.py` | Garde-fou réseau (`[tools.net]`) : domaines, adresses publiques seulement, redirections vérifiées ; `net.download` (avec, pour une forge, des en-têtes limités à son origine), et `net.post` (un webhook sortant hors Apprise, un `response_url`) |
| `tools/webcache.py` | Cache web en mémoire vive, quelques minutes, borné |
| `tools/html_text.py` | HTML → texte lisible par un modèle |
| `tools/web_search.py` | `web_search` : requête à SearXNG, résultats numérotés, filtre par domaines |
| `tools/web_fetch.py` | `web_fetch` : lecture d'une page ou du texte d'un PDF, tailles bornées |
| `tools/web_grep.py` | `web_grep` : expression régulière dans une page lue, avec contexte et offset |
| `tools/pattern.py` | Une expression régulière écrite par le modèle, lue dans le proxy (`web_grep`) : bibliothèque `regex`, délai et longueur bornés ; l'extrait autour d'une correspondance |
| `tools/ocr.py` | `ocr` : texte d'une image ou d'un PDF scanné, par un modèle de vision |
| `tools/transcribe.py` | `transcribe` : transcription d'un fichier audio par un backend, texte découpé et caché |
| `tools/code_execution.py` | `code_execution` : client de l'exécuteur, texte et fichiers produits ; `execute`, l'appel à l'exécuteur, commun avec `repository` |
| `tools/image_generation.py` | `image_generation` : génération ou retouche par un backend, image rendue en fichier |
| `tools/memory.py` | `memory` : `view`, `create`, `str_replace`, `insert`, `delete`, `rename` |
| `tools/repository.py` | `repository` : lire un projet d'une forge (`tree`, `read`, `grep`). Le proxy télécharge l'archive (par `net.download`, ou `git fetch` + `git archive`) et la garde compressée ; un bac de l'exécuteur la décompresse et la lit |
| `tools/repository_bac.py` | Le script que `repository` fait tourner dans le bac : décompression sûre, `tree`, `read`, `grep` (un texte envoyé à l'exécuteur, pas un module importé) |
| `tools/webhook.py` | `webhook` : poste sur un webhook sortant du compte ; l'envoi (`post` : Apprise, sinon `send` en JSON par `net.post`) ; le format Slack dans les deux sens (`to_mrkdwn`, `from_slack`) |
| `tools/provenance.py` | Les sites connus d'une conversation : ceux qu'un outil qui lit une URL peut ouvrir |
| `tools/extensions.py` | Le chargement des extensions de `[extensions].path` (un sous-dossier chacune : `<nom>.py`, `<nom>.js`), appelé par `startup.lifespan` avant les serveurs MCP : dossiers et fichiers hors d'atteinte du proxy exigés, outils et familles tous vérifiés puis enregistrés (`register`, `register_family`), une extension en faute écartée entière au journal ; `active`, `listing`, `switch` : l'interrupteur de la page ; `module_url`, `script` : le `.js` servi |
| `prompts/` | Le catalogue des prompts ([Prompts](prompts.md)) : `tools.toml`, `page.toml`, `chat.toml`, `mcp.toml` (les textes par défaut, en anglais) ; `__init__.py` : `text(clé, **variables)`, `for_page` (les gabarits que la page remplit, `chat.prompts` de `/ui/session`), `ref`/`resolve` pour les descriptions d'un schéma, `template`, `CATEGORIES` (les catégories de la page, les essentielles montrées par défaut), le formatage restreint aux variables `{nom}`, la validation ; les modifications de l'admin dans `[prompts]` de `config.toml`, écrites par `core/config_file.py` |
| `tools/page/` | Les déclarations des outils natifs du navigateur (`BrowserTool`) et de leurs familles (`Family`) : `page.py`, `device.py`, `sandbox.py`, dans l'ordre du menu Outils ; leur exécution est dans `ui/web/static/chat/tools/` |
| `tools/mcp/` | Client MCP (Streamable HTTP) : outils des serveurs configurés enregistrés comme outils hébergés. Paquet : `protocol`, `tool` (`McpTool`), `server`, `registry` |

### `exposure/` : les outils offerts tels quels

| Fichier | Rôle |
|---|---|
| `exposure/mcp_server.py` | Serveur MCP (`POST /mcp`), sans état, sur les outils hébergés actifs |
| `exposure/tool_routes.py` | `/v1/tools`, `/mcp`, fichiers rendus ; appels directs bornés par client (`[tools].direct_calls_per_minute`) |

### `storage/` : ce que le proxy garde dans `data/`

| Fichier | Rôle |
|---|---|
| `storage/stats/` | Compteurs SQLite (par requête, par exécution d'outil), lecture de l'`usage` dans le flux. Paquet : `collector` (`UsageCollector`), `db` (SQLite, file d'écriture), `usage` (Usage API, vitesse de génération par modèle : `decode_speeds`) |
| `storage/conversations.py` | Historique du chat de la page (`data/conversations.db`) : conversations et images par compte |
| `storage/memories.py` | Magasin de la mémoire persistante (`data/memory.db`), par client, sous `/memories` |
| `storage/voices.py` | Les voix clonées de la page, par compte (table à part de `conversations.db`) |
| `storage/owned.py` | Ce que partagent les magasins par compte de `conversations.db` (webhooks, sources, backends) : identifiants, noms (uniques sans casse), adresses, bornes, colonnes ajoutées |
| `storage/backends.py` | Les backends de chaque compte, à soi ou partagés avec sa clé (clé gardée, jamais rendue ; table à part de `conversations.db`) |
| `storage/repos.py` | Les sources de dépôts de chaque compte (jeton gardé, jamais rendu ; table à part de `conversations.db`) et celles de `[repos.sources]` |
| `storage/webhooks.py` | Les webhooks entrants (jeton gardé, retrouvé par son condensé) et sortants (URL Apprise et son service, exposition à l'outil) de chaque compte (tables à part de `conversations.db`) |
| `storage/files.py` | Fichiers rendus par les outils : en mémoire, bornés, servis par `GET /v1/files/<jeton>/<nom>` sans clé |
| `storage/backup.py` | Sauvegarde et restauration de `data/` |

### `ui/` : le tableau de bord et le chat

| Fichier | Rôle |
|---|---|
| `ui/dashboard/` | Routes `/ui/*` : entrée, session, comptes et clés, chat, mémoire. Paquet : `page`, `accounts`, `chat`, `memory`, sur un routeur commun |
| `ui/static_files.py` | Les fichiers de la page : le build de Vite (`web/dist/`, ses `assets/` à empreinte gardés un an) et `vendor/` sous `/ui/static/` ; `/ui` sert `dist/index.html`, une page d'erreur sans build |
| `ui/conversation_routes.py` | `/ui/conversations`, `/ui/assets` : l'historique du chat (`storage/conversations.py`) |
| `ui/config_view.py` | La configuration effective pour la page (secrets masqués), le formulaire, la vérification d'un TOML modifié, son enregistrement (`/ui/config/save`, par `core/config_file.py`) et le rechargement |
| `ui/usage_api.py` | L'Usage API (`/v1/organization/usage/*`) et ce qu'une page connectée en voit |
| `ui/agent_usage.py` | `/v1/usage/agent` et `/ui/usage/agent` (la vue Usage) : l'usage et les limites d'un compte au format du panneau d'agents d'Omarchy (`stats.slots`, part équitable, forfaits) |
| `ui/health.py` | `/healthz` et `/ui/health` : état détaillé du déploiement |
| `ui/page_tools.py` | Les outils du navigateur pour la page : leurs déclarations (`tools.PAGE`, `tools.FAMILIES`) servies par `/ui/session`, moins celles que refuse `[ui].page_tools_disabled` ou d'une extension désactivée, retirées aussi de toute requête du chat de la page ; les familles `authored` (provenance) |
| `ui/linux_relay.py` | Le relais réseau (Wisp) de la VM Linux : jeton signé, adresses publiques seulement, plafond d'octets et délai d'inactivité |
| `ui/voice_routes.py` | Routes `/ui/voices` (voix clonées) |
| `ui/repo_routes.py` | `/ui/repos` : les sources de dépôts du compte |
| `ui/backend_routes.py` | `/ui/backends` : les backends du compte, et ceux que d'autres partagent ; `/ui/backends/{id}/models`, son catalogue entier pour cocher ses modèles exposés |
| `ui/runtime_routes.py` | `/ui/runtime` : les bacs de l'exécuteur (les siens ; tous pour un admin) et les caches du proxy, à détruire ou vider |
| `ui/extension_routes.py` | `/ui/extensions` : les extensions chargées et écartées, l'interrupteur à chaud (admin) ; `/ui/extensions/<nom>/<nom>.js`, le module de la page d'une extension dont un outil du navigateur est actif |
| `ui/prompt_routes.py` | `/ui/prompts` : le catalogue des prompts et les textes modifiés ; `PUT`/`DELETE /ui/prompts/{clé}` (admin) |
| `ui/webhook_routes.py` | `POST /hooks/<jeton>` (un message au format Slack, « ok » tout de suite) et `/ui/webhooks` (la gestion par la page) |
| `ui/webhook_turn.py` | Le tour d'un message reçu, sans navigateur : la conversation, le seul message reçu et le prompt système envoyés au modèle, `/ui/chat/completions` appelé en interne au nom du créateur, les envois sortants |
| `ui/dataurl.py` | Lecture des URL `data:` envoyées par la page (images, audio) |
| `ui/web/static/` | Les sources de la page, racine de Vite (`proxy/wasm/vite.config.mjs`) : `index.html` (l'entrée), `main.js` (monte l'application, enregistre le chat et la mémoire), `App.vue` (session, vues, menu), composants `.vue` et modules ES par domaine — `stats/` (Statistiques, Usage), `chat/`, `config/` (une partie par fichier), `memory/`, `lib/` (appels, formats, fichiers, Markdown, prompts, thème) —, `dashboard.css`, `public/` (manifeste, icônes, copiés tels quels). Vue 3 (avec son compilateur de gabarits, pour les cartes des extensions) et VueUse 15 sont des imports (`vue`, `@vueuse/core`) |
| `ui/web/dist/` | Le build de la page (`npm run build`), non versionné |
| `ui/web/static/chat/` | `ChatPanel.vue`, `api.js` (les messages envoyés au modèle, règle « ajout seul »), `turn.js` (les étapes d'un tour et leur chronologie d'affichage), `store.js`, `stream.js`, `attachments.js`, `images.js` ; `tools/`, les outils de la page (voir ci-dessous) ; `workspace.js` (le dossier ouvert), `artifacts.js` (le rendu isolé d'un artifact, et son pont de débogage ; pour un projet, l'aperçu d'un HTML avec le CSS et le JS du projet mis en ligne, la source colorée d'un fichier, l'archive `.zip`), `artifact-bridge.js` (la page ↔ l'artifact affiché), `models/`, `diff.js`, `ArtifactPanel.vue`, `LinuxPanel.vue` (le terminal de la VM Linux), `SidePanel.vue` (leur panneau commun : largeur, Large, Réseau), `voice/` (la voix : moteurs navigateur et proxy, voix clonées et leur studio, normalisation de la référence, transcription des modèles ASR), `notify.js` |
| `ui/web/static/chat/tools/` | L'exécution des outils du navigateur (leurs déclarations : `tools/page/` du proxy) : `contract.js` (les listes `TOOL` et `FAMILY`, l'interface `PageTool`), `index.js` (le registre, qui vérifie chaque outil au chargement et y installe les déclarations de `/ui/session`, et les modules des extensions), `page/` (questions, fichiers, artifacts, dossier), `device/` (l'appareil ; `vueuse.js`, les composables VueUse des outils ; `media.js`, les flux), `sandbox/` (`bac.js`, le bac commun ; `files.js` ; `javascript.js`, `typescript.js`, `php.js`, `sql.js`, `postgres.js`, `sqlbase.js`, `tables.js` ; `linux.js` et `linuxvm.js`, la VM Linux ; `programs.js`, le Code Mode, dont les `tools.x()` passent par la page et `/ui/tools`) ; `text.js`, lire, chercher et retoucher un texte (artifacts et fichiers de la VM). Voir [Écrire un outil de la page](navigateur.md#écrire-un-outil-de-la-page) |
| `ui/web/static/vendor/` | Construit par `proxy/wasm/`, non versionné : `php.js`/`php.wasm` (PHP 8.3, @php-wasm), `sql.js`/`sql.wasm` (SQLite, sql.js), `chart.js` (Chart.js), `html-to-image.js` (la capture d'un artifact), `highlight.js` (la coloration d'un artifact de code), `sucrase.js`, `typescript.js` (le compilateur TypeScript 6 et ses `lib.*.d.ts`), `pglite.*` et `initdb.wasm` (PostgreSQL, PGlite), `xterm.*` (le terminal) ; `linux/`, la VM Linux, construite par `proxy/linux/build.sh` |
