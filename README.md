# llm-proxy

Passerelle OpenAI-compatible qui expose **plusieurs backends LLM derrière
un seul endpoint** : Albert (DINUM), machines llama.cpp locales, ou tout
autre serveur compatible OpenAI. Le client parle à une seule URL et
choisit le backend par le **préfixe du nom de modèle**
(`albert/deepseek-v4-flash`, `bigchuck/qwen3-32b`).

## Fonctionnalités

- **Routage multi-backends** — `BACKENDS` (JSON en env) déclare les
  backends ; le préfixe du modèle est le discriminant de routage, retiré
  avant transfert (llama.cpp reçoit `qwen3-32b`, pas `bigchuck/qwen3-32b`).
- **Catalogue unifié** — `GET /v1/models` interroge tous les backends en
  direct, préfixe les ids et **normalise les entrées sur un schéma
  uniforme** (type, coûts, `max_context_length` dérivé du `--ctx-size`
  ou du `n_ctx_train` côté llama.cpp) ; les détails internes (chemins
  `.gguf`, args…) ne sont jamais publiés. Les noms renvoyés sont
  directement routables.
- **Limiteur de quotas Albert** — temporise les requêtes pour rester
  sous les limites du compte (fenêtres minute **et** jour, chargées via
  `/v1/me/info`, rafraîchies périodiquement). Retarde plutôt que
  rejeter ; si l'attente dépasse `MAX_QUEUE_SECONDS` (quota journalier
  épuisé) → 429 local avec `Retry-After`. Plusieurs backends à quotas
  possibles (deux comptes Albert = deux jeux de limiteurs indépendants).
- **Backends locaux à la demande** — jamais sondés en tâche de fond
  (souvent éteints) : connexion coupée à 5 s, backend éteint → 503
  `backend_offline` avec `Retry-After`, et simplement absent de
  `/v1/models`.
- **Clé centralisée** — la clé Albert ne vit que dans le proxy ;
  l'`Authorization` du client est remplacé. Les clients n'ont rien à
  configurer (une valeur bidon suffit si leur SDK exige une clé).
- **Correctif `tool_choice`** — si `tools` est présent sans
  `tool_choice`, injecte `tool_choice: "auto"` (le schéma d'Albert
  déclare `"default": "none"`, ce qui casse le tool calling des agents).
  Un `tool_choice` explicite n'est jamais écrasé ; inoffensif pour les
  autres backends.
- **Surface minimale** — seuls `POST /v1/chat/completions`,
  `GET /v1/models` et les chemins de `FORWARD_POST_PATHS` sont relayés ;
  toute autre URL → 404 local `unknown_route`. Le streaming SSE passe
  intact.
- **Observabilité** — `GET /healthz` expose l'état de chaque backend
  (URL, quotas restants par fenêtre, derniers modèles vus) ; un résumé
  périodique des compteurs est loggé (`STATUS_INTERVAL`).
- **Tableau de bord** — `GET /ui` (ou `/`) : page HTML + HTMX qui
  consomme ces compteurs. Le rafraîchissement est **différentiel** : le
  sondage (5 s) envoie la révision affichée, le serveur répond `204` si
  rien n'a bougé, sinon il ne renvoie **que les valeurs concernées**
  (swaps *out of band* ciblés) — aucune ligne, aucune carte, aucune
  structure n'est redessinée, et les « il y a 3 min » vieillissent côté
  navigateur. HTMX est servi par le proxy, aucun CDN.
- **Statistiques d'usage** — `GET /v1/stats` : compteurs **par modèle**
  (nom préfixé), alimentés en lisant l'`usage` des réponses upstream —
  streaming SSE compris — avec repli sur une estimation quand l'upstream
  n'en renvoie pas. Le retour est **dynamique** : un modèle n'apparaît
  qu'à partir de sa première requête servie. En mémoire, remis à zéro au
  redémarrage.

## Fichiers

