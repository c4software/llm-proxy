# albert-proxy

Proxy transparent devant une API OpenAI-compatible qui applique
`tool_choice="none"` par défaut — cas d'Albert (DINUM), dont le schéma
OpenAPI déclare `"default": "none"` alors que sa propre description dit
que `auto` s'applique quand `tools` est présent.

Conséquence sans proxy : les agents (Hermes, pi, …) envoient `tools`,
n'envoient pas `tool_choice`, et le modèle *décrit* ce qu'il ferait au
lieu d'appeler les outils. Pas d'erreur, juste un agent qui s'arrête au
premier tour.

## Ce que fait le proxy

- Sur `POST /v1/chat/completions` : si `tools` est une liste non vide et
  que `tool_choice` est absent, ajoute `tool_choice: "auto"` (un
  `tool_choice` explicite du client n'est jamais écrasé).
- Route chaque requête vers son backend d'après le **préfixe du modèle**
  (`albert/…`, `bigchuck/…`) — voir « Multi-backends ».
- Temporise les requêtes vers Albert pour rester sous ses quotas
  (fenêtres minute et jour, limites exactes du compte via `/v1/me/info`).
- Le streaming SSE passe intact.

## Fichiers

| Fichier | Rôle |
|---|---|
| `main.py` | Le proxy : backends, routage au préfixe, `/v1/models` fusionné, HTTP |
| `albert.py` | Tout ce qui est spécifique à Albert : limiteur de quotas (fenêtres minute/jour), familles, association routeurs ↔ modèles via `/v1/me/info` |

`main.py` ne connaît d'Albert que « le backend marqué `quotas: true`
passe par `albert.get_limiter()` » ; toute la mécanique de quotas vit
dans `albert.py` et peut s'ignorer tant qu'on ne touche pas aux limites.

## Déploiement

### Docker Compose

    docker compose up -d --build

### Coolify

Nouvelle ressource → Dockerfile, pointer sur ce dépôt. Port 8000.
Ajouter les variables d'environnement ci-dessous, puis exposer le
service via Nginx Proxy Manager sur un sous-domaine interne.

## Variables

| Variable | Défaut | Rôle |
|---|---|---|
| `FORCE_TOOL_CHOICE` | `auto` | Valeur injectée |
| `UPSTREAM_API_KEY` | *(vide)* | Clé du backend à quotas ; si définie, **remplace systématiquement** l'`Authorization` du client |
| `UPSTREAM_TIMEOUT` | `600` | Secondes ; large pour les longues générations |
| `LOG_LEVEL` | `INFO` | `INFO` logue chaque injection |
| `BACKENDS` | *Albert seul* | JSON `{"<nom>": {url, api_key?, quotas?, timeout?, verify_ssl?}}` — le nom est le préfixe de routage ; **seule source de vérité des URLs** |
| `FORWARD_POST_PATHS` | `/v1/completions,/v1/embeddings,/v1/rerank,/v1/audio/transcriptions,/v1/ocr` | Seules routes POST relayées (en plus de `/v1/chat/completions` et `/v1/models`) ; toute autre URL → 404 local |

## Multi-backends (Albert + llama.cpp local)

`BACKENDS` déclare les backends, et **le préfixe du nom de modèle est le
discriminant de routage** — le client parle à un seul endpoint :

    BACKENDS: |
      {
        "albert":   {"url": "https://albert.api.etalab.gouv.fr",
                     "quotas": true},
        "bigchuck": {"url": "http://bigchuck:8009"}
      }

- `model: "bigchuck/qwen3-32b"` → bigchuck, préfixe retiré avant
  transfert (llama.cpp reçoit `qwen3-32b`), **sans limiteur** (local =
  illimité). `model: "albert/openweight-large"` → Albert, préfixe retiré,
  limiteur de quotas habituel.
- **Tout modèle doit être préfixé** : modèle sans préfixe reconnu → 400
  `unknown_backend_prefix`. Seules les requêtes sans champ `model`
  (endpoints de compte, corps non JSON) partent vers le backend à
  quotas.
- Chaque backend `"quotas": true` porte **sa propre** mécanique de
  quotas (`/v1/me/info`, familles, buckets — voir `albert.py`). On peut
  en déclarer plusieurs : deux comptes Albert avec des clés différentes
  (`"albert"`, `"albert2"`) ont chacun leurs limiteurs et leur refresh.
  Idem côté local : plusieurs machines llama.cpp = plusieurs entrées,
  chacune son préfixe. Les URLs ne vivent **que** dans `BACKENDS`
  (défaut si absent : Albert seul) ; `UPSTREAM_API_KEY` ne porte que le
  secret, hérité par les backends à quotas sans `api_key` dans le JSON.
- Les backends sans quota (llama.cpp, pas toujours allumés) ne sont
  **jamais sondés en tâche de fond** : ils sont contactés uniquement à
  la demande (connexion coupée au bout de 5 s). Backend éteint → 503
  `backend_offline` avec `Retry-After`.
- `GET /v1/models` interroge les backends **en direct** et renvoie le
  catalogue fusionné, chaque id préfixé par son backend (`albert/…`,
  `bigchuck/…`) — les noms renvoyés sont directement routables. Un
  backend éteint est simplement absent de la liste.
- Options par backend : `api_key` (`--api-key` de llama-server),
  `timeout` (secondes, défaut `UPSTREAM_TIMEOUT`), `verify_ssl: false`
  (certificat auto-signé).
- Le proxy ne relaie **que les routes nécessaires** : `POST
  /v1/chat/completions`, `GET /v1/models` et les chemins de
  `FORWARD_POST_PATHS`. Toute autre URL ou méthode → 404 local
  `unknown_route`, rien n'est transmis aveuglément aux backends.
- État visible dans `/healthz` → `backends`.

L'injection `tool_choice=auto` s'applique à tous les backends
(inoffensif, llama.cpp est OpenAI-compatible).

## Clé centralisée

Avec `UPSTREAM_API_KEY` défini, la clé Albert ne vit qu'ici. Les clients
n'ont plus rien à configurer : l'en-tête `Authorization` qu'ils envoient
est retiré et remplacé, et ceux qui n'en envoient pas fonctionnent aussi.

Côté pi, `apiKey` reste obligatoire dans `registerProvider()` — mettre
`"unused"` suffit. Côté Hermes, `api_key` peut recevoir la même valeur
bidon.

**Le proxy devient une clé Albert ouverte pour quiconque peut l'atteindre.**
C'est assumé (proxy public) — l'exposition se contrôle au niveau réseau
(Nginx Proxy Manager, Tailscale, réseau Docker partagé).

## Vérification

    curl http://localhost:8000/healthz

    curl -s http://localhost:8000/v1/chat/completions \
      -H "Content-Type: application/json" \
      -d '{"model":"albert/deepseek-v4-flash",
           "messages":[{"role":"user","content":"Liste /tmp"}],
           "tools":[{"type":"function","function":{"name":"list_dir",
             "parameters":{"type":"object","properties":{"path":{"type":"string"}},
             "required":["path"]}}}]}' \
      | jq '.choices[0].finish_reason'

Doit renvoyer `"tool_calls"`. Les logs du conteneur affichent alors :
`tool_choice=auto injecté (model=albert/deepseek-v4-flash, 1 tools)`.
Même chose côté local : `"model":"bigchuck/qwen3.8-27b"` part vers
llama.cpp (503 `backend_offline` si la machine est éteinte).

## Une fois en place

- Hermes : retirer `extra_body.tool_choice` du provider `albert` dans
  `~/.hermes/config.yaml`, pointer `api` sur le proxy.
- pi : retirer `patchFetchForAlbert()` de l'extension, pointer
  `ENDPOINT` sur le proxy.

## À terme

Ce proxy contourne ce qui ressemble à un bug côté Albert (le `default`
du schéma contredit la description du champ). Une issue chez Etalab
rendrait ce dépôt inutile — c'est le but.
