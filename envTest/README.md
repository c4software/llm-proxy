# envTest — valider le proxy avec de vrais clients

Trois clients jetables, chacun dans son conteneur, qui tapent le proxy
et jouent des scénarios de validation — **Claude Code** (API Anthropic,
traduite par le proxy), **pi** ([pi.dev](https://pi.dev), API OpenAI) et
**Codex CLI** (API Responses, traduite par le proxy).
Chaque jeu est rejoué pour **chaque modèle** de `MODELS`. Rien n'est
installé sur l'hôte ; `~/.claude`, `~/.pi` et `~/.codex` ne sont jamais
lus ni écrits : chaque client a sa configuration dans l'image, et son dossier
de travail disparaît avec le conteneur.

## Derniers résultats

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

    cd envTest
    cp .env.example .env        # PROXY_URL, MODELS, clé — voir le fichier
    docker compose run --rm claude    # scénarios Claude Code, pour chaque modèle
    docker compose run --rm pi        # scénarios pi (API OpenAI), pour chaque modèle
    docker compose run --rm codex     # scénarios Codex (API Responses), pour chaque modèle

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
Code), `MAX_TURNS` (plafond de tours par scénario, 40 par défaut).
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
`LLM_PROXY_URL` pour l'extension web de pi.

Les images prennent la **dernière** version de chaque client. Pour
rejouer celles qui ont été validées à la main le 05/10/2026 :

    docker compose build --build-arg CLAUDE_CODE_VERSION=2.1.287 claude
    docker compose build --build-arg PI_VERSION=0.87.1 pi
    docker compose build --build-arg CODEX_VERSION=0.157.1 codex

## Fichiers

| Fichier | Rôle |
|---|---|
| `.env.example` | `PROXY_URL` (le proxy vu des conteneurs, local ou distant), `PROXY_API_KEY`, `MODELS` (préfixés, séparés par des espaces), `SMALL_MODEL` — copié en `.env`, ignoré par git |
| `docker-compose.yml` | Les trois services, en **réseau hôte** (`127.0.0.1:8000` = le proxy de la racine) |
| `claude/Dockerfile` | `node:22-slim` + `@anthropic-ai/claude-code` (`CLAUDE_CODE_VERSION`, la dernière par défaut), utilisateur non root (requis par `--dangerously-skip-permissions`), télémétrie et mises à jour coupées |
| `claude/settings.json` | Le `~/.claude/settings.json` **du conteneur** : `CLAUDE_CODE_ATTRIBUTION_HEADER=0`, pour que l'attribution (variable d'une requête à l'autre) ne décale pas le préfixe et ne fasse pas manquer le cache du backend |
| `claude/scenarios.sh` | Les 14 scénarios Claude Code, rejoués pour chaque modèle de `MODELS` (`ANTHROPIC_MODEL` posé par le script) ; `ONLY=N` pour n'en jouer que N |
| `pi/Dockerfile` | `node:22-slim` + `@earendil-works/pi-coding-agent` (`PI_VERSION`), `PI_CODING_AGENT_DIR=/pi/agent` ; télécharge l'extension `tools/llm-proxy-web.ts` de [llmsetup](https://github.com/c4software/llmsetup) à un commit **épinglé**, sha256 vérifié, dans `/pi/extensions` — la mise à jour est décrite dans le fichier |
| `pi/models.json.tpl` | Les providers pi : `llm-proxy` (`openai-completions`, `${PROXY_URL}/v1`) — le seul joué — et `llm-proxy-anthropic` (`anthropic-messages`), gardé pour un essai à la main |
| `pi/entrypoint.sh` | Substitue `${PROXY_URL}` et génère une entrée de modèle par élément de `MODELS` → `models.json` du conteneur ; pose `LLM_PROXY_URL` et `LLM_PROXY_KEY` (lues par l'extension) depuis `PROXY_URL` et `PROXY_API_KEY` |
| `pi/scenarios.sh` | 6 scénarios, rejoués pour chaque modèle de `MODELS` |
| `codex/Dockerfile` | `node:22-slim` + `@openai/codex` (`CODEX_VERSION`), `CODEX_HOME=/codex` |
| `codex/entrypoint.sh` | Génère `config.toml` : provider `llm-proxy`, `wire_api = "responses"`, `${PROXY_URL}/v1`, clé lue dans `PROXY_API_KEY` (non vide), `model` = le premier de `MODELS` pour un essai à la main |
| `codex/scenarios.sh` | 7 scénarios (les cinq de pi, puis deux sur les outils web), rejoués pour chaque modèle de `MODELS` |

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
**Banc écrit le 05/10/2026 et pas encore joué** : la traduction a été
validée ce jour-là avec Codex CLI 0.157.1 lancé hors conteneur (session
de 12 requêtes vers `bigchuck/qwen3.8-flash-next`, appels d'outils
compris), pas avec cette image. Relu le même jour contre les sources de
Codex à l'étiquette `rust-v0.157.1` et le registre npm : nom du paquet,
clés du provider (`name`, `base_url`, `wire_api`, `env_key`), options de
`codex exec` (`--skip-git-repo-check`, `--ephemeral`, `--json`,
`--sandbox`) et forme des événements JSON — une relecture, pas une
exécution.

### Outils web hébergés par le proxy

Un scénario « recherche web » par client, à la suite des autres, et pour
Codex un second qui enchaîne recherche et lecture de page. **Écrits le
05/10/2026 et pas encore joués** : ce jour-là les outils ont été validés
à la main, hors conteneur, avec Claude Code 2.1.287, pi 0.87.1 et Codex
0.157.1 (README principal, « Outils hébergés ») — pas avec ces images, et
pas avec ces scénarios.

| Client | N° | Ce qui est demandé | Chemin dans le proxy |
|---|---|---|---|
| Claude Code | 14 | l'URL de la page des releases du dépôt GitHub `ggml-org/llama.cpp`, avec l'outil `WebSearch` | sous-requête `/v1/messages` avec l'outil serveur `web_search_20250305` |
| pi | 6 | la même URL, avec pour seul outil `proxy_web_search` (extension `llm-proxy-web.ts`) | `GET /v1/tools`, `POST /v1/tools/web_search` |
| Codex | 6 | la même URL (`--sandbox read-only`) | `web_search` déclaré dans `/v1/responses`, boucle du proxy |
| Codex | 7 | une recherche sur le dépôt `c4software/llmsetup`, puis la lecture de `tools/llm-proxy-web.ts` à un commit donné, et la ligne qui y définit `PREFIXE` | `web_search` puis `web_fetch` dans la même boucle |

**Sauté plutôt qu'échoué.** Avant de jouer, chaque banc lit `GET
/healthz` du proxy visé (`tools.enabled`, sans clé). Sans `web_search` —
et, pour le scénario 7 de Codex, sans `web_fetch` — le scénario imprime
`SKIP` avec ce qu'il a lu. Un proxy antérieur au 05/10/2026, ou qui ne
répond pas sur `/healthz`, donne une liste vide : sauté aussi.

**Deux vérifications par scénario, et c'est la première qui compte.**

1. *La recherche a eu lieu*, lu dans la trace du client et non dans sa
   réponse — un modèle peut écrire l'URL d'un dépôt connu de mémoire :
   - Claude Code (`stream-json`) : un bloc `tool_use` de l'outil
     `WebSearch`, suivi de son `tool_result` sans erreur et portant au
     moins une URL. Claude Code compose ce résultat depuis les blocs
     `web_search_tool_result` de sa sous-requête ; une recherche en échec
     y laisse `Web search error: …`, sans lien.
   - pi (`--mode json`) : un événement `tool_execution_end` de
     `proxy_web_search`, `isError` faux, dont le résultat porte au moins
     une URL — le texte rendu par `POST /v1/tools/web_search`.
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
- La lecture de page de Claude Code (`WebFetch`) et les commandes `/web`
  et `/page` de l'extension pi ne sont pas jouées ; la lecture de page
  par `/v1/tools/web_fetch` (pi) non plus.
- Le scénario de pi limite le modèle à l'outil de recherche : pi avec
  tous ses outils **et** l'extension n'est pas joué.
- Le scénario 7 de Codex lit `raw.githubusercontent.com` : un proxy dont
  `[tools.web_fetch].allowed_domains` ne le permet pas le fera échouer.

## Ce qui n'est PAS vérifié ici

- Le limiteur de quotas et les `event: ping` pendant l'attente : il faut
  un backend à quotas et de la contention (voir `tests/` pour la
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
35B-A3B, pi compris.

## Docker Desktop (mac / Windows)

Pas de réseau hôte : mettre `PROXY_URL=http://host.docker.internal:8000`
dans `.env` et retirer `network_mode: host` du `docker-compose.yml`.