| Fichier | Rôle |
|---|---|
| `main.py` | La passerelle : backends, routage au préfixe, `/v1/models` fusionné, HTTP |
| `stats.py` | Compteurs d'usage par modèle (requêtes, tokens, latences) et extraction de l'`usage` dans le flux de réponse |
| `ui.py` | Tableau de bord `/ui` : prépare les valeurs et décide quoi envoyer (rien / valeurs / structure). Aucun balisage |
| `templates/` | Le HTML du tableau de bord (Jinja2) : `page.html`, `row.html`, `share.html`, `update.html`, `macros.html` |
| `static/` | `dashboard.css`, `dashboard.js` et HTMX 2.0.4 vendorisé (tableau de bord fonctionnel hors ligne) |
| `albert.py` | Tout ce qui est spécifique à Albert : limiteur de quotas (fenêtres minute/jour), familles de modèles, association routeurs ↔ modèles via `/v1/me/info` |

`main.py` ne connaît d'Albert que « un backend `quotas: true` passe par
sa `QuotaState` » ; toute la mécanique de quotas vit dans `albert.py`.

## Déploiement

### Docker Compose

    docker compose up -d --build

### Coolify

Nouvelle ressource → Dockerfile, pointer sur ce dépôt. Port 8000.
Ajouter les variables d'environnement ci-dessous, puis exposer le
service via Nginx Proxy Manager sur un sous-domaine interne.

## Configuration

### Backends

`BACKENDS` est la **seule source de vérité des URLs** — le nom de chaque
entrée est le préfixe de routage :

    BACKENDS: |
      {
        "albert":   {"url": "https://albert.api.etalab.gouv.fr",
                     "quotas": true},
        "bigchuck": {"url": "http://bigchuck:8009"}
      }

Options par backend : `api_key` (**la clé du backend vit ici**, et
nulle part ailleurs — clé Albert, ou `--api-key` de llama-server ; si
définie, elle remplace l'`Authorization` du client), `quotas` (active
le limiteur Albert), `timeout` (secondes, défaut `UPSTREAM_TIMEOUT`),
`meta_timeout` (secondes pour les appels de méta-données — `/v1/models`,
`/v1/me/info` —, défaut `5`), `verify_ssl: false` (certificat
auto-signé).

**Tout modèle doit être préfixé** : préfixe inconnu → 400
`unknown_backend_prefix`. Seules les requêtes sans champ `model`
(endpoints de compte, corps non JSON) partent vers le backend à quotas.

### Variables générales

| Variable | Défaut | Rôle |
|---|---|---|
| `BACKENDS` | *Albert seul* | Déclaration des backends (voir ci-dessus) |
| `PROXY_API_KEY` | *(vide)* | Clé(s) exigée(s) **des clients** pour appeler le proxy (`Authorization: Bearer <clé>`, à la OpenAI). Vide = proxy ouvert. Plusieurs clés séparées par des virgules ; 401 sinon, `/healthz` exempté ; `/ui` accepte aussi `?key=<clé>` (puis cookie) |
| `UPSTREAM_TIMEOUT` | `600` | Secondes ; large pour les longues générations |
| `FORCE_TOOL_CHOICE` | `auto` | Valeur injectée quand `tools` est présent sans `tool_choice` |
| `FORWARD_POST_PATHS` | `/v1/completions,/v1/embeddings,/v1/rerank,/v1/audio/transcriptions,/v1/ocr` | Routes POST relayées en plus des handlers dédiés ; le reste → 404 |
| `LOG_LEVEL` | `INFO` | `INFO` logue chaque injection et chaque mise en attente |
| `STATS_LATENCY_SAMPLES` | `500` | Échantillons de latence gardés par modèle pour le p95 de `/v1/stats` |
| `STATS_MAX_BODY_BYTES` | `2097152` | Taille max d'une réponse non streamée bufferisée pour y lire l'`usage` ; au-delà, estimation |

### Variables du limiteur (backends à quotas)

