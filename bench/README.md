# bench — valider le proxy avec de vrais clients

Cinq clients jetables, chacun dans son conteneur, qui tapent le proxy
et jouent des scénarios de validation — **Claude Code** (API Anthropic,
traduite par le proxy), **pi** ([pi.dev](https://pi.dev), API OpenAI),
**Codex CLI** (API Responses, traduite par le proxy), **omp**
([oh-my-pi](https://github.com/can1357/oh-my-pi), fork de pi, API OpenAI
par l'extension de son provider) et **api**, qui n'est pas un agent : un client HTTP nu
pour le chemin qu'aucun agent ne prend de lui-même, l'outil hébergé
*déclaré* sur `/v1/chat/completions`.
Chaque jeu est rejoué pour **chaque modèle** de `MODELS`. Rien n'est
installé sur l'hôte ; `~/.claude`, `~/.pi`, `~/.codex` et `~/.omp` ne sont
jamais lus ni écrits : chaque client a sa configuration dans l'image, et son
dossier de travail disparaît avec le conteneur.

**Renommage du 07/10/2026.** Ce dossier s'appelait `envTest/` ; il
s'appelle `bench/` depuis la réorganisation du dépôt en un dossier par
rôle (`proxy/`, `services/`, `bench/`). Rien d'autre n'a changé : mêmes
scénarios, mêmes images, et les résultats ci-dessous sont ceux des runs
tels qu'ils ont été joués, sous l'ancien nom pour les plus anciens. Seul
effet visible : Compose nomme le projet d'après le dossier, donc les
images construites s'appellent `bench-claude`, `bench-pi`… au lieu de
`envtest-…` — les anciennes se suppriment par
`docker image rm envtest-claude envtest-pi envtest-codex envtest-omp envtest-api`.

Les bancs `omp` et `api` ont été **écrits le 05/10/2026 et pas encore
joués** : aucun chiffre ci-dessous ne les concerne. Ce qui en a été vérifié
sans les lancer est dit dans leurs sections.

**Changement du 07/10/2026 — pi et omp, scénario 6.** L'extension
`llm-proxy-web.ts` (outils `proxy_web_search` / `proxy_web_fetch` par
`/v1/tools`, commandes `/web` et `/page`) est retirée : pi et omp
reçoivent les outils web **d'office**, par `[chat].always` du proxy, et
leur scénario 6 joue ce chemin. Les images ne téléchargent plus cette
extension. Rejoués sous cette forme le jour même (premier tableau
ci-dessous) ; les 6/6 du 05/10/2026, plus bas, sont ceux de l'ancien
scénario 6.

## Derniers résultats

Run du 7 octobre 2026, proxy `633e70f` déployé (`[chat].always =
web_search, web_fetch, ocr, transcribe, code_execution`), pi 1.0.4, omp
18.8.0, backend gufo `bigchuck`, clients en conteneurs sur la machine du
proxy (`PROXY_URL=http://llmproxy`), les deux bancs joués à la suite.

| Modèle | pi (6 scénarios) | omp (6 scénarios) |
|---|---|---|
| `bigchuck/qwen3.8-flash-next` | **6/6** | **6/6** |

Premier passage du scénario 6 par les outils d'office : lancés avec
`--no-tools`, sans extension, les deux clients ont rendu l'URL d'une
source, et le compteur d'exécutions de `web_search` du proxy sur
`/v1/chat/completions` a avancé de 1 à chaque fois. Les scénarios 1 à 5
(fichiers, édition, création de code) passent avec les cinq définitions
d'outils du proxy en plus dans chaque requête. Codex, Claude Code et
`api` n'ont pas été rejoués ce jour-là.

Run du 5 octobre 2026, proxy `9cd08f1` déployé avec son instance SearXNG
(outils `web_search` et `web_fetch` actifs), Codex CLI 0.157.1, pi 0.87.1,
Claude Code 2.1.287 (versions fixées à la construction des images),
backend gufo 0.8.0 `bigchuck`, clients en conteneurs sur la machine du
backend, proxy distant (`PROXY_URL=http://llmproxy`), les trois bancs
joués à la suite.

| Modèle | Codex (7 scénarios) | pi (6 scénarios) | Claude Code (14 scénarios) |
|---|---|---|---|
| `bigchuck/qwen3.8-flash-next` | **7/7** | **6/6** | **14/14** |

Premier passage du banc Codex (commandes réellement exécutées dans le
conteneur, à travers `/v1/responses`) et des quatre scénarios d'outils
web : chacun a vu sa recherche dans la trace du client, et Codex 7 la
lecture de la page épinglée. Aucun scénario sauté. Appels, tokens et
latences non relevés pour ce run.

Le même jour, après ajout des bancs `omp` et `api` (proxy `e8ab35d`, omp
18.3.2, mêmes backend et modèle) :

| Modèle | omp (6 scénarios) | api (7 scénarios) |
|---|---|---|
| `bigchuck/qwen3.8-flash-next` | **6/6** | **7/7** |

Un premier passage avait donné 5/6 et 5/7 : les trois échecs venaient de
SearXNG, dont les trois moteurs web actifs par défaut avaient bloqué
l'instance après une journée d'essais (limite de débit, CAPTCHA). Rejoué
après l'ajout de cinq moteurs à `services/searxng/settings.yml`. Hors banc, dans
l'image `codex` avec un catalogue de modèles déclarant `apply_patch` en
texte libre : Codex 0.157.1 a écrit `hello.txt` par l'outil `custom`.

Run du 23 août 2026, proxy `e2713da`+, Claude Code 2.1.241, pi 0.84.2,
backend llama.cpp `bigchuck` (`images = true`, `tokenize_path =
"/tokenize"`), scénarios joués à la suite sur un seul GPU.

| Modèle | Claude Code (13 scénarios) | pi (5 scénarios) | Appels | Tokens entrée / sortie | Latence moy. / max |
|---|---|---|---|---|---|
| `bigchuck/qwen3.8-27b-mtp-nothink` | **13/13** | **5/5** | 120 (85 via `/v1/messages`) | 1 471 561 / 9 608 | 17,1 s / 97,0 s |
| `bigchuck/qwen3.6-35b-a3b-mtp-nothink` | **13/13** | **5/5** | 55 (35 via `/v1/messages`) | 743 173 / 5 237 | 6,9 s / 30,0 s |

Durée totale ≈ 1 h 05 (le 27B dense prend les deux tiers). 0 erreur,
0 requête estimée — tous les comptages viennent de l'`usage` upstream.
Chiffres lus sur l'Usage API du proxy (`bucket_width=all`,
`group_by=model`), tels que le tableau de bord les montre.

## Lancer

Le proxy doit tourner (depuis la racine : `docker compose up -d`), ou être
joignable s'il est déjà déployé ailleurs — voir
[Viser un proxy distant](#viser-un-proxy-distant). Puis :

    cd bench
    cp .env.example .env        # PROXY_URL, MODELS, clé — voir le fichier
    docker compose run --rm claude    # scénarios Claude Code, pour chaque modèle
    docker compose run --rm pi        # scénarios pi (API OpenAI), pour chaque modèle
    docker compose run --rm codex     # scénarios Codex (API Responses), pour chaque modèle
    docker compose run --rm omp       # scénarios omp (API OpenAI), pour chaque modèle
    docker compose run --rm api       # requêtes directes (outil déclaré, /v1/tools, compteur), pour chaque modèle

Chaque scénario imprime `PASS` ou `FAIL` avec ce qu'il a vu ; la commande
sort en erreur si l'un échoue. Un scénario de recherche web imprime `SKIP`
quand le proxy visé n'héberge pas l'outil : ni réussite ni échec, compté à
part (`13/13, 1 sauté(s)`), et la commande ne sort pas en erreur pour autant.
Sortie attendue :

    ════ Claude Code 2.1.241 (Claude Code) → http://127.0.0.1:8000 | modèle bigchuck/qwen3.8-27b-mtp-nothink ════
    1. Réponse simple (POST /v1/messages, flux SSE)
      PASS Paris
    2. Outils : Write + Bash + Read (tool_use / tool_result, plusieurs tours)
      PASS hello.txt = bonjour — …
    …
    14. Recherche web hébergée (WebSearch → sous-requête à l'outil serveur web_search, exécutée par le proxy)
      PASS 1 recherche(s) aboutie(s) sur 1 (…) — https://github.com/ggml-org/llama.cpp/releases
    ════ Claude Code 2.1.241 (Claude Code) → http://127.0.0.1:8000 | modèle bigchuck/qwen3.6-35b-a3b-mtp-nothink ════
    …
    Résumé :
      bigchuck/qwen3.8-27b-mtp-nothink : 14/14
      bigchuck/qwen3.6-35b-a3b-mtp-nothink : 14/14
    Tout passe.

Devant un proxy sans outils web, la fin devient :

    14. Recherche web hébergée (…)
      SKIP web_search n'est pas hébergé par ce proxy (/healthz : tools.enabled = [])
    Résumé :
      bigchuck/qwen3.8-27b-mtp-nothink : 13/13, 1 sauté(s)
    1 scénario(s) sauté(s) : outils web non hébergés par le proxy.
    Tout passe.

Pour essayer à la main, même image, même configuration :

    docker compose run --rm claude claude          # Claude Code interactif
    docker compose run --rm pi pi                  # pi interactif (/model pour changer de modèle ou de provider)
    docker compose run --rm claude claude -p "…"   # une question

Variables utiles à `docker compose run -e …` : `MODELS` (un seul modèle
pour aller vite), `ONLY=5` (ne joue que les N premiers scénarios Claude
Code), `MAX_TURNS` (plafond de tours par scénario, 40 par défaut),
`MAX_TIME` (omp : durée au bout de laquelle il arrête un scénario, `20m`
par défaut), `TIMEOUT` (api : secondes sans un octet avant d'abandonner
une requête, 600 par défaut).
`PROXY_URL`, lui, se change dans `.env`. Avec
`[anthropic] trace = true` dans le `config.toml` du proxy, chaque réponse
du modèle apparaît dans `docker compose logs` (outils appelés, tokens).

### Viser un proxy distant

Une seule variable, dans `.env` : l'adresse du proxy **sans `/v1` ni barre
finale**, telle que la machine qui lance les conteneurs la joint.

    PROXY_URL=http://llmproxy
    PROXY_API_KEY=unused          # ou une clé de proxy.api_keys ; jamais vide
    MODELS="bigchuck/qwen3.8-flash-next"   # des modèles de CE proxy

Les conteneurs sont en réseau hôte : c'est la machine hôte qui résout
`llmproxy` (DNS, `/etc/hosts`, Tailscale…). Si ce nom n'existe que sur un
réseau Docker, le réseau hôte ne le verra pas : remplacer alors
`network_mode: host` par ce réseau dans `docker-compose.yml`. Chaque
client en tire sa propre adresse — `ANTHROPIC_BASE_URL` pour Claude Code
(posée par le `docker-compose.yml`, qui lit `.env` : d'où le changement
dans le fichier plutôt que par `-e`), `${PROXY_URL}/v1` pour pi et Codex,
`LLM_PROXY_URL` pour l'extension du provider d'omp, `PROXY_URL` tel quel
pour `api`.

Les images prennent la **dernière** version de chaque client. Pour
rejouer celles qui ont été validées à la main le 05/10/2026 :

    docker compose build --build-arg CLAUDE_CODE_VERSION=2.1.287 claude
    docker compose build --build-arg PI_VERSION=0.87.1 pi
    docker compose build --build-arg CODEX_VERSION=0.157.1 codex
    docker compose build --build-arg OMP_VERSION=18.3.2 omp

(Pour omp, « validée à la main » veut dire : la commande du banc, lancée
sur un poste contre le proxy déployé — pas le banc lui-même, qui n'a pas
encore été joué.)

## Fichiers

| Fichier | Rôle |
|---|---|
| `.env.example` | `PROXY_URL` (le proxy vu des conteneurs, local ou distant), `PROXY_API_KEY`, `MODELS` (préfixés, séparés par des espaces), `SMALL_MODEL` — copié en `.env`, ignoré par git |
| `docker-compose.yml` | Les cinq services, en **réseau hôte** (`127.0.0.1:8000` = le proxy de la racine) |
| `claude/Dockerfile` | `node:22-slim` + `@anthropic-ai/claude-code` (`CLAUDE_CODE_VERSION`, la dernière par défaut), utilisateur non root (requis par `--dangerously-skip-permissions`), télémétrie et mises à jour coupées |
| `claude/settings.json` | Le `~/.claude/settings.json` **du conteneur** : `CLAUDE_CODE_ATTRIBUTION_HEADER=0`, pour que l'attribution (variable d'une requête à l'autre) ne décale pas le préfixe et ne fasse pas manquer le cache du backend |
| `claude/scenarios.sh` | Les 14 scénarios Claude Code, rejoués pour chaque modèle de `MODELS` (`ANTHROPIC_MODEL` posé par le script) ; `ONLY=N` pour n'en jouer que N |
| `pi/Dockerfile` | `node:22-slim` + `@earendil-works/pi-coding-agent` (`PI_VERSION`), `PI_CODING_AGENT_DIR=/pi/agent` ; aucune extension (jusqu'au 07/10/2026 : `tools/llm-proxy-web.ts` de llmsetup, retirée) |
| `pi/models.json.tpl` | Les providers pi : `llm-proxy` (`openai-completions`, `${PROXY_URL}/v1`) — le seul joué — et `llm-proxy-anthropic` (`anthropic-messages`), gardé pour un essai à la main |
| `pi/entrypoint.sh` | Substitue `${PROXY_URL}` et génère une entrée de modèle par élément de `MODELS` → `models.json` du conteneur |
| `pi/scenarios.sh` | 6 scénarios, rejoués pour chaque modèle de `MODELS` |
| `codex/Dockerfile` | `node:22-slim` + `@openai/codex` (`CODEX_VERSION`), `CODEX_HOME=/codex` |
| `codex/entrypoint.sh` | Génère `config.toml` : provider `llm-proxy`, `wire_api = "responses"`, `${PROXY_URL}/v1`, clé lue dans `PROXY_API_KEY` (non vide), `model` = le premier de `MODELS` pour un essai à la main |
| `codex/scenarios.sh` | 8 scénarios (les cinq de pi, deux sur les outils web, et l'exemple en deux tours), rejoués pour chaque modèle de `MODELS` |
| `omp/Dockerfile` | `node:22-slim` + le binaire `omp-linux-x64` des [releases GitHub](https://github.com/can1357/oh-my-pi/releases) (`OMP_VERSION`, la dernière par défaut), `PI_CODING_AGENT_DIR=/omp/agent` (vide) ; l'extension du provider, `tools/llm-proxy.ts` de [llmsetup](https://github.com/c4software/llmsetup), dans `/omp/extensions`, à un commit **épinglé**, sha256 vérifié |
| `omp/install.mjs` | Ce que le Dockerfile exécute à la construction : télécharge omp (sha256 lu dans le `SHA256SUMS.txt` de la release) et l'extension du provider (sha256 épinglé), puis remplace dans `llm-proxy.ts` les deux lignes qui portent l'adresse et la clé en dur par la lecture de `LLM_PROXY_URL` / `LLM_PROXY_API_KEY` |
| `omp/entrypoint.sh` | Pose `LLM_PROXY_URL` et `LLM_PROXY_API_KEY` (lues par l'extension du provider) depuis `PROXY_URL` et `PROXY_API_KEY` ; aucun fichier de configuration à générer |
| `omp/scenarios.sh` | 6 scénarios (ceux de pi), rejoués pour chaque modèle de `MODELS` sous le nom `albert/<modèle>` |
| `api/Dockerfile` | `python:3-slim`, rien à installer |
| `api/scenarios.py` | 8 scénarios en requêtes HTTP (bibliothèque standard), rejoués pour chaque modèle de `MODELS` |

## Ce que les scénarios vérifient

**Claude Code** (`claude -p --dangerously-skip-permissions --max-turns 40
--output-format json`, donc sans aucune question, le champ `result` est
lu ; `--max-turns` parce qu'en mode `-p` Claude Code ne plafonne pas les
tours — un modèle qui répète le même appel d'outil tournerait à l'infini,
c'est arrivé : 754 tours identiques sur un 27B, une heure de GPU, avant
que ce plafond n'existe. `MAX_TURNS` dans l'environnement pour le
changer) :

1. **Réponse simple** — `POST /v1/messages` en flux SSE, traduction de la
   réponse (`message_start` → `text_delta` → `message_stop`).
2. **Write + Bash + Read** — plusieurs tours d'outils : `tool_use` du
   modèle → `tool_result` de Claude Code → messages `tool` OpenAI ;
   vérifié sur le disque (`hello.txt` contient `bonjour`).
3. **Glob + Grep + Edit** — arguments JSON des outils fragmentés dans le
   flux (`input_json_delta`), plusieurs outils par tour ; vérifié sur le
   disque (`a - b` → `a + b`).
4. **Image dans un `tool_result`** — Claude Code lit un `.png` : relayée
   au modèle s'il est multimodal au catalogue et que le backend a
   `images = true`, remplacée par un texte sinon. Le test passe dès que
   la réponse n'est pas une erreur (un modèle texte qui recevait l'image
   faisait un 500 chez llama.cpp).
5. **`count_tokens`** — exact via `tokenize_path` du backend s'il est
   défini, estimation sinon ; les deux répondent `{"input_tokens": N}`.
6. **`GET /v1/models`** avec `anthropic-version` — forme Anthropic, le
   modèle de `.env` doit y être.
7. **Erreur au dialecte Anthropic** — corps invalide → `400` avec
   `{"type": "error", "error": {…}}`, la forme que le SDK Anthropic
   attend. (Un modèle inconnu ne conviendrait pas : `default` du
   `model_map` l'attrape.)
8. **Création de code** — le modèle écrit un module Node (`mean`,
   `median`) et ses tests `node:assert`, les exécute et itère jusqu'à ce
   qu'ils passent ; vérifié en relançant `node test.js` et en appelant
   les fonctions.
9. **Corriger un bug sous test** — `slugify.test.js` échoue ; le modèle
   doit corriger `slugify.js` **sans toucher au test** (contrôlé par
   `cksum`) et le faire passer.
10. **Refactor multi-fichiers** — extraire le calcul dupliqué de
    `cart.js` / `invoice.js` dans un nouveau `money.js` avec `Edit`, tests
    verts et `require("./money")` présent dans les deux fichiers.
11. **Lectures en parallèle** — trois fichiers lus dans un tour, une
    réponse factuelle vérifiable (`5432`).
12. **Échappement JSON des arguments d'outil** — un `Write` avec accents,
    guillemets droits, antislashs : on vérifie que ces caractères
    traversent la traduction intacts, pas que le modèle recopie mot pour
    mot (le 27B reformule les libellés).
13. **Bash en pipeline** — un chiffre vérifiable (`3` lignes).
14. **Recherche web hébergée** — voir
    [Outils web hébergés](#outils-web-hébergés-par-le-proxy). Seul
    scénario lancé en `--output-format stream-json --verbose` et avec
    `--tools WebSearch`.

Les scénarios 8–10 sont ceux qui ressemblent au travail réel : une
dizaine de tours d'outils chacun, arguments volumineux (le contenu des
fichiers), plusieurs outils par tour.

**pi** (`pi -p --no-session --provider llm-proxy --model …`, les outils
s'exécutent sans confirmation) — les scénarios 1 à 3, puis la création
de code (8) et la correction sous test (9), par l'API OpenAI du proxy :
c'est le chemin de relais brut, sans traduction ; puis, en sixième, la
[recherche web hébergée](#outils-web-hébergés-par-le-proxy). (Le provider
`llm-proxy-anthropic` du `models.json` n'est pas joué ; il a servi une
fois à vérifier la traduction avec un second client Anthropic, et reste
disponible pour un essai à la main.)

**Codex CLI** (`codex exec --skip-git-repo-check --ephemeral
--dangerously-bypass-approvals-and-sandbox -m …` : le bac à sable de
Codex ne démarre pas dans un conteneur sans privilèges, le conteneur
jetable en tient lieu) — les cinq premiers scénarios de pi, par `POST
/v1/responses` : outils `function`, `namespace` aplatis, `web_search`
exécuté par le proxy s'il l'héberge et ignoré sinon, appels rejoués
(`function_call` / `function_call_output`) d'un tour à l'autre ; puis
deux scénarios sur les
[outils web hébergés](#outils-web-hébergés-par-le-proxy) (6 et 7).
Banc écrit le 05/10/2026 et joué le jour même avec Codex CLI 0.157.1
(7/7, voir « Derniers résultats »). Relu le même jour contre les sources de
Codex à l'étiquette `rust-v0.157.1` et le registre npm : nom du paquet,
clés du provider (`name`, `base_url`, `wire_api`, `env_key`), options de
`codex exec` (`--skip-git-repo-check`, `--ephemeral`, `--json`,
`--sandbox`) et forme des événements JSON.

**omp** (`omp --no-session --no-extensions -e /omp/extensions/llm-proxy.ts
--no-skills --no-rules --no-lsp --no-title --max-time 20m --model
albert/… -p "…"`, plus `--auto-approve` : aucun outil n'attend d'accord) —
les six scénarios de pi, sous les mêmes prompts : les outils d'omp
s'appellent aussi `read`, `write`, `edit` et `bash`. Le chemin dans le
proxy est celui de pi, le relais brut de `/v1/chat/completions` ; ce qui
change est le client, et son provider :

- **le provider vient de l'extension `tools/llm-proxy.ts`** de llmsetup,
  celle des postes : elle lit `GET /v1/models` du proxy et enregistre ses
  modèles sous le nom `albert`, avec ses réglages de raisonnement
  (`chat_template_kwargs`). Le fichier *versionné* a l'adresse du proxy
  en dur ; la copie installée sur les postes, non versionnée, lit
  `LLM_PROXY_URL` et `LLM_PROXY_API_KEY`, et ne diffère de lui que par ces
  deux lignes (et, sans effet, par la langue des commentaires et des
  messages). L'image prend donc le
  fichier versionné à un commit épinglé, vérifie son sha256, puis y
  remplace ces deux lignes-là (`omp/install.mjs`, qui s'arrête si elles
  n'y sont plus). omp a bien un fichier de providers, `models.yml` dans
  son dossier d'agent (sa doc `docs/models.md`), l'équivalent du
  `models.json` de pi : il aurait donné un provider sans rien substituer,
  mais pas celui des postes, et un chemin qu'aucune validation à la main
  n'a pris ;
- le dossier d'agent du conteneur est **vide** : ni configuration, ni
  extension découverte d'office. En mode interactif omp y ouvrirait son
  assistant de premier lancement ; pas constaté en `-p`.

Banc écrit le 05/10/2026 et joué le jour même avec omp 18.3.2 (6/6, voir
« Derniers résultats »). Vérifié avant de le lancer :
l'artefact (`omp-linux-x64` de la release `v18.3.2`, 278 132 192 octets,
sha256 `8cbbcd4b…2534` au `SHA256SUMS.txt` de la release : le binaire
installé sur le poste de validation a le même) et `omp/install.mjs`, joué
hors conteneur avec `OMP_VERSION=18.3.2` (téléchargement, sommes,
substitution : le fichier obtenu ne diffère du versionné que des deux
lignes) ; chaque option, lue dans `omp --help` de la 18.3.2 ; la forme des
événements de `--mode json` et la règle de `--tools`, lues dans les
sources d'omp à l'étiquette `v18.3.2` (`modes/print-mode.ts`,
`packages/agent/src/types.ts`, `sdk.ts`, `cli/args.ts`) ; la logique du
script, contre un faux `omp` et un faux proxy. La commande elle-même a été
validée à la main le même jour avec omp 18.3.2, `--auto-approve`,
`--max-time` et `--mode json` en moins.

**api** (`python3 scenarios.py`, des requêtes `urllib`) — huit scénarios,
détaillés dans [Le banc `api`](#le-banc-api--loutil-hébergé-déclaré-sur-chatcompletions).

### Outils web hébergés par le proxy

Un scénario « recherche web » par client, à la suite des autres, et pour
Codex un second qui enchaîne recherche et lecture de page. Écrits le
05/10/2026 et joués le jour même contre un proxy déployé avec SearXNG :
les quatre passent (voir « Derniers résultats »). Ceux de pi et d'omp
ont changé de chemin le 07/10/2026 (outils d'office, plus d'extension)
et ont été rejoués sous cette forme le jour même : 6/6 chacun.

| Client | N° | Ce qui est demandé | Chemin dans le proxy |
|---|---|---|---|
| Claude Code | 14 | l'URL de la page des releases du dépôt GitHub `ggml-org/llama.cpp`, avec l'outil `WebSearch` | sous-requête `/v1/messages` avec l'outil serveur `web_search_20250305` |
| pi | 6 | la même URL, **sans aucun outil côté pi** (`--no-tools`) : ceux du proxy, présentés d'office | `[chat].always` : boucle de `/v1/chat/completions`, appel caché du client |
| Codex | 6 | la même URL (`--sandbox read-only`) | `web_search` déclaré dans `/v1/responses`, boucle du proxy |
| Codex | 7 | une recherche sur le dépôt `c4software/llmsetup`, puis la lecture de `tools/llm-proxy-web.ts` à un commit donné, et la ligne qui y définit `PREFIXE` | `web_search` puis `web_fetch` dans la même boucle |
| Codex | 8 | l'exemple en deux tours : résumer une page longue, puis, dans la même session (`codex exec resume --last`), une question dont la réponse n'est que dans la page | `web_fetch` par morceaux (`offset`), puis la mémoire des résultats du proxy |
| omp | 6 | la même URL que pi, **sans aucun outil d'omp** (`--no-tools`) : ceux du proxy, présentés d'office | `[chat].always` : boucle de `/v1/chat/completions`, appel caché du client |

**Sauté plutôt qu'échoué.** Avant de jouer, chaque banc lit `GET
/healthz` du proxy visé (`tools.enabled`, sans clé). Sans `web_search` —
et, pour le scénario 7 de Codex, sans `web_fetch` — le scénario imprime
`SKIP` avec ce qu'il a lu. Un proxy antérieur au 05/10/2026, ou qui ne
répond pas sur `/healthz`, donne une liste vide : sauté aussi. Les
scénarios 6 de pi et d'omp sont sautés de même quand le proxy ne présente
pas `web_search` d'office (`chat.always` de `/healthz`) — il faut, côté
proxy, `[chat] hosted_tools = true` et `always = ["web_search",
"web_fetch"]`, et que les modèles de `MODELS` ne soient pas dans
`always_except`.

**Deux vérifications par scénario, et c'est la première qui compte.**

1. *La recherche a eu lieu*, lu dans la trace du client et non dans sa
   réponse — un modèle peut écrire l'URL d'un dépôt connu de mémoire :
   - Claude Code (`stream-json`) : un bloc `tool_use` de l'outil
     `WebSearch`, suivi de son `tool_result` sans erreur et portant au
     moins une URL. Claude Code compose ce résultat depuis les blocs
     `web_search_tool_result` de sa sous-requête ; une recherche en échec
     y laisse `Web search error: …`, sans lien.
   - pi et omp (`--mode json --no-tools`, depuis le 07/10/2026) : la
     recherche est faite **par le proxy**, dans sa boucle, et le client
     n'en voit rien — ni appel, ni résultat : c'est le principe des outils
     d'office. Sa trace ne prouve donc qu'une chose, qu'**aucun outil du
     client n'a été exécuté** (pas d'événement `tool_execution_start` ;
     `--no-tools` n'en laisse d'ailleurs aucun au modèle — ni le
     `web_search` intégré d'omp, qui cherche depuis le conteneur, ni
     `read`, ni `bash`). La preuve est **côté proxy** : le compteur
     d'exécutions de `web_search` par la route `/v1/chat/completions`
     ([Usage API des outils](../README.md#usage-des-outils-hébergés)), lu
     avant et après l'appel, doit avoir avancé d'au moins 1. Le compteur
     est celui du proxy entier — un autre client peut l'avancer. Illisible
     (proxy sans cette route, clé refusée), rien ne prouve la recherche :
     le scénario échoue et le libellé le dit. (Avant le 07/10/2026 : un
     événement `tool_execution_end` de `proxy_web_search`, l'outil de
     l'extension, dans la trace du client.)
   - Codex (`--json`) : un élément `web_search` terminé dans la trace, et
     pour le scénario 7 un second dont l'action est `open_page`. Codex ne
     les construit que depuis les `web_search_call` du proxy. La trace ne
     dit **pas** si la recherche a rendu des résultats ni si la page a pu
     être lue : l'élément ne porte que la requête ou l'URL. C'est la
     réponse qui le dit, au point 2.
2. *La réponse est la bonne*, par une sous-chaîne qui ne dépend ni de
   l'actualité ni de la formulation :
   - `github.com/ggml-org/llama.cpp` (casse ignorée) pour la recherche :
     l'URL est déterminée par le nom du dépôt, et toute bonne réponse la
     contient (`/releases`, `/releases/latest`, lien Markdown). Le prompt
     ne la donne pas, et n'est de toute façon pas dans le texte examiné.
     Demander plutôt la dernière release aurait été fragile : le
     05/10/2026 elle s'appelle `v0.6.0`, après des années d'étiquettes
     `bNNNN`.
   - `proxy_` pour la lecture de page (Codex 7) : la page est un fichier
     adressé par son commit (contenu immuable), écrit le 05/10/2026 donc
     absent de la mémoire de tout modèle, et la ligne demandée —
     `const PREFIXE = "proxy_";` — ne s'écrit que d'une façon. Ici la
     réponse prouve à elle seule que la page est arrivée au modèle.

Ce que ces scénarios ne peuvent pas dire :

- La trace de Codex ne distingue pas une recherche fructueuse d'une
  recherche vide, ni le nombre de requêtes HTTP : « dans le même tour »
  est ce que le client voit, « dans la même réponse `/v1/responses` » se
  lit dans les logs du proxy (`outil hébergé web_search(…) → N car.`).
- Un échec sur la sous-chaîne, preuve de recherche acquise, met en cause
  le modèle ou les moteurs que SearXNG joint ce jour-là, pas la
  traduction : le message d'échec dit laquelle des deux vérifications a
  manqué.
- La lecture de page de Claude Code (`WebFetch`) n'est pas jouée ; celle
  de pi et d'omp (`web_fetch` d'office) non plus : l'outil est présenté
  au modèle, rien ne l'exige.
- Les scénarios 6 de pi et d'omp retirent au client tous ses outils :
  pi ou omp avec leurs outils **et** ceux du proxy — tour mixte, et pour
  omp son `web_search` intégré à côté — n'est pas joué. Les scénarios 1 à
  5 reçoivent bien les outils d'office en plus des leurs, mais rien n'y
  vérifie ce que le modèle en fait.
- La mémoire des échanges cachés (le tour suivant d'une conversation où
  le proxy a cherché) n'est pas jouée par ces bancs : une seule requête.
- Le compteur du proxy dit qu'une recherche a été exécutée par
  `/v1/chat/completions` pendant l'appel, pas par qui : sur un proxy que
  d'autres utilisent au même moment, le scénario 6 de pi ou d'omp peut
  passer sans que sa propre requête ait cherché — la trace du client ne
  relie plus la recherche au scénario.
- `--no-tools` a été lu dans `pi --help` (1.0.4) et `omp --help`
  (18.6.3) le 07/10/2026, et joué ce jour-là avec pi 1.0.4 et omp
  18.8.0 ; pas avec les versions validées à la main le 05/10/2026 (pi
  0.87.1, omp 18.3.2).
- Le scénario 7 de Codex lit toujours `tools/llm-proxy-web.ts` de
  llmsetup, mais à un **commit donné** : le fichier y reste lisible une
  fois l'extension retirée de la branche, tant que ce commit existe dans
  le dépôt public. S'il disparaissait (historique réécrit), le scénario
  serait à pointer sur un autre fichier immuable.
- Le scénario 7 de Codex lit `raw.githubusercontent.com` : un proxy dont
  `[tools.web_fetch].allowed_domains` ne le permet pas le fera échouer.

### Le banc `api` : l'outil hébergé déclaré sur chat/completions

Aucun client agentique ne met `{"type": "web_search"}` dans les `tools`
d'une requête `/v1/chat/completions` : c'est une forme que le proxy
définit ([README principal](../README.md#client-chatcompletions--déclarer-loutil)),
active seulement avec `[chat].hosted_tools = true`. Le banc est donc un
client HTTP minimal — `api/scenarios.py`, Python et sa bibliothèque
standard — qui envoie les requêtes et lit les réponses. Il n'exécute
aucun outil et ne tient aucune conversation. Écrit le 05/10/2026, **pas
encore joué** contre un proxy : seulement à blanc, contre un faux serveur
local dont les réponses chat/completions sortaient du vrai
`llm_proxy.chat_api.Translator`.

La question posée est celle des autres bancs — l'URL de la page des
releases de `ggml-org/llama.cpp`, « recopiée telle qu'elle apparaît dans
les résultats » — et la sous-chaîne attendue la même,
`github.com/ggml-org/llama.cpp`.

| N° | Requête | Ce qui est vérifié |
|---|---|---|
| 1 | `POST /v1/chat/completions`, `tools: [{"type": "web_search"}]`, `stream: true`, `include_usage` | un flux SSE ; **un seul `id`** sur tous les blocs ; **aucun `tool_calls`** dans les deltas ; **un seul `finish_reason`** ; au plus un bloc `usage` ; `[DONE]` une fois, en dernier ; pas de bloc `error` ; la sous-chaîne dans le texte ; **au moins une annotation `url_citation`**, et pour chacune `contenu[start_index:end_index] == url` |
| 2 | la même, `stream: false` | un seul choix, pas de `tool_calls`, un `finish_reason`, la sous-chaîne, les annotations (`message.annotations`) aux mêmes conditions |
| 3 | la même **sans** `tools` | relais ordinaire : `200`, un contenu, un `finish_reason`, **aucune annotation**. La réponse elle-même n'est pas jugée (le modèle répond de mémoire) |
| 4 | `GET /v1/tools` | `web_search` dans la liste, avec une description et un paramètre `query` |
| 5 | `POST /v1/tools/web_search`, `{"query": …}` | `{"name": "web_search", "result", "is_error": false}`, au moins une URL dans le résultat |
| 6 | `POST /v1/responses`, `tools: [{"type": "web_search"}]`, `stream: false` | réponse `completed` ; un élément `web_search_call` **terminé**, puis un `message` ; aucun `function_call` ; la sous-chaîne dans le texte |
| 7 | `GET /v1/organization/usage/tools` (`bucket_width=all`, `group_by[]=endpoint`, `group_by[]=tool`), lu au début du jeu puis à la fin | par route — `/v1/chat/completions`, `/v1/tools`, `/v1/responses` — le compteur de `web_search` a avancé d'**au moins autant que de scénarios réussis sur cette route** |
| 8 | deux `POST /v1/responses` : résumer une page longue, puis la même conversation renvoyée avec ses éléments `web_search_call` et une question de suivi | tour 1 : au moins deux morceaux contigus depuis 0, rendus « url [a, b] », résumé pertinent ; tour 2 : aucun outil rappelé, aucune lecture comptée par le proxy, réponse juste, et au moins la moitié du prompt reprise du cache du backend |

**L'exemple en deux tours (scénario 8 des bancs codex et api).** Né
d'un essai à la main du 05/10/2026 : « résume-moi cette page », puis
« et quelle règle métier s'applique à la suppression ? ». Il tient
ensemble ce que les autres scénarios ne voient pas : une page plus longue
qu'un morceau de `web_fetch`, lue par `offset` et affichée par le client
avec ses plages ; puis un second tour où le client renvoie la
conversation SANS le contenu de la page — le modèle ne peut répondre que
si le proxy le lui a rendu (mémoire des résultats), et le backend ne
reprend son cache que si la conversation rejouée est celle qu'il a
calculée. La page par défaut est une page de cours du propriétaire du
dépôt (51 143 caractères ce jour-là, dont le TP interdit de supprimer une
tâche non terminée) ; `LONG_URL`, `LONG_NEEDLE`, `LONG_QUESTION` et
`LONG_ANSWER` en choisissent une autre. Joué le jour même : banc api 8/8
contre le proxy déployé ; scénario Codex joué hors conteneur avec Codex
0.157.1 (trois morceaux, aucune relecture au second tour), pas encore
dans son image.

**Ce qui prouve la recherche.** Côté client, pour 1 et 2, l'annotation :
le proxy n'en pose que pour une URL qu'un outil a rendue *et* que le
modèle a écrite à l'identique — une réponse de mémoire n'en porte pas.
(Le client ne voit rien d'autre : c'est le principe de ce chemin, les
appels hébergés ne lui arrivent jamais.) Pour 6, l'élément
`web_search_call`. Côté proxy, pour les trois routes, le compteur du
scénario 7 — et chaque scénario affiche déjà le sien (`compteur
/v1/chat/completions : +1`), pour qu'un échec se lise sans les logs du
proxy : `+0` sous « aucune annotation » dit que le modèle n'a pas
cherché, `+1` qu'il a cherché sans recopier une URL des résultats.

**Sauté plutôt qu'échoué**, d'après `GET /healthz` : sans `web_search`
dans `tools.enabled`, tout sauf le scénario 3 ; avec `chat.hosted_tools`
faux, 1 et 2 ; avec `responses.enabled` faux, 6 ; et 7 si la route
d'usage des outils ne répond pas `200` (proxy antérieur au 05/10/2026).

Ce que ce banc ne dit pas :

- l'usage **cumulé** sur les tours : le client reçoit un seul bloc
  `usage` et n'a aucun moyen de savoir combien de tours il additionne.
  Le banc l'affiche et vérifie qu'il n'y en a pas deux ; le cumul se
  vérifie dans `proxy/tests/test_chat_api.py` ;
- `[chat].annotations = false` sur le proxy fait échouer 1 et 2 (rien
  dans `/healthz` ne le dit) ;
- la lecture de page (`web_fetch`) n'est pas exigée : le modèle l'a sous
  la main dans 1, 2 et 6, libre à lui ;
- le tour mixte (outil hébergé et outil du client dans le même tour), le
  `400` d'un outil déclaré mais désactivé, `web_search_options`, la
  requête suivante de la conversation : seulement dans les tests du dépôt ;
- le compteur est celui du proxy entier : « au moins », jamais « exactement ».

## Ce qui n'est PAS vérifié ici

- Le limiteur de quotas et les `event: ping` pendant l'attente : il faut
  un backend à quotas et de la contention (voir `proxy/tests/` pour la
  traduction, et la section Claude Code du README principal pour le
  scénario joué à la main).
- La qualité des réponses : un modèle qui «corrige» `a - b` en autre chose
  que `a + b` fait échouer le scénario 3 sans que le proxy y soit pour
  rien. Prendre des modèles qui suivent les instructions (`MODELS` de
  `.env`) — voir les derniers résultats en tête de ce fichier.

## Ce que ces scénarios ont déjà trouvé

Trois bugs du proxy, invisibles sur des tests unitaires :

- llama.cpp répond **500 à une image** envoyée à un modèle texte → les
  images sont décidées par modèle, depuis le catalogue ;
- Claude Code ajoute **`?beta=true`** à ses URLs, relayé à l'upstream ;
- les gabarits Qwen / Mistral refusent un **message `system` en cours de
  conversation** (Claude Code en envoie pour ses rappels) → fondu dans
  le `user` suivant.

## Coût

Chaque appel Claude Code porte son prompt système (~18–20 k tokens
d'entrée) ; les 13 scénarios font 80 à 90 appels **par modèle**, soit
~1,5 M de tokens d'entrée sur un 27B (qui tâtonne davantage) et
~0,75 M sur le 35B-A3B. Viser des backends **sans quota** dans `.env`,
pas Albert. Durée : ~40 min sur un 27B dense local, ~20 min sur un
35B-A3B, pi compris. Les bancs `omp` et `api` n'ont pas encore été
chronométrés ; `api` fait, par modèle, trois réponses avec recherche, une
sans, et une recherche directe.

## Docker Desktop (mac / Windows)

Pas de réseau hôte : mettre `PROXY_URL=http://host.docker.internal:8000`
dans `.env` et retirer `network_mode: host` du `docker-compose.yml`.
