# Architecture

Cette page décrit l'organisation du dépôt et le rôle de chaque fichier.
Elle explique aussi comment une requête est relayée vers un backend.

## Le dépôt

La racine ne porte que l'orchestration. Chaque rôle a son dossier, avec
son image et ses tests.

```text
llm-proxy/
├── docker-compose.yml                    pile de base : le proxy et SearXNG
├── docker-compose.override.example.yml   le facultatif : exécuteur de code, adaptations à la machine
├── .env.example                          les secrets
├── data/       configuration et statistiques du déploiement (non versionnées),
│               config.example.toml, le modèle
├── docs/       la documentation et ses captures
├── proxy/      le proxy : Dockerfile, requirements*.txt, main.py, llm_proxy/, tests/,
│               wasm/ (l'outillage Node de la page : runtimes des bacs, vérification des types)
├── services/   les services tiers de la pile
│   ├── executor/   l'exécuteur de code : Dockerfile, executor/, sandbox/, tests/
│   └── searxng/    settings.yml, monté dans l'image officielle
└── bench/      les bancs de vrais clients, en conteneurs jetables
```

Commandes :

- `proxy/` : `pytest`, `uvicorn main:app` ;
- `proxy/wasm/` : `npm ci && npm run build`, qui écrit les runtimes des
  bacs dans `llm_proxy/ui/web/static/vendor/` (non versionné ; l'image le fait
  elle-même), et `npm run check`, qui vérifie les types du code de la
  page (fichiers en `// @ts-check`) et le registre de ses outils (aussi
  lancé par pytest) ;
- `services/executor/` : `pytest` ;
- `bench/`, qui a son propre compose : `docker compose run --rm <client>`.

`docker compose` se lance depuis la racine. L'image du proxy s'y
construit aussi (`context: .`, `dockerfile: proxy/Dockerfile`), car elle
embarque `data/config.example.toml`.

`data/` est le seul dossier écrit à l'exécution (`config.toml`,
`stats.db`, bases SQLite). C'est le volume à monter.

## Le relais

Une requête suit toujours le même chemin.

1. **Un `Call` la porte.** Cet objet connaît le backend, le modèle
   demandé, l'endpoint et le dialecte du client. Il écrit la ligne de
   stats, une seule fois, quelle que soit l'issue. Il construit les
   erreurs à la forme attendue : `{"error": {…}}`, ou
   `{"type": "error", …}` pour un client Anthropic.
2. **La porte de quota (`gate`).** Un backend `quotas = true` passe par sa
   `QuotaState`. Le relais n'en sait pas plus : la mécanique des quotas
   vit dans `albert.py`.
3. **Le relais (`forward`)** envoie la requête au backend. Les octets de
   la réponse traversent un « robinet », qui les lit ou les réécrit au
   passage :
   - `stats.UsageCollector` laisse passer la réponse et lit l'`usage` ;
   - `anthropic_api.Translator` la réécrit pour un client Anthropic ;
   - `responses_api.Translator` la réécrit pour un client Responses.
4. **Les outils hébergés.** Les traducteurs portent aussi les outils
   hébergés, par un contrat commun (`pending`, `resolve`, `next_turn`,
   `finalize`, `fail`). Une seule boucle, `hosted_loop`, exécute les
   appels et relance le backend, pour toutes les surfaces.

`gate` et `forward` sont communs à toutes les routes.

## Fichiers

### Racine, données, services

