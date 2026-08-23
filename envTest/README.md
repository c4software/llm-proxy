# envTest — valider le proxy avec de vrais clients

Deux clients jetables, chacun dans son conteneur, qui tapent le proxy
et jouent des scénarios de validation — **Claude Code** (API Anthropic,
traduite par le proxy) et **pi** ([pi.dev](https://pi.dev), API OpenAI).
Chaque jeu est rejoué pour **chaque modèle** de `MODELS`. Rien n'est
installé sur l'hôte ; `~/.claude` et `~/.pi` ne sont jamais lus ni
écrits : chaque client a sa configuration dans l'image, et son dossier
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

Le proxy doit tourner (depuis la racine : `docker compose up -d`). Puis :

    cd envTest
    cp .env.example .env        # PROXY_URL, MODELS, clé — voir le fichier
    docker compose run --rm claude    # scénarios Claude Code, pour chaque modèle
    docker compose run --rm pi        # scénarios pi (API OpenAI), pour chaque modèle

Chaque scénario imprime `PASS` ou `FAIL` avec ce qu'il a vu ; la commande
sort en erreur si l'un échoue. Sortie attendue :

    ════ Claude Code 2.1.241 (Claude Code) → http://127.0.0.1:8000 | modèle bigchuck/qwen3.8-27b-mtp-nothink ════
    1. Réponse simple (POST /v1/messages, flux SSE)
      PASS Paris
    2. Outils : Write + Bash + Read (tool_use / tool_result, plusieurs tours)
      PASS hello.txt = bonjour — …
    …
    ════ Claude Code 2.1.241 (Claude Code) → http://127.0.0.1:8000 | modèle bigchuck/qwen3.6-35b-a3b-mtp-nothink ════
    …
    Résumé :
      bigchuck/qwen3.8-27b-mtp-nothink : 13/13
      bigchuck/qwen3.6-35b-a3b-mtp-nothink : 13/13
    Tout passe.

Pour essayer à la main, même image, même configuration :

    docker compose run --rm claude claude          # Claude Code interactif
    docker compose run --rm pi pi                  # pi interactif (/model pour changer de modèle ou de provider)
    docker compose run --rm claude claude -p "…"   # une question

Variables utiles à `docker compose run -e …` : `MODELS` (un seul modèle
pour aller vite), `ONLY=5` (ne joue que les N premiers scénarios Claude
Code), `MAX_TURNS` (plafond de tours par scénario, 40 par défaut). Avec
`[anthropic] trace = true` dans le `config.toml` du proxy, chaque réponse
du modèle apparaît dans `docker compose logs` (outils appelés, tokens).

## Fichiers

| Fichier | Rôle |
|---|---|
| `.env.example` | `PROXY_URL` (le proxy vu des conteneurs), `PROXY_API_KEY`, `MODELS` (préfixés, séparés par des espaces), `SMALL_MODEL` — copié en `.env`, ignoré par git |
| `docker-compose.yml` | Les deux services, en **réseau hôte** (`127.0.0.1:8000` = le proxy de la racine) |
| `claude/Dockerfile` | `node:22-slim` + `@anthropic-ai/claude-code`, utilisateur non root (requis par `--dangerously-skip-permissions`), télémétrie et mises à jour coupées |
| `claude/settings.json` | Le `~/.claude/settings.json` **du conteneur** : `CLAUDE_CODE_ATTRIBUTION_HEADER=0`, pour que l'attribution (variable d'une requête à l'autre) ne décale pas le préfixe et ne fasse pas manquer le cache du backend |
| `claude/scenarios.sh` | Les 13 scénarios Claude Code, rejoués pour chaque modèle de `MODELS` (`ANTHROPIC_MODEL` posé par le script) ; `ONLY=N` pour n'en jouer que N |
| `pi/Dockerfile` | `node:22-slim` + `@earendil-works/pi-coding-agent`, `PI_CODING_AGENT_DIR=/pi/agent` |
| `pi/models.json.tpl` | Les providers pi : `llm-proxy` (`openai-completions`, `${PROXY_URL}/v1`) — le seul joué — et `llm-proxy-anthropic` (`anthropic-messages`), gardé pour un essai à la main |
| `pi/entrypoint.sh` | Substitue `${PROXY_URL}` et génère une entrée de modèle par élément de `MODELS` → `models.json` du conteneur |
| `pi/scenarios.sh` | 5 scénarios, rejoués pour chaque modèle de `MODELS` |

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

Les scénarios 8–10 sont ceux qui ressemblent au travail réel : une
dizaine de tours d'outils chacun, arguments volumineux (le contenu des
fichiers), plusieurs outils par tour.

**pi** (`pi -p --no-session --provider llm-proxy --model …`, les outils
s'exécutent sans confirmation) — les scénarios 1 à 3, puis la création
de code (8) et la correction sous test (9), par l'API OpenAI du proxy :
c'est le chemin de relais brut, sans traduction. (Le provider
`llm-proxy-anthropic` du `models.json` n'est pas joué ; il a servi une
fois à vérifier la traduction avec un second client Anthropic, et reste
disponible pour un essai à la main.)

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
