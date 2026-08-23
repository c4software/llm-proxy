# llm-proxy

Passerelle OpenAI-compatible qui expose **plusieurs backends LLM derrière
un seul endpoint** : Albert (DINUM), machines llama.cpp locales, ou tout
autre serveur compatible OpenAI. Le client parle à une seule URL et
choisit le backend par le **préfixe du nom de modèle**
(`albert/deepseek-v4-flash`, `bigchuck/qwen3-32b`).

![Tableau de bord /ui : cartes de synthèse (requêtes, tokens, modèles actifs, erreurs) et détail par modèle](preview.jpg)

## Fonctionnalités

- **Configuration en un seul fichier** — tout vit dans
  `data/config.toml` (voir [Configuration](#configuration)). L'environnement
  ne sert plus qu'aux **secrets**, injectés dans le TOML par `${VAR}`.
- **Routage multi-backends** — une table `[backends.<nom>]` par backend ;
  le nom est le discriminant de routage, retiré avant transfert
  (llama.cpp reçoit `qwen3-32b`, pas `bigchuck/qwen3-32b`).
- **Catalogue unifié** — `GET /v1/models` interroge tous les backends en
  direct, préfixe les ids et **normalise les entrées sur un schéma
  uniforme** (type, coûts, `max_context_length` dérivé du `--ctx-size`
  ou du `n_ctx_train` côté llama.cpp) ; les détails internes (chemins
  `.gguf`, args…) ne sont jamais publiés. Les noms renvoyés sont
  directement routables.
- **Limiteur de quotas Albert** — temporise les requêtes pour rester
  sous les limites du compte (fenêtres minute **et** jour, chargées via
  `/v1/me/info`, rafraîchies périodiquement). Retarde plutôt que
  rejeter ; si l'attente dépasse `quotas.max_queue_seconds` (quota journalier
  épuisé) → 429 local avec `Retry-After`. Un client qui raccroche pendant
  l'attente quitte la file sans rien consommer (compté `499`) : un SDK qui
  retente sur timeout n'empile pas de doublons facturés. Plusieurs backends
  à quotas possibles (deux comptes Albert = deux jeux de limiteurs
  indépendants).
- **Backends locaux à la demande** — jamais sondés en tâche de fond
  (souvent éteints) : connexion coupée à 5 s, backend éteint → 503
  `backend_offline` avec `Retry-After`, et simplement absent de
  `/v1/models`.
- **Clé centralisée** — la clé Albert ne vit que dans le proxy ;
  l'`Authorization` du client est remplacé. Les clients n'ont rien à
  configurer (une valeur bidon suffit si leur SDK exige une clé).
- **Correctif `tool_choice`** — quand `tools` est présent sans
  `tool_choice`, le proxy peut injecter `tool_choice: "auto"` (le schéma
  d'Albert déclare `"default": "none"`, ce qui casse le tool calling des
  agents). **Rien n'est injecté par défaut** : le correctif s'active
  backend par backend (`force_tool_choice`), là où il est nécessaire. Un
  `tool_choice` explicite du client n'est jamais écrasé.
- **Surface minimale** — seuls `POST /v1/chat/completions`,
  `GET /v1/models` et les chemins de `FORWARD_POST_PATHS` sont relayés ;
  toute autre URL → 404 local `unknown_route`. Le streaming SSE passe
  intact.
- **Observabilité** — `GET /healthz` expose l'état de chaque backend
  (URL, quotas restants par fenêtre, derniers modèles vus, réglage
  `tool_choice`) ; un résumé périodique des compteurs est loggé
  (`quotas.status_interval`).
- **Statistiques persistantes, à la forme d'OpenAI** —
  `GET /v1/organization/usage/completions` : **l'Usage API d'OpenAI**,
  servie depuis les compteurs du proxy. Une ligne SQLite par requête
  (`data/stats.db`), donc les chiffres survivent au redémarrage et toute
  fenêtre temporelle est calculable après coup. C'est la **seule**
  lecture des statistiques — il n'existe pas de format privé, et le
  SDK OpenAI officiel s'y branche tel quel (voir
  [Statistiques](#statistiques-usage-api)).
- **Tableau de bord** — `GET /ui` (ou `/`) : une page Vue 3 qui consomme
  cette même Usage API, en vues **Jour / Semaine / Tout**
  (voir [Tableau de bord](#tableau-de-bord)).

## Fichiers

| Fichier | Rôle |
|---|---|
| `main.py` | Point d'entrée (`uvicorn main:app`) — trois lignes, tout le code est dans le paquet |
| `llm_proxy/config.py` | Chargement de `data/config.toml` : substitution des `${VAR}`, accès typés, création du fichier depuis l'exemple au premier démarrage |
| `llm_proxy/settings.py` | La table `[proxy]`, en constantes typées |
| `llm_proxy/backends.py` | Déclaration des backends, clients HTTP, **routage au préfixe de modèle** |
| `llm_proxy/albert.py` | Tout ce qui est spécifique à Albert : limiteur de quotas (fenêtres minute/jour), familles de modèles, association routeurs ↔ modèles via `/v1/me/info` |
| `llm_proxy/stats.py` | Compteurs persistés en SQLite (une ligne par requête), extraction de l'`usage` dans le flux de réponse, et l'Usage API |
| `llm_proxy/app.py` | L'application FastAPI : routes, auth, relais, `/v1/models` fusionné |
| `llm_proxy/web/` | Le tableau de bord : `templates/index.html` (le gabarit Vue, servi tel quel) et `static/` (`dashboard.js`, `dashboard.css`, `vue.global.prod.js`) |
| `data/config.example.toml` | Le modèle de configuration, documenté — copié en `data/config.toml` au premier démarrage |

`app.py` ne connaît d'Albert que « un backend `quotas = true` passe par
sa `QuotaState` » ; toute la mécanique de quotas vit dans `albert.py`.

`data/` est le seul dossier écrit à l'exécution (`config.toml`,
`stats.db`) : c'est le volume à monter.

## Statistiques (Usage API)

Les compteurs sont **persistés** : une ligne SQLite par requête servie
(`data/stats.db`), écrite hors de la boucle d'événements par un thread
dédié. Ils survivent donc au redémarrage, et n'importe quelle fenêtre
temporelle se calcule après coup. Purge automatique au-delà de
`stats.retention_days` (90 jours par défaut, `0` = illimité).

La seule route de lecture est **l'Usage API d'OpenAI** :

    GET /v1/organization/usage/completions
        ?start_time=<epoch>        (obligatoire)
        &end_time=<epoch>
        &bucket_width=1m|1h|1d|all
        &group_by[]=model
        &models[]=albert/openweight-large
        &limit=<n>&page=<curseur>

Réponse : `page` → `bucket` → `result`, au schéma OpenAI exact. Le SDK
officiel s'y branche sans adaptation :

```python
from openai import OpenAI
c = OpenAI(base_url="http://localhost:8000/v1", api_key="x", admin_api_key="x")
page = c.admin.organization.usage.completions(
    start_time=..., bucket_width="1d", group_by=["model"], limit=7)
```

Deux écarts, **tous deux additifs** — un SDK ignore ce qu'il ne connaît
pas, la compatibilité reste entière :

- `bucket_width=all` en plus de `1m`/`1h`/`1d` : un seul seau couvrant
  toute la plage, pour obtenir un total sans agréger soi-même ;
- chaque `result` porte, en plus des champs du schéma, ce que le proxy
  sait mesurer et qu'OpenAI n'expose pas : `num_errors`,
  `num_streamed_requests`, `num_estimated_requests`,
  `total_latency_seconds`, `avg_latency_seconds`, `max_latency_seconds`,
  `first_request_time`, `last_request_time`.

**Toutes ces grandeurs s'agrègent sans perte** : elles s'additionnent
(requêtes, tokens, somme des latences) ou se maximisent (latence max).
Un client peut donc recomposer n'importe quelle période à partir de
seaux plus fins et retrouver *exactement* ce qu'aurait rendu un seau
unique — à condition d'aligner `start_time` sur la largeur des seaux,
puisqu'ils se calent sur des multiples depuis l'epoch. C'est ce qui
permet au tableau de bord de tout tirer d'un seul appel. Il n'y a
volontairement **pas de percentile** : un p95 n'a pas cette propriété,
il imposerait un appel séparé et une requête de tri par partition.

Le champ `model` est le nom **préfixé** (`albert/openweight-large`),
donc directement réutilisable comme `model` d'une requête. Les
dimensions que le proxy ne possède pas (`project_id`, `user_id`,
`api_key_id`, `batch`) valent `null` ; **filtrer** dessus rend une page
vide, plutôt que d'ignorer le filtre en silence et de sur-déclarer
l'usage.

Comptage des tokens : le bloc `usage` de l'upstream quand il existe —
streaming SSE compris —, sinon une estimation à ~4 caractères par
token. Les deux sont comptés séparément (`num_estimated_requests`) pour
que le chiffre affiché reste honnête.

## Tableau de bord

`GET /ui` (ou `/`) — c'est la copie d'écran ci-dessus. La page est servie
telle quelle : le serveur ne calcule aucun balisage. Le gabarit est
**déclaratif, écrit dans le HTML**, et rendu par **Vue 3** — build complet
embarqué localement (`/ui/static/vue.global.prod.js`), donc **aucun CDN et
aucune étape de compilation**. `dashboard.js` ne contient que l'état et
les valeurs dérivées ; rien n'y touche au DOM.

Trois vues, sélecteur en haut de page :

| Vue | Période | Découpage | Raccourci |
|---|---|---|---|
| Jour | 24 dernières heures | 1 heure | <kbd>D</kbd> |
| Semaine | 7 derniers jours | 1 jour | <kbd>W</kbd> |
| Tout | depuis le plus ancien enregistrement | 1 heure ou 1 jour, selon l'étendue | <kbd>A</kbd> |

Le choix est mémorisé (`localStorage`). **Un seul appel par
rafraîchissement** : les seaux de la période, groupés par modèle. Totaux,
ligne par modèle et courbe s'en déduisent, puisque tout ce qu'expose
l'API s'additionne ou se maximise. La borne de départ est alignée sur la
largeur des seaux, si bien que les chiffres du tableau portent exactement
sur ce que montre la courbe. Un second appel, léger, sert uniquement à
savoir jusqu'où remonte l'historique — au chargement et au changement de
période, jamais dans la boucle.

Rien n'est demandé tant que l'onglet est masqué ; au retour, la page se
rafraîchit immédiatement plutôt que d'afficher des chiffres périmés.

Le rafraîchissement de 5 s ne clignote pas : Vue rapproche les listes par
leur clé et ne réécrit que ce qui a changé, si bien que les nœuds
survivent d'un cycle à l'autre. Ils gardent donc leurs transitions en
cours, le survol et la sélection de texte — et les barres **glissent**
vers leur nouvelle hauteur au lieu d'y sauter.

Au changement de période, en revanche, le découpage change : les barres ne
représentent plus les mêmes seaux, les faire glisser d'une valeur à
l'autre n'aurait pas de sens. Elles remontent donc de zéro, en cascade
(`@keyframes` armés par la classe `entering`, posée pour la seule durée de
l'animation). La barre de répartition, elle, garde ses segments et glisse
— rien à l'ouverture de la page, une barre qui se remplit au chargement se
remarque pour rien.

La courbe **Trafic** ne porte qu'une mesure — les requêtes —, donc un seul
axe et une seule couleur ; les tokens sont dans l'infobulle, en CSS pur.
Les seaux vides sont dessinés eux aussi : un creux doit se voir comme un
creux.

Les chiffres sont lus sur `/ui/usage`, qui est la même route que
`/v1/organization/usage/completions`. Ce doublon n'existe que pour l'auth :
un `fetch` de navigateur ne peut pas porter l'en-tête `Authorization`,
alors que le cookie posé par `/ui` vaut pour tout ce qui est sous `/ui` —
et pour rien d'autre, si bien qu'il ne peut jamais servir à dépenser des
tokens. Si `proxy.api_keys` est renseigné, ouvrir `/ui?key=<clé>` une
fois : la clé est ensuite mémorisée dans un cookie `HttpOnly`.

Le badge **exact / estimé** de la colonne *Comptage* dit d'où viennent les
tokens : le bloc `usage` de l'upstream, ou l'estimation de repli
(streaming sans `stream_options.include_usage`).

## Déploiement

### Docker Compose

    cp .env.example .env        # y mettre ALBERT_API_KEY
    docker compose up -d --build

`./data` est monté comme volume : il porte la configuration
(`config.toml`, créée au premier démarrage depuis l'exemple) **et** la
base de statistiques (`stats.db`), qui survit ainsi aux redémarrages et
aux reconstructions d'image.

### Coolify

Nouvelle ressource → Dockerfile, pointer sur ce dépôt. Port 8000.
Déclarer un **volume persistant sur `/app/data`** (configuration et base
de statistiques), ajouter les secrets en variables d'environnement
(`ALBERT_API_KEY`…), puis exposer le service via Nginx Proxy Manager sur
un sous-domaine interne. Les réglages se modifient ensuite dans
`data/config.toml`, dans le volume.

## Configuration

**Tout vit dans `data/config.toml`.** Le fichier est créé au premier
démarrage à partir de `data/config.example.toml`, qui est documenté ligne
à ligne — c'est la référence à lire. L'environnement ne sert plus qu'à
deux choses :

- `CONFIG_PATH` : où trouver le TOML (défaut `data/config.toml`) ;
- les **secrets** : toute chaîne du TOML peut contenir `${VAR}`, remplacé
  au chargement par la variable d'environnement. Les clés d'API restent
  ainsi hors du fichier, donc hors du dépôt, tandis que la structure
  reste versionnable.

`data/config.toml` et `data/stats.db` sont dans `.gitignore` : le dépôt
ne garde que l'exemple.

### Backends

Une table `[backends.<nom>]` par backend — **le nom est le préfixe de
routage**, et ces tables sont la seule source de vérité des URLs :

```toml
[backends.albert]
url = "https://albert.api.etalab.gouv.fr"
api_key = "${ALBERT_API_KEY}"
quotas = true
force_tool_choice = "auto"

[backends.bigchuck]
url = "http://bigchuck:8009"
```

Champs : `api_key` (**la clé du backend vit ici**, et nulle part ailleurs
— clé Albert, ou `--api-key` de llama-server ; si définie, elle remplace
l'`Authorization` du client), `quotas` (active le limiteur Albert),
`timeout` (secondes, défaut `proxy.upstream_timeout`), `meta_timeout`
(secondes pour les appels de méta-données — `/v1/models`, `/v1/me/info`
—, défaut `proxy.meta_timeout`), `verify_ssl = false` (certificat
auto-signé), `force_tool_choice` (injection de `tool_choice` : absent ou
`false` → **aucune injection**, c'est le défaut ; `true` → la valeur de
`proxy.tool_choice` ; une chaîne (`"auto"`, `"required"`…) → cette
valeur-là). Seule exception : si `[backends]` est absent du TOML, le
backend Albert par défaut est livré avec `"auto"`, puisque c'est lui que
le correctif vise.

**Tout modèle doit être préfixé** : préfixe inconnu → 400
`unknown_backend_prefix`. Seules les requêtes sans champ `model`
(endpoints de compte, corps non JSON) partent vers le backend à quotas.

### `[proxy]`

| Clé | Défaut | Rôle |
|---|---|---|
| `api_keys` | `[]` | Clé(s) exigée(s) **des clients** pour appeler le proxy (`Authorization: Bearer <clé>`, à la OpenAI). Liste vide = proxy ouvert ; 401 sinon, `/healthz` exempté ; `/ui` accepte aussi `?key=<clé>` (puis cookie) |
| `upstream_timeout` | `600` | Secondes ; large pour les longues générations |
| `meta_timeout` | `5` | Secondes pour `/v1/models`, `/v1/me/info` — court, un backend lent ne doit pas bloquer le catalogue |
| `tool_choice` | `"auto"` | Valeur injectée quand `tools` est présent sans `tool_choice`, pour les backends ayant `force_tool_choice = true`. L'injection est désactivée par défaut : elle s'active par backend |
| `forward_post_paths` | `["/v1/completions", "/v1/embeddings", "/v1/rerank", "/v1/audio/transcriptions", "/v1/ocr"]` | Routes POST relayées en plus des handlers dédiés ; le reste → 404 |
| `exempt_paths` | `["/embeddings", "/rerank", "/audio/transcriptions", "/ocr"]` | Suffixes de routes exclus du limiteur |
| `log_level` | `"INFO"` | `INFO` logue chaque injection et chaque mise en attente |

### `[stats]`

| Clé | Défaut | Rôle |
|---|---|---|
| `database` |  `"stats.db"` | Base SQLite ; chemin relatif = à côté de `config.toml` |
| `retention_days` | `90` | Purge des lignes plus anciennes (`0` = conservation illimitée) |

### `[quotas]` (backends à quotas)

| Clé | Défaut | Rôle |
|---|---|---|
| `margin` | `0.9` | Fraction des limites réellement utilisée (marge de sécurité) |
| `max_queue_seconds` | `900` | Attente max avant 429 local (quota journalier épuisé) |
| `limits_refresh` | `3600` | Période de rechargement de `/v1/me/info` (0 = jamais) |
| `generic_rpm` / `generic_tpm` | `30` / `128000` | Limites des modèles hors familles connues |
| `status_interval` | `600` | Période du résumé des compteurs dans les logs |
| `[quotas.family_limits.<famille>]` | *intégré* | `rpm`, `tpm`, `models = [préfixes]` — limites statiques de repli par famille |
| `[quotas.router_models]` | *(vide)* | `<router_id> = ["préfixe", …]` — association manuelle routeurs ↔ modèles, prioritaire sur la détection par signature |

### Association routeurs ↔ modèles (Albert)

`/v1/me/info` donne les limites par `router_id` mais pas quels modèles
chaque routeur sert. Le proxy reconstruit le mapping par **signature** :
chaque famille de modèles (`[quotas.family_limits]`) est rattachée au
routeur du compte qui porte ses (rpm, tpm). Si l'association est
ambiguë, la famille reste sur ses limites statiques ;
`[quotas.router_models]` permet de la fixer manuellement. Id et alias
(`openai/gpt-oss-120b` ↔ `openweight-large`) partagent le même compteur.

## Sécurité

Avec une `api_key` configurée sur un backend, **le proxy devient une
clé Albert ouverte pour quiconque peut l'atteindre**. Deux parades,
cumulables :

- **`proxy.api_keys`** : exige des clients une clé à la OpenAI
  (`Authorization: Bearer <clé>`) — sans elle, 401. Quand l'auth est
  active, ni le Bearer du client ni ses cookies ne sont relayés aux
  backends (c'est la clé du proxy, pas de l'upstream). Le tableau de bord `/ui` est
  soumis à la même clé : un navigateur ne pouvant pas poser d'en-tête,
  elle s'y passe une fois en `?key=<clé>` puis est mémorisée dans un
  cookie `HttpOnly` (`SameSite=Strict`).
- **Le réseau** : Nginx Proxy Manager, Tailscale, réseau Docker partagé.

## Vérification

    curl http://localhost:8000/healthz

    curl -s http://localhost:8000/v1/models | jq '.data[].id'

    # tableau de bord : http://localhost:8000/ui
    # (si proxy.api_keys est renseigné : http://localhost:8000/ui?key=<clé>,
    #  la clé est ensuite mémorisée en cookie ; D / W / A changent de vue)

    # usage par modèle, tout l'historique (Usage API OpenAI)
    curl -s "http://localhost:8000/v1/organization/usage/completions\
?start_time=0&bucket_width=all&group_by[]=model" \
      | jq '.data[0].results[] | {model, num_model_requests,
                                  tokens: (.input_tokens + .output_tokens)}'

    # les 7 derniers jours, un seau par jour
    curl -s "http://localhost:8000/v1/organization/usage/completions\
?start_time=$(( $(date +%s) - 604800 ))&bucket_width=1d&limit=8" \
      | jq '.data[] | {jour: (.start_time | todate),
                       req: ([.results[].num_model_requests] | add // 0)}'

    curl -s http://localhost:8000/v1/chat/completions \
      -H "Content-Type: application/json" \
      -d '{"model":"albert/deepseek-v4-flash",
           "messages":[{"role":"user","content":"Liste /tmp"}],
           "tools":[{"type":"function","function":{"name":"list_dir",
             "parameters":{"type":"object","properties":{"path":{"type":"string"}},
             "required":["path"]}}}]}' \
      | jq '.choices[0].finish_reason'

Doit renvoyer `"tool_calls"`, avec dans les logs (si le backend a
`force_tool_choice`) :
`tool_choice=auto injecté (backend=albert, model=albert/deepseek-v4-flash, 1 tools)`.
Le réglage de chaque backend est rappelé au démarrage et lisible dans
`/healthz` (`tool_choice: false` = aucune injection). Même chose côté
local : `"model":"bigchuck/qwen3-32b"` part vers llama.cpp (503
`backend_offline` si la machine est éteinte).

## Côté clients

- Hermes : retirer `extra_body.tool_choice` du provider dans
  `~/.hermes/config.yaml`, pointer `api` sur le proxy.
- pi : retirer `patchFetchForAlbert()` de l'extension, pointer
  `ENDPOINT` sur le proxy ; `apiKey` reste obligatoire dans
  `registerProvider()` — `"unused"` suffit.
