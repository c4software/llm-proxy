# Bancs de validation

Cinq bancs valident le proxy avec de vrais clients, chacun dans un conteneur jetable.
Chaque banc joue une série de scénarios, une fois pour chaque modèle de `MODELS`, et imprime `PASS`, `FAIL` ou `SKIP` pour chacun.
Rien n'est installé sur l'hôte. `~/.claude`, `~/.pi`, `~/.codex` et `~/.omp` ne sont jamais lus ni écrits : chaque client a sa configuration dans l'image, et son dossier de travail disparaît avec le conteneur.

| Banc | Client | Surface du proxy | Scénarios |
|---|---|---|---|
| `claude` | [Claude Code](https://www.npmjs.com/package/@anthropic-ai/claude-code) | `/v1/messages`, traduite ([API Anthropic](../docs/api-anthropic.md)) | 14 |
| `pi` | [pi](https://pi.dev) | `/v1/chat/completions`, relais brut ; `/mcp` au scénario 6 | 6 |
| `codex` | [Codex CLI](https://www.npmjs.com/package/@openai/codex) | `/v1/responses`, traduite ([API Responses](../docs/api-responses.md)) ; `/mcp` au scénario 10 | 10 |
| `omp` | [oh-my-pi](https://github.com/can1357/oh-my-pi), fork de pi | `/v1/chat/completions`, par l'extension de son provider ; `/mcp` au scénario 6 | 6 |
| `api` | aucun agent : requêtes HTTP en Python | outil hébergé déclaré sur `/v1/chat/completions`, `/v1/tools`, `/v1/responses`, Usage API | 8 |

## Prérequis

- Docker et Docker Compose.
- Un proxy joignable : celui de la racine du dépôt (`docker compose up -d` à la racine), ou un proxy déjà déployé (voir [Viser un proxy distant](#viser-un-proxy-distant)).
- Des modèles servis par ce proxy, de préférence **sans quota** : chaque appel Claude Code porte ~20 k tokens d'entrée (voir [Coût et durée](#coût-et-durée)).
- Pour les scénarios d'outils web : `web_search`, `web_fetch` et `web_grep` actifs sur le proxy, et `[mcp_server] enabled = true` pour les scénarios MCP. Sinon, ces scénarios sont sautés (voir [Lire le résultat](#lire-le-résultat)).

## Lancer

```sh
cd bench
cp .env.example .env              # PROXY_URL, PROXY_API_KEY, MODELS, SMALL_MODEL
docker compose run --rm claude    # Claude Code
docker compose run --rm pi        # pi
docker compose run --rm codex     # Codex CLI
docker compose run --rm omp       # omp
docker compose run --rm api       # requêtes HTTP directes
```

La première commande de chaque banc construit son image. La commande sort en erreur si un scénario échoue.

Pour essayer un client à la main, avec la même image et la même configuration :

```sh
docker compose run --rm claude claude          # Claude Code interactif
docker compose run --rm claude claude -p "…"   # une question
docker compose run --rm pi pi                  # pi interactif (/model pour changer de modèle ou de provider)
```

### Variables

Dans `.env` (copié de `.env.example`, ignoré par git) :

| Variable | Rôle |
|---|---|
| `PROXY_URL` | Le proxy vu des conteneurs, **sans `/v1` ni barre finale**. Par défaut `http://127.0.0.1:8000`. Se change dans `.env`, pas par `-e` : `docker-compose.yml` en tire `ANTHROPIC_BASE_URL` pour Claude Code |
| `PROXY_API_KEY` | Une clé de `proxy.api_keys`, ou n'importe quoi devant un proxy ouvert. **Jamais vide** : Codex refuse de démarrer sur une clé vide |
| `MODELS` | Les modèles préfixés (`backend/modèle`), séparés par des espaces. Tout le jeu est rejoué pour chacun. Ce sont les noms du proxy visé (`GET /v1/models`) |
| `SMALL_MODEL` | Claude Code : modèle des tâches d'arrière-plan (titres, résumés) |

À passer par `docker compose run -e VAR=… <banc>` :

| Variable | Banc | Défaut | Rôle |
|---|---|---|---|
| `MODELS` | tous | `.env` | Un seul modèle, pour aller vite |
| `ONLY` | claude | `99` | Ne joue que les N premiers scénarios |
| `MAX_TURNS` | claude | `40` | Plafond de tours par scénario. En mode `-p`, Claude Code ne plafonne pas : un modèle qui répète le même appel d'outil tournerait sans fin |
| `MAX_TIME` | omp | `20m` | Durée au bout de laquelle omp arrête un scénario (`--max-time`) |
| `TIMEOUT` | api | `600` | Secondes sans un octet avant d'abandonner une requête |
| `LONG_URL`, `LONG_NEEDLE`, `LONG_QUESTION`, `LONG_ANSWER` | codex, api | une page de cours, `eloquent`, une question sur la suppression, `termin` | L'exemple en deux tours (scénario 8) : la page longue (plus de 20 000 caractères de texte), le mot attendu dans le résumé, la question de suivi, le mot attendu dans la réponse |

Pour voir chaque réponse du modèle dans `docker compose logs` du proxy (outils appelés, tokens) : `[anthropic] trace = true` dans son `config.toml` (voir [Configuration](../docs/configuration.md)).

### Viser un proxy distant

Dans `.env` :

```sh
PROXY_URL=http://llmproxy
PROXY_API_KEY=unused                    # ou une clé de proxy.api_keys ; jamais vide
MODELS="bigchuck/qwen3.8-flash-next"    # des modèles de CE proxy
```

Les conteneurs sont en réseau hôte (`network_mode: host`) : c'est la machine hôte qui résout `llmproxy` (DNS, `/etc/hosts`, Tailscale…). Si le nom n'existe que sur un réseau Docker, remplacer `network_mode: host` par ce réseau dans `docker-compose.yml`.

Chaque client en tire son adresse : `ANTHROPIC_BASE_URL` pour Claude Code, `${PROXY_URL}/v1` pour pi et Codex, `LLM_PROXY_URL` pour l'extension d'omp, `PROXY_URL` tel quel pour `api`.

### Docker Desktop (macOS, Windows)

Pas de réseau hôte : mettre `PROXY_URL=http://host.docker.internal:8000` dans `.env` et retirer `network_mode: host` de `docker-compose.yml`.

### Versions des clients

Les images prennent la **dernière** version de chaque client. Pour en fixer une :

```sh
docker compose build --build-arg CLAUDE_CODE_VERSION=2.1.287 claude
docker compose build --build-arg PI_VERSION=0.87.1 pi
docker compose build --build-arg CODEX_VERSION=0.157.1 codex
docker compose build --build-arg OMP_VERSION=18.3.2 omp
```

## Lire le résultat

Chaque scénario imprime une ligne `PASS`, `FAIL` ou `SKIP`, suivie de ce qu'il a vu. Puis un résumé par modèle :

```text
════ Claude Code 2.1.241 (Claude Code) → http://127.0.0.1:8000 | modèle bigchuck/qwen3.8-27b-mtp-nothink ════
1. Réponse simple (POST /v1/messages, flux SSE)
  PASS Paris
2. Outils : Write + Bash + Read (tool_use / tool_result, plusieurs tours)
  PASS hello.txt = bonjour — …
…
14. Recherche web hébergée (WebSearch → sous-requête à l'outil serveur web_search, exécutée par le proxy)
  PASS 1 recherche(s) aboutie(s) sur 1 (…) — https://github.com/ggml-org/llama.cpp/releases
Résumé :
  bigchuck/qwen3.8-27b-mtp-nothink : 14/14
Tout passe.
```

| Verdict | Sens | Code de sortie |
|---|---|---|
| `PASS` | Le scénario a vu ce qu'il attendait | 0 |
| `FAIL` | Le message dit ce qui a manqué (fichier absent, test modifié, aucun appel d'outil dans la trace, réponse sans le texte attendu…) | 1 |
| `SKIP` | Le proxy visé n'a pas ce que le scénario vérifie. Ni réussite ni échec : compté à part (`13/13, 1 sauté(s)`) | 0 si rien n'échoue |

Devant un proxy sans outils web, la fin devient :

```text
14. Recherche web hébergée (…)
  SKIP web_search n'est pas hébergé par ce proxy (/healthz : tools.enabled = [])
Résumé :
  bigchuck/qwen3.8-27b-mtp-nothink : 13/13, 1 sauté(s)
1 scénario(s) sauté(s) : outils web non hébergés par le proxy.
Tout passe.
```

**D'où vient le `SKIP`.** Avant de jouer, chaque banc lit `GET /healthz` du proxy avec `PROXY_API_KEY` : `tools.enabled`, `mcp_server.enabled`, et pour `api` `chat.hosted_tools` et `responses.enabled`. Un proxy à clés ne donne ce détail qu'à une clé valide. Avec une clé refusée, ou un proxy qui ne répond pas, la liste est vide et **tous** les scénarios web sont sautés : un `SKIP` inattendu se vérifie d'abord par la clé.

**Un `FAIL` n'accuse pas toujours le proxy.** Les scénarios agentiques jugent aussi le modèle : un modèle qui « corrige » `a - b` en autre chose que `a + b` fait échouer le scénario 3. Prendre des modèles qui suivent les instructions. Sur un scénario web, le message dit laquelle des deux vérifications a manqué (voir [Scénarios d'outils web](#scénarios-doutils-web)).

## Banc `claude` : Claude Code

Commande : `claude -p --dangerously-skip-permissions --max-turns 40 --output-format json`. Aucune question n'est posée, le champ `result` est lu. Le scénario 14 tourne en `--output-format stream-json --verbose --tools WebSearch`, pour lire la trace.

| N° | Scénario | Ce qui est vérifié |
|---|---|---|
| 1 | Réponse simple | `POST /v1/messages` en flux SSE, réponse traduite ; contient `Paris` |
| 2 | Write + Bash + Read | Plusieurs tours `tool_use` / `tool_result` ; `hello.txt` contient `bonjour` sur le disque |
| 3 | Glob + Grep + Edit | Arguments JSON fragmentés dans le flux (`input_json_delta`), plusieurs outils par tour ; `src/calc.py` corrigé (`a - b` → `a + b`) |
| 4 | Image dans un `tool_result` | Claude Code lit un `.png`. L'image est relayée si le modèle est multimodal au catalogue et le backend a `images = true`, remplacée par un texte sinon. Passe si la réponse n'est pas une erreur |
| 5 | `count_tokens` | Répond `{"input_tokens": N}` : exact via `tokenize_path` du backend, estimé sinon |
| 6 | `GET /v1/models` | Avec `anthropic-version` : forme Anthropic, le modèle joué y figure |
| 7 | Erreur au dialecte Anthropic | Corps invalide → `400` avec `{"type": "error", "error": {…}}`. Un modèle inconnu ne conviendrait pas : `default` de `[anthropic.model_map]` l'attrape |
| 8 | Création de code | Le modèle écrit `stats.js` (`mean`, `median`) et ses tests `node:assert`, les exécute ; vérifié en relançant `node test.js` et en appelant `median` |
| 9 | Corriger un bug sous test | `slugify.test.js` échoue ; le modèle corrige `slugify.js` **sans toucher au test** (contrôlé par `cksum`) |
| 10 | Refactor multi-fichiers | Le calcul dupliqué de `cart.js` / `invoice.js` passe dans `money.js` ; tests verts, `require("./money")` dans les deux fichiers |
| 11 | Lectures en parallèle | Trois fichiers lus dans un tour ; la réponse contient `5432` |
| 12 | Échappement JSON | Un `Write` avec accents, guillemets droits et antislashs : ces caractères traversent la traduction intacts (le texte peut être reformulé) |
| 13 | Bash en pipeline | Un nombre vérifiable : `3` lignes |
| 14 | Recherche web hébergée | Voir [Scénarios d'outils web](#scénarios-doutils-web) |

Les scénarios 8 à 10 ressemblent au travail réel : une dizaine de tours d'outils, des arguments volumineux, plusieurs outils par tour.

## Bancs `pi` et `omp`

Les deux bancs jouent les mêmes six scénarios, avec les mêmes prompts. Le chemin dans le proxy est le relais brut de `/v1/chat/completions` ; seul le client change.

| N° | Scénario | Ce qui est vérifié |
|---|---|---|
| 1 | Réponse simple | La réponse contient `Paris` |
| 2 | write + bash + read | `hello.txt` contient `bonjour` |
| 3 | edit | `src/calc.py` corrigé (`a - b` → `a + b`) |
| 4 | Création de code | Comme le scénario 8 de Claude Code |
| 5 | Corriger un bug sans toucher au test | Comme le scénario 9 de Claude Code |
| 6 | Recherche web par le serveur MCP du proxy | Voir [Scénarios d'outils web](#scénarios-doutils-web). Les scénarios 1 à 5 tournent sans serveur MCP |

**pi** : `pi -p --no-session --provider llm-proxy --model …`, les outils s'exécutent sans confirmation. `pi/entrypoint.sh` génère `models.json` depuis `pi/models.json.tpl`, avec une entrée par modèle de `MODELS`. Le provider `llm-proxy-anthropic` du gabarit n'est pas joué ; il reste disponible pour un essai à la main. Le scénario 6 tourne dans un second dossier d'agent, `$PI_CODING_AGENT_DIR-mcp`, qui déclare le serveur MCP du proxy, avec `--tools 'mcp__llmproxy__*'`.

**omp** : `omp --no-session --no-extensions -e /omp/extensions/llm-proxy.ts --no-skills --no-rules --no-lsp --no-title --max-time 20m --model albert/<modèle> -p "…"`, plus `--auto-approve`. Le scénario 6 ajoute `--mode json --no-tools`, dans `$PI_CODING_AGENT_DIR-mcp` comme pi.

- Le provider vient de l'extension `tools/llm-proxy.ts` de [llmsetup](https://github.com/c4software/llmsetup), celle des postes. Elle lit `GET /v1/models` du proxy et enregistre ses modèles sous le nom `albert` : d'où `albert/<modèle>`.
- L'image prend l'extension à un commit épinglé (`LLMSETUP_REV`), sha256 vérifié (`LLM_PROXY_SHA256`). `omp/install.mjs` y remplace l'adresse et la clé en dur par la lecture de `LLM_PROXY_URL` et `LLM_PROXY_API_KEY`, et s'arrête si ces lignes ont changé. `omp/entrypoint.sh` dérive ces deux variables de `PROXY_URL` et `PROXY_API_KEY`.
- Pour mettre à jour l'extension, changer ensemble les deux valeurs dans `omp/Dockerfile`, puis `docker compose build omp` :

```sh
git ls-remote https://github.com/c4software/llmsetup HEAD
curl -fsSL https://raw.githubusercontent.com/c4software/llmsetup/<commit>/tools/llm-proxy.ts | sha256sum
```

- Le binaire `omp-linux-x64` vient des [releases GitHub](https://github.com/can1357/oh-my-pi/releases), sha256 vérifié contre le `SHA256SUMS.txt` de la release. Le dossier d'agent `/omp/agent` est vide : ni configuration, ni extension découverte d'office.

## Banc `codex` : Codex CLI

Commande : `codex exec --skip-git-repo-check --ephemeral --dangerously-bypass-approvals-and-sandbox -m …`. Le bac à sable de Codex ne démarre pas dans un conteneur sans privilèges : le conteneur jetable en tient lieu. Les scénarios web tournent en `--sandbox read-only --json`, pour lire la trace et ne laisser au modèle que le proxy pour atteindre le web.

`codex/entrypoint.sh` génère `config.toml` dans `CODEX_HOME` : provider `llm-proxy`, `wire_api = "responses"`, `${PROXY_URL}/v1`, clé lue dans `PROXY_API_KEY`. Il écrit aussi `$CODEX_HOME-mcp`, la même configuration plus le serveur MCP du proxy (outils approuvés d'office, délai de 600 s), pour le scénario 10.

| N° | Scénario | Ce qui est vérifié |
|---|---|---|
| 1 | Réponse simple | `POST /v1/responses` en flux ; contient `Paris` |
| 2 | Écrire puis relire | `function_call` / `function_call_output` sur plusieurs tours ; `hello.txt` contient `bonjour` |
| 3 | Modifier un fichier | `src/calc.py` corrigé |
| 4 | Création de code | Comme le scénario 8 de Claude Code |
| 5 | Corriger un bug sans toucher au test | Comme le scénario 9 de Claude Code |
| 6 | Recherche web hébergée | `web_search` déclaré par Codex, exécuté par le proxy |
| 7 | Recherche puis lecture de page | `web_search` puis `web_fetch` dans le même tour |
| 8 | Page longue, puis question de suivi | Voir [L'exemple en deux tours](#lexemple-en-deux-tours) |
| 9 | Recherche dans une page | `web_grep`, par la boucle de `web_search` |
| 10 | Recherche par le serveur MCP du proxy | `POST /mcp`, `tools/call web_search` |

Le détail des scénarios 6 à 10 est dans [Scénarios d'outils web](#scénarios-doutils-web).

## Banc `api` : client HTTP nu

Aucun agent ne met `{"type": "web_search"}` dans les `tools` d'une requête `/v1/chat/completions` : c'est une forme propre au proxy, active avec `[chat].hosted_tools = true` (voir [Client chat/completions](../docs/outils-heberges.md#client-chatcompletions--déclarer-loutil)). Ce banc l'envoie lui-même : `api/scenarios.py`, Python et sa bibliothèque standard. Il n'exécute aucun outil.

La question est celle des autres bancs : l'URL de la page des releases de `ggml-org/llama.cpp`, « recopiée telle qu'elle apparaît dans les résultats ». La sous-chaîne attendue est `github.com/ggml-org/llama.cpp`.

| N° | Requête | Ce qui est vérifié |
|---|---|---|
| 1 | `POST /v1/chat/completions`, `tools: [{"type": "web_search"}]`, `stream: true`, `include_usage` | Un flux SSE ; **un seul `id`** ; **aucun `tool_calls`** dans les deltas ; **un seul `finish_reason`** ; au plus un bloc `usage` ; `[DONE]` une fois, en dernier ; pas de bloc `error` ; la sous-chaîne dans le texte ; **au moins une annotation `url_citation`**, avec `contenu[start_index:end_index] == url` |
| 2 | La même, `stream: false` | Un seul choix, pas de `tool_calls`, un `finish_reason`, la sous-chaîne, les annotations (`message.annotations`) aux mêmes conditions |
| 3 | La même **sans** `tools` | Relais ordinaire : `200`, un contenu, un `finish_reason`, **aucune annotation**. La réponse n'est pas jugée |
| 4 | `GET /v1/tools` | `web_search` listé, avec une description et un paramètre `query` |
| 5 | `POST /v1/tools/web_search`, `{"query": …}` | `{"name": "web_search", "result", "is_error": false}`, au moins une URL dans le résultat |
| 6 | `POST /v1/responses`, `tools: [{"type": "web_search"}]`, `stream: false` | Réponse `completed` ; un `web_search_call` **terminé**, puis un `message` ; aucun `function_call` ; la sous-chaîne |
| 7 | `GET /v1/organization/usage/tools`, lu au début puis à la fin | Par route (`/v1/chat/completions`, `/v1/tools`, `/v1/responses`), le compteur de `web_search` a avancé d'**au moins** autant que de scénarios réussis sur cette route |
| 8 | Deux `POST /v1/responses` | Voir [L'exemple en deux tours](#lexemple-en-deux-tours) |

**Ce qui prouve la recherche.** Pour 1 et 2, l'annotation : le proxy n'en pose que pour une URL qu'un outil a rendue *et* que le modèle a écrite à l'identique. Une réponse de mémoire n'en porte pas. Pour 6, l'élément `web_search_call`. Pour les trois routes, le compteur du scénario 7 ([Usage des outils hébergés](../docs/statistiques.md#usage-des-outils-hébergés)). Chaque scénario affiche déjà le sien (`compteur /v1/chat/completions : +1`) : `+0` sous « aucune annotation » dit que le modèle n'a pas cherché, `+1` qu'il a cherché sans recopier une URL des résultats.

**Sauts**, d'après `/healthz` :

| Condition | Scénarios sautés |
|---|---|
| `web_search` absent de `tools.enabled` | tous sauf 3 |
| `chat.hosted_tools` faux | 1, 2 |
| `responses.enabled` faux | 6, 8 |
| `web_fetch` absent de `tools.enabled` | 8 |
| `GET /v1/organization/usage/tools` ne répond pas (route absente, clé refusée) | 7 |

`[chat].annotations = false` sur le proxy fait échouer 1 et 2 : rien dans `/healthz` ne le dit.

## Scénarios d'outils web

Tous posent une question dont la réponse se vérifie par une sous-chaîne stable. La plupart demandent l'URL de la page des releases de `ggml-org/llama.cpp` : elle est déterminée par le nom du dépôt, et toute bonne réponse contient `github.com/ggml-org/llama.cpp` (casse ignorée).

| Banc | N° | Ce qui est demandé | Chemin dans le proxy |
|---|---|---|---|
| claude | 14 | L'URL des releases, avec `WebSearch` seul | Sous-requête `/v1/messages` avec l'outil serveur `web_search_20250305` ([outils-heberges.md](../docs/outils-heberges.md#loutil-serveur-web_search)) |
| pi | 6 | L'URL des releases, avec les seuls outils MCP du proxy | `POST /mcp`, `tools/call web_search` ([Serveur MCP](../docs/outils-heberges.md#serveur-mcp--mcp)) |
| omp | 6 | L'URL des releases, sans aucun outil intégré d'omp (`--no-tools`) | `POST /mcp`, `tools/call web_search` |
| codex | 6 | L'URL des releases | `web_search` déclaré dans `/v1/responses`, boucle du proxy |
| codex | 7 | Une recherche sur `c4software/llmsetup`, puis la ligne qui définit `PREFIXE` dans `tools/llm-proxy-web.ts` à un commit épinglé | `web_search` puis `web_fetch` dans la même boucle |
| codex | 8 | Résumer une page longue, puis une question de suivi | `web_fetch` par morceaux, puis mémoire des résultats |
| codex | 9 | La valeur de `DELAI_APPEL` dans le même fichier, avec `web_grep` | `web_grep`, rendu en action `find_in_page` |
| codex | 10 | L'URL des releases, dans `$CODEX_HOME-mcp` | `POST /mcp`, `tools/call web_search` |
| api | 1, 2, 5, 6 | Voir [Banc `api`](#banc-api--client-http-nu) | |

### Deux vérifications par scénario

La première compte le plus : **la recherche a eu lieu**, lue dans la trace du client et non dans sa réponse. Un modèle peut écrire l'URL d'un dépôt connu de mémoire.

| Client | Preuve dans la trace |
|---|---|
| Claude Code (`stream-json`) | Un `tool_use` de `WebSearch`, suivi d'un `tool_result` sans erreur et portant au moins une URL. Une recherche en échec y laisse `Web search error: …`, sans lien |
| pi, omp (`--mode json`) | Un `tool_execution_end` de l'outil MCP (`mcp__llmproxy__web_search` pour pi, `mcp__llmproxy_web_search` pour omp), sans erreur, portant au moins une URL ; **aucun autre outil exécuté** (ni le `web_search` intégré d'omp, ni `read`, ni `bash`). En plus, le compteur de `web_search` par la route `/mcp` doit avancer d'au moins 1 ; illisible, il n'est pas exigé |
| Codex (`--json`) | Un élément `web_search` terminé ; pour 7, un second d'action `open_page` ; pour 9, une action `find_in_page` ; pour 10, un `mcp_tool_call` du serveur `llmproxy`, outil `web_search`, sans erreur |

La seconde : **la réponse est la bonne**, par sous-chaîne.

| Scénario | Sous-chaîne attendue |
|---|---|
| URL des releases | `github.com/ggml-org/llama.cpp` |
| codex 7 | `proxy_` : la ligne `const PREFIXE = "proxy_";` d'un fichier adressé par son commit, absent de la mémoire des modèles. La réponse prouve à elle seule que la page est arrivée |
| codex 9 | `70000`, comparé sans séparateurs (`70_000` et `70 000` valent pareil) |

Un échec sur la sous-chaîne alors que la recherche est prouvée met en cause le modèle ou les moteurs de recherche du jour, pas la traduction.

### Sauts

| Banc | Scénario sauté si |
|---|---|
| claude | 14 : `web_search` absent de `tools.enabled` |
| pi, omp | 6 : `web_search` absent, ou `mcp_server.enabled` faux (`[mcp_server] enabled = true` côté proxy) |
| codex | 6 : `web_search` absent ; 7 : `web_search` ou `web_fetch` absent ; 8 : `web_fetch` absent ; 9 : `web_grep` absent ; 10 : `web_search` absent ou serveur MCP inactif |
| api | voir [Banc `api`](#banc-api--client-http-nu) |

### L'exemple en deux tours

Scénario 8 des bancs `codex` et `api`. Premier tour : « résume-moi cette page », sur une page plus longue qu'un morceau de `web_fetch`. Second tour : une question dont la réponse n'est que dans la page. Le client renvoie la conversation **sans** le contenu de la page : le modèle ne peut répondre que si le proxy le lui rend ([Mémoire des résultats](../docs/outils-heberges.md#mémoire-des-résultats)).

| Tour | Ce qui est vérifié |
|---|---|
| 1 | Au moins deux morceaux contigus depuis 0, rendus `url [a, b]` ; `LONG_NEEDLE` dans le résumé |
| 2 | Aucune relecture ni recherche ; `LONG_ANSWER` dans la réponse |
| 2, `api` seul | Aucune lecture `web_fetch` comptée par le proxy ; au moins 50 % du prompt repris du cache du backend, signe que la conversation rejouée est celle qu'il a calculée |

Codex joue les deux tours dans une même session : le premier sans `--ephemeral`, le second par `codex exec resume --last`.

### Ce que ces scénarios ne disent pas

- La trace de Codex ne distingue pas une recherche fructueuse d'une recherche vide : l'élément ne porte que la requête ou l'URL. Le nombre de requêtes HTTP se lit dans les logs du proxy (`outil hébergé web_search(…) → N car.`).
- `WebFetch` de Claude Code n'est pas joué (Claude Code l'exécute lui-même, sans le proxy). `web_fetch` et `web_grep` par MCP sont offerts à pi et omp, rien ne les exige.
- pi et omp sont joués sans leurs propres outils au scénario 6 : un client avec ses outils **et** ceux du proxy n'est pas joué.
- La mémoire des échanges cachés de `/v1/chat/completions` (le tour suivant d'une conversation où le proxy a cherché) n'est pas jouée.
- Le compteur du proxy est celui du proxy entier : « au moins », jamais « exactement ». Il confirme la trace, il ne la remplace pas.
- `api` reçoit un seul bloc `usage` : le cumul sur les tours se vérifie dans `proxy/tests/test_chat_api.py`.
- Le tour mixte (outil hébergé et outil du client), le `400` d'un outil déclaré mais désactivé, `web_search_options` : seulement dans les tests du dépôt.
- Codex 7 et 9 lisent `raw.githubusercontent.com` : un proxy dont `[tools.web_fetch].allowed_domains` ne le permet pas les fait échouer. Le fichier lu est adressé par commit ; si ce commit disparaissait du dépôt public, ces scénarios seraient à repointer.

## Ce qui n'est pas vérifié ici

- Le limiteur de quotas et les `event: ping` pendant l'attente : il faut un backend à quotas et de la contention. La traduction est couverte par `proxy/tests/`.
- La qualité des réponses du modèle, au-delà des critères ci-dessus.

Pour les vérifications à la main et les tests automatiques du proxy, voir [Vérification](../docs/verification.md).

## Coût et durée

Chaque appel Claude Code porte son prompt système, ~18 à 20 k tokens d'entrée. Le banc `claude` fait 80 à 90 appels **par modèle**, soit ~0,75 à 1,5 M de tokens d'entrée selon que le modèle tâtonne. Viser des backends sans quota.

Ordre de grandeur sur un GPU local : ~40 min pour `claude` et `pi` sur un 27B dense, ~20 min sur un 35B-A3B. Les appels, tokens et latences d'un run se lisent dans l'[Usage API](../docs/statistiques.md) du proxy.

## Fichiers

| Fichier | Rôle |
|---|---|
| `.env.example` | Modèle de `.env` : `PROXY_URL`, `PROXY_API_KEY`, `MODELS`, `SMALL_MODEL`, variables facultatives |
| `docker-compose.yml` | Les cinq services, en réseau hôte |
| `claude/Dockerfile` | `node:22-slim` + `@anthropic-ai/claude-code` (`CLAUDE_CODE_VERSION`). Utilisateur non root, requis par `--dangerously-skip-permissions`. Télémétrie et mises à jour coupées |
| `claude/settings.json` | Le `~/.claude/settings.json` du conteneur : `CLAUDE_CODE_ATTRIBUTION_HEADER=0`, pour que l'en-tête d'attribution ne décale pas le préfixe et ne fasse pas manquer le cache du backend |
| `claude/scenarios.sh` | Les 14 scénarios Claude Code |
| `pi/Dockerfile` | `node:22-slim` + `@earendil-works/pi-coding-agent` (`PI_VERSION`), `PI_CODING_AGENT_DIR=/pi/agent` |
| `pi/models.json.tpl` | Les providers pi : `llm-proxy` (`openai-completions`, joué) et `llm-proxy-anthropic` (`anthropic-messages`, pour un essai à la main) |
| `pi/entrypoint.sh` | Génère `models.json`, et le dossier `$PI_CODING_AGENT_DIR-mcp` avec `mcp.json` |
| `pi/scenarios.sh` | Les 6 scénarios pi |
| `codex/Dockerfile` | `node:22-slim` + `@openai/codex` (`CODEX_VERSION`), `CODEX_HOME=/codex` |
| `codex/entrypoint.sh` | Génère `config.toml` dans `CODEX_HOME` et dans `$CODEX_HOME-mcp` |
| `codex/scenarios.sh` | Les 10 scénarios Codex |
| `omp/Dockerfile` | `node:22-slim` + le binaire omp (`OMP_VERSION`) et l'extension du provider (`LLMSETUP_REV`, `LLM_PROXY_SHA256`), `PI_CODING_AGENT_DIR=/omp/agent` |
| `omp/install.mjs` | Exécuté à la construction : télécharge et vérifie omp et l'extension, adapte l'extension aux variables `LLM_PROXY_URL` / `LLM_PROXY_API_KEY` |
| `omp/entrypoint.sh` | Pose `LLM_PROXY_URL` et `LLM_PROXY_API_KEY`, et écrit `$PI_CODING_AGENT_DIR-mcp/mcp.json` |
| `omp/scenarios.sh` | Les 6 scénarios omp |
| `api/Dockerfile` | `python:3-slim`, rien à installer |
| `api/scenarios.py` | Les 8 scénarios en requêtes HTTP |

## Dernière validation

| Date | Proxy | Clients | Modèle | Résultat |
|---|---|---|---|---|
| 15/10/2026 | `536193d` (`[mcp_server]` actif) | pi 1.3.0, omp 18.11.1, Codex CLI 0.162.1 | `silentchuck/qwen3.8-flash-next` | pi 6/6, omp 6/6, codex 10/10 |
| 05/10/2026 | `9cd08f1`, `e8ab35d` | Claude Code 2.1.287 | `bigchuck/qwen3.8-flash-next` | claude 14/14, api 7/7 (scénario 8 ajouté ensuite, joué 8/8) |