| Fichier | Rôle |
|---|---|
| `docker-compose.yml` | La pile de base, versionnée : le proxy et SearXNG |
| `docker-compose.override.example.yml` | Modèle de `docker-compose.override.yml` (non versionné, fusionné par Compose) : l'exécuteur et son réseau interne, les adaptations à la machine. Voir [Le compose de base et son override](execution-de-code.md#le-compose-de-base-et-son-override) |
| `data/config.example.toml` | Modèle de configuration documenté, copié en `data/config.toml` au premier démarrage |
| `docs/outils.md` | Le contrat des outils hébergés, et comment en écrire un |
| `services/executor/` | L'exécuteur de code : `entrypoint.sh`, `containers/` (podman sans root), `executor/` (`sandbox.py` : un bac par conversation, `server.py` : l'API, `validate.py`), `sandbox/` (contenu d'un bac), `tests/` (faux podman) |
| `services/searxng/settings.yml` | Réglages de SearXNG : ses défauts plus le format JSON. Monté en lecture seule |
| `bench/` | Validation avec de vrais clients en conteneurs jetables, scénarios PASS/FAIL. Voir [bench/README.md](../bench/README.md) |

### Le proxy : l'image et l'assemblage

| Fichier | Rôle |
|---|---|
| `proxy/Dockerfile` | L'image du proxy. Contexte de build à la racine (`docker build -f proxy/Dockerfile .`), borné par `.dockerignore` |
| `proxy/requirements.txt`, `requirements-dev.txt` | Dépendances du proxy ; celles de ses tests |
| `proxy/main.py` | Point d'entrée (`uvicorn main:app`), trois lignes |
| `proxy/tests/` | Tests du proxy, sans réseau (`cd proxy && pytest`, `pytest.ini`) |
| `app.py` | L'application FastAPI : routes et middlewares, catch-all en dernier |
| `startup.py` | Démarrage et arrêt : stats, clients upstream, partage équitable, serveurs MCP, journal, limites des backends à quotas |

Le code est dans `proxy/llm_proxy/`, rangé en paquets par domaine. Chacun
a un `__init__.py` qui dit ce qu'il contient ; les chemins ci-dessous sont
sous `proxy/llm_proxy/`.

### `core/` : le noyau

| Fichier | Rôle |
|---|---|
| `core/config.py` | Lecture de `data/config.toml` : `${VAR}`, accès typés, création depuis l'exemple |
| `core/settings.py` | La table `[proxy]` en constantes typées |
| `core/httputil.py` | Forme des erreurs (OpenAI ou Anthropic), lecture des corps JSON |
| `core/multipart.py` | Champ `model` d'un corps multipart : lu pour router, préfixe retiré |

### `access/` : qui parle au proxy

| Fichier | Rôle |
|---|---|
| `access/auth.py` | Clés (TOML, comptes), session signée de la page, middleware d'auth |
| `access/accounts.py` | Comptes et clés gérées (`data/accounts.db`) : création, révocation, rôles, part de chacun |
| `access/oidc.py` | La connexion de la page par OIDC (Authlib) |
| `access/fairshare.py` | Partage équitable : une part de tokens par client sur une heure glissante, puis attente |

### `upstream/` : les backends

| Fichier | Rôle |
|---|---|
| `upstream/backends.py` | Backends, clients HTTP, routage au préfixe de modèle |
| `upstream/albert.py` | Spécificités d'Albert : limiteur de quotas, familles de modèles, `/v1/me/info` |
| `upstream/relaying.py` | Le relais : `Call`, porte de quota, `forward` |
| `upstream/catalog.py` | `/v1/models` fusionné et préfixé, type et contexte de chaque modèle |

### `surfaces/` : les API de conversation

| Fichier | Rôle |
|---|---|
| `surfaces/endpoints.py` | `/v1/chat/completions`, `/v1/messages` (et `count_tokens`), `/v1/responses` |
| `surfaces/chat_api.py` | Outils hébergés sur `/v1/chat/completions` : une seule réponse pour plusieurs tours upstream |
| `surfaces/responses_api.py` | Traduction Responses ↔ chat/completions, flux compris ; outils hébergés ; `namespace` aplatis |
| `surfaces/anthropic_api.py` | Traduction Messages ↔ chat/completions, flux compris ; `model_map` ; outils serveur `web_search` et `web_fetch` |
| `surfaces/tool_loop.py` | La boucle des outils hébergés, commune aux trois surfaces |
| `surfaces/relay_routes.py` | `GET /v1/audio/voices` et le catch-all (`forward_post_paths`, sinon `404`) |

### `tools/` : les outils hébergés

| Fichier | Rôle |
|---|---|
| `tools/__init__.py` | Le commun : registre, exécution bornée (délai, taille, appels par réponse), stats, mémoire des résultats |
| `tools/contract.py` | Le contrat : `Tool`, `Result`, `Call`, `ToolError`, codes d'erreur, liaisons aux protocoles. Voir [outils.md](outils.md) |
| `tools/net.py` | Garde-fou réseau (`[tools.net]`) : domaines, adresses publiques seulement, redirections vérifiées ; `net.download` |
| `tools/webcache.py` | Cache web en mémoire vive, quelques minutes, borné |
| `tools/html_text.py` | HTML → texte lisible par un modèle |
| `tools/web_search.py` | `web_search` : requête à SearXNG, résultats numérotés, filtre par domaines |
| `tools/web_fetch.py` | `web_fetch` : lecture d'une page ou du texte d'un PDF, tailles bornées |
| `tools/web_grep.py` | `web_grep` : expression régulière dans une page lue, avec contexte et offset |
| `tools/ocr.py` | `ocr` : texte d'une image ou d'un PDF scanné, par un modèle de vision |
| `tools/transcribe.py` | `transcribe` : transcription d'un fichier audio par un backend, texte découpé et caché |
| `tools/code_execution.py` | `code_execution` : client de l'exécuteur, texte et fichiers produits |
| `tools/image_generation.py` | `image_generation` : génération ou retouche par un backend, image rendue en fichier |
| `tools/memory.py` | `memory` : `view`, `create`, `str_replace`, `insert`, `delete`, `rename` |
| `tools/mcp.py` | Client MCP (Streamable HTTP) : outils des serveurs configurés enregistrés comme outils hébergés |
| `tools/render.py` | Le rendu des pages en JavaScript par browserless (`render: true` de `web_fetch` et `web_grep`) |

### `exposure/` : les outils offerts tels quels

| Fichier | Rôle |
|---|---|
| `exposure/mcp_server.py` | Serveur MCP (`POST /mcp`), sans état, sur les outils hébergés actifs |
| `exposure/tool_routes.py` | `/v1/tools`, `/mcp`, fichiers rendus |

### `storage/` : ce que le proxy garde dans `data/`

| Fichier | Rôle |
|---|---|
| `storage/stats.py` | Compteurs SQLite (par requête, par exécution d'outil), lecture de l'`usage` dans le flux |
| `storage/conversations.py` | Historique du chat de la page (`data/conversations.db`) : conversations et images par compte |
| `storage/memories.py` | Magasin de la mémoire persistante (`data/memory.db`), par client, sous `/memories` |
| `storage/files.py` | Fichiers rendus par les outils : en mémoire, bornés, servis par `GET /v1/files/<jeton>/<nom>` sans clé |
| `storage/backup.py` | Sauvegarde et restauration de `data/` |

### `ui/` : le tableau de bord et le chat

| Fichier | Rôle |
|---|---|
| `ui/dashboard.py` | Routes `/ui/*` : entrée, session, comptes et clés, chat, mémoire |
| `ui/static_files.py` | Fichiers de la page sous un chemin versionné (`/ui/static/v/<empreinte>/…`, gardés un an) |
| `ui/conversation_routes.py` | `/ui/conversations`, `/ui/assets` : l'historique du chat (`storage/conversations.py`) |
| `ui/config_view.py` | La configuration effective pour la page (secrets masqués), le formulaire, la vérification d'un TOML modifié. N'écrit jamais |
| `ui/usage_api.py` | L'Usage API (`/v1/organization/usage/*`) et ce qu'une page connectée en voit |
| `ui/health.py` | `/healthz` et `/ui/health` : état détaillé du déploiement |
| `ui/web/templates/index.html` | Squelette de la page |
| `ui/web/static/` | Modules ES par domaine : `app.js` (session, vues, menu), `stats/`, `chat/`, `config/` (une partie par fichier), `memory/`, `lib/` (appels, formats, fichiers, Markdown, thème), plus `dashboard.css`, `vue.global.prod.js` et `vueuse.iife.min.js` (VueUse 15, ses modules `shared` et `core`) |
| `ui/web/static/chat/` | `ChatPanel.js`, `store.js`, `stream.js`, `attachments.js`, `images.js` ; `tools/`, les outils de la page (voir ci-dessous) ; `workspace.js` (le dossier ouvert), `artifacts.js` (le rendu isolé d'un artifact, et son pont de débogage), `artifact-bridge.js` (la page ↔ l'artifact affiché), `models/`, `diff.js`, `ArtifactPanel.js`, `voice.js`, `notify.js` |
| `ui/web/static/chat/tools/` | Les outils que la page exécute : `contract.js` (les listes `TOOL` et `FAMILY`, l'interface `PageTool`), `index.js` (le registre, qui vérifie chaque outil au chargement), `page/` (questions, fichiers, artifacts, dossier), `device/` (l'appareil ; `vueuse.js`, les composables VueUse des outils ; `media.js`, les flux), `sandbox/` (`bac.js`, le bac commun ; `files.js` ; `javascript.js`, `typescript.js`, `php.js`, `sql.js`, `sqlbase.js`). Voir [Écrire un outil de la page](navigateur.md#écrire-un-outil-de-la-page) |
| `ui/web/static/vendor/` | Construit par `proxy/wasm/`, non versionné : `php.js`/`php.wasm` (PHP 8.3, @php-wasm), `sql.js`/`sql.wasm` (SQLite, sql.js), `chart.js` (Chart.js), `html-to-image.js` (la capture d'un artifact), `sucrase.js`, `typescript.js` (le compilateur TypeScript 6 et ses `lib.*.d.ts`) |
