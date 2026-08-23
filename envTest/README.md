# envTest — valider le proxy avec de vrais clients

Deux clients jetables, chacun dans son conteneur, qui tapent le proxy
et jouent des scénarios de validation — **Claude Code** (API Anthropic,
traduite par le proxy) et **pi** ([pi.dev](https://pi.dev), API OpenAI
*et* API Anthropic). Rien n'est installé sur l'hôte ; `~/.claude` et
`~/.pi` ne sont jamais lus ni écrits : chaque client a sa configuration
dans l'image, et son dossier de travail disparaît avec le conteneur.

## Lancer

Le proxy doit tourner (depuis la racine : `docker compose up -d`). Puis :

    cd envTest
    cp .env.example .env        # PROXY_URL, MODEL, clé — voir le fichier
    docker compose run --rm claude    # scénarios Claude Code
    docker compose run --rm pi        # scénarios pi (OpenAI puis Anthropic)

Chaque scénario imprime `PASS` ou `FAIL` avec ce qu'il a vu ; la commande
sort en erreur si l'un échoue. Sortie attendue :

    Claude Code 2.1.241 (Claude Code) → http://127.0.0.1:8000 | modèle bigchuck/qwen3.8-27b-mtp-nothink
    1. Réponse simple (POST /v1/messages, flux SSE)
      PASS Paris
    2. Outils : Write + Bash + Read (tool_use / tool_result, plusieurs tours)
      PASS hello.txt = bonjour — …
    …
    Tout passe.

Pour essayer à la main, même image, même configuration :

    docker compose run --rm claude claude          # Claude Code interactif
    docker compose run --rm pi pi                  # pi interactif (/model pour changer de provider)
    docker compose run --rm claude claude -p "…"   # une question

## Fichiers

| Fichier | Rôle |
|---|---|
| `.env.example` | `PROXY_URL` (le proxy vu des conteneurs), `PROXY_API_KEY`, `MODEL` (préfixé), `SMALL_MODEL` — copié en `.env`, ignoré par git |
| `docker-compose.yml` | Les deux services, en **réseau hôte** (`127.0.0.1:8000` = le proxy de la racine) |
| `claude/Dockerfile` | `node:22-slim` + `@anthropic-ai/claude-code`, utilisateur non root (requis par `--dangerously-skip-permissions`), télémétrie et mises à jour coupées |
| `claude/scenarios.sh` | Les 13 scénarios Claude Code |
| `pi/Dockerfile` | `node:22-slim` + `@earendil-works/pi-coding-agent`, `PI_CODING_AGENT_DIR=/pi/agent` |
| `pi/models.json.tpl` | Les deux providers pi : `llm-proxy` (`openai-completions`, `${PROXY_URL}/v1`) et `llm-proxy-anthropic` (`anthropic-messages`, `${PROXY_URL}`) |
| `pi/entrypoint.sh` | Substitue `${PROXY_URL}` / `${MODEL}` dans le gabarit → `models.json` du conteneur |
| `pi/scenarios.sh` | 5 scénarios, joués une fois par provider |

## Ce que les scénarios vérifient

**Claude Code** (`claude -p --dangerously-skip-permissions --output-format json`,
donc sans aucune question, le champ `result` est lu) :

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

Les scénarios 8–10 sont ceux qui ressemblent au travail réel : une
dizaine de tours d'outils chacun, arguments volumineux (le contenu des
fichiers), plusieurs outils par tour.

**pi** (`pi -p --no-session --provider … --model …`, les outils
s'exécutent sans confirmation) — les scénarios 1 à 3, puis la création
de code (8) et la correction sous test (9), une fois par provider. Le provider `llm-proxy-anthropic` est un **second client
Anthropic, indépendant du SDK officiel** : il valide la traduction sur
une autre implémentation (pi envoie `?beta=true`, des en-têtes
`anthropic-beta`, du `cache_control` — tout doit être ignoré proprement).

## Ce qui n'est PAS vérifié ici

- Le limiteur de quotas et les `event: ping` pendant l'attente : il faut
  un backend à quotas et de la contention (voir `tests/` pour la
  traduction, et la section Claude Code du README principal pour le
  scénario joué à la main).
- La qualité des réponses : un modèle qui «corrige» `a - b` en autre chose
  que `a + b` fait échouer le scénario 3 sans que le proxy y soit pour
  rien. Prendre un modèle qui suit les instructions (`MODEL` de `.env`).
  Validés : `bigchuck/qwen3.8-27b-mtp-nothink` (13/13, pi 10/10),
  `bigchuck/qwen3.6-35b-a3b-mtp-nothink` (7/7 sur la première série).

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
d'entrée) ; les 13 scénarios font une cinquantaine d'appels, soit
**~1 M de tokens d'entrée**. Viser un backend **sans quota** dans
`.env`, pas Albert. Durée : ~15 min sur un 27B dense local, ~5 min sur
un 35B-A3B.

## Docker Desktop (mac / Windows)

Pas de réseau hôte : mettre `PROXY_URL=http://host.docker.internal:8000`
dans `.env` et retirer `network_mode: host` du `docker-compose.yml`.