| Variable | Défaut | Rôle |
|---|---|---|
| `RATE_LIMIT_MARGIN` | `0.9` | Fraction des limites réellement utilisée (marge de sécurité) |
| `MAX_QUEUE_SECONDS` | `900` | Attente max avant 429 local (quota journalier épuisé) |
| `LIMITS_REFRESH` | `3600` | Période de rechargement de `/v1/me/info` (0 = jamais) |
| `GENERIC_RPM` / `GENERIC_TPM` | `30` / `128000` | Limites des modèles hors familles connues |
| `FAMILY_LIMITS` | *intégré* | JSON `{"<famille>": {rpm, tpm, models: [préfixes]}}` — limites statiques de repli par famille |
| `ROUTER_MODELS` | *(vide)* | JSON `{"<router_id>": ["préfixe", ...]}` — association manuelle routeurs ↔ modèles, prioritaire sur la détection par signature |
| `EXEMPT_PATHS` | `/embeddings,/rerank,/audio/transcriptions,/ocr` | Suffixes de routes exclus du limiteur |
| `STATUS_INTERVAL` | `600` | Période du résumé des compteurs dans les logs |

### Association routeurs ↔ modèles (Albert)

`/v1/me/info` donne les limites par `router_id` mais pas quels modèles
chaque routeur sert. Le proxy reconstruit le mapping par **signature** :
chaque famille de modèles (`FAMILY_LIMITS`) est rattachée au routeur du
compte qui porte ses (rpm, tpm). Si l'association est ambiguë, la
famille reste sur ses limites statiques ; `ROUTER_MODELS` permet de la
fixer manuellement. Id et alias (`openai/gpt-oss-120b` ↔
`openweight-large`) partagent le même compteur.

## Sécurité

Avec une `api_key` configurée sur un backend, **le proxy devient une
clé Albert ouverte pour quiconque peut l'atteindre**. Deux parades,
cumulables :

- **`PROXY_API_KEY`** : exige des clients une clé à la OpenAI
  (`Authorization: Bearer <clé>`) — sans elle, 401. Quand l'auth est
  active, le Bearer du client n'est jamais relayé aux backends (c'est
  la clé du proxy, pas de l'upstream). Le tableau de bord `/ui` est
  soumis à la même clé : un navigateur ne pouvant pas poser d'en-tête,
  elle s'y passe une fois en `?key=<clé>` puis est mémorisée dans un
  cookie `HttpOnly` (`SameSite=Strict`).
- **Le réseau** : Nginx Proxy Manager, Tailscale, réseau Docker partagé.

## Vérification

    curl http://localhost:8000/healthz

    curl -s http://localhost:8000/v1/models | jq '.data[].id'

    # tableau de bord : http://localhost:8000/ui
    # (si PROXY_API_KEY est défini : http://localhost:8000/ui?key=<clé>,
    #  la clé est ensuite mémorisée en cookie)

    # usage par modèle (seuls les modèles ayant généré apparaissent)
    curl -s http://localhost:8000/v1/stats \
      | jq '.data[] | {id, requests, tokens: .usage.total_tokens}'

    curl -s http://localhost:8000/v1/chat/completions \
      -H "Content-Type: application/json" \
      -d '{"model":"albert/deepseek-v4-flash",
           "messages":[{"role":"user","content":"Liste /tmp"}],
           "tools":[{"type":"function","function":{"name":"list_dir",
             "parameters":{"type":"object","properties":{"path":{"type":"string"}},
             "required":["path"]}}}]}' \
      | jq '.choices[0].finish_reason'

Doit renvoyer `"tool_calls"`, avec dans les logs :
`tool_choice=auto injecté (model=albert/deepseek-v4-flash, 1 tools)`.
Même chose côté local : `"model":"bigchuck/qwen3-32b"` part vers
llama.cpp (503 `backend_offline` si la machine est éteinte).

## Côté clients

- Hermes : retirer `extra_body.tool_choice` du provider dans
  `~/.hermes/config.yaml`, pointer `api` sur le proxy.
- pi : retirer `patchFetchForAlbert()` de l'extension, pointer
  `ENDPOINT` sur le proxy ; `apiKey` reste obligatoire dans
  `registerProvider()` — `"unused"` suffit.
