# llm-proxy

Passerelle LLM qui met **plusieurs backends derrière un seul endpoint** et
laisse **chaque client parler sa propre API**. Les backends sont des
serveurs compatibles OpenAI : machines locales (llama.cpp, gufo), service
hébergé à quotas (Albert, de la DINUM), ou tout autre. Le client vise une
seule URL et choisit le backend par le **préfixe du nom de modèle**
(`bigchuck/qwen3.8-flash-next`, `albert/deepseek-v4-flash`).

Trois API côté client, une seule côté backend (`/v1/chat/completions`) :

- **OpenAI** (pi, omp, Hermes, tout SDK OpenAI) : relayée telle quelle.
- **Anthropic Messages** (Claude Code) : traduite.
- **OpenAI Responses** (Codex CLI) : traduite.

Autour du relais : un catalogue `/v1/models` unifié, des **outils web
hébergés** (recherche par une instance SearXNG, lecture de page) que le
proxy exécute pour le modèle, un limiteur de quotas pour les backends qui
en ont, des statistiques persistantes à la forme de l'Usage API d'OpenAI
et un tableau de bord.

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
- **Limiteur de quotas** — pour un backend à quotas (`quotas = true` ;
  aujourd'hui Albert, dont le limiteur connaît le compte) : temporise les
  requêtes pour rester
  sous les limites du compte (fenêtres minute **et** jour, chargées via
  `/v1/me/info`, rafraîchies périodiquement). Retarde plutôt que
  rejeter ; si l'attente dépasse `quotas.max_queue_seconds` (quota journalier
  épuisé) → 429 local avec `Retry-After`. Un client qui raccroche pendant
  l'attente quitte la file sans rien consommer (compté `499`) : un SDK qui
  retente sur timeout n'empile pas de doublons facturés. Plusieurs backends
  à quotas possibles (deux comptes Albert = deux jeux de limiteurs
  indépendants).
- **Backends locaux à la demande** — jamais sondés en tâche de fond
  (souvent éteints) : connexion coupée à 1 s, backend éteint → 503
  `backend_offline` avec `Retry-After`, et simplement absent de
  `/v1/models`.
- **Clés centralisées** — la clé d'un backend (`api_key`) ne vit que
  dans le proxy ; l'`Authorization` du client est remplacé. Les clients n'ont rien à
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
- **Audio et images** — synthèse (`/v1/audio/speech`), transcription,
  génération et édition d'image passent par le même routage au préfixe
  de modèle. Les corps multipart (transcription, édition) sont routés
  d'après leur champ `model`, préfixe retiré, le reste recopié octet pour
  octet (`llm_proxy/multipart.py`) ; sans cela ils partaient vers le
  backend de repli. `GET /v1/audio/voices?model=<backend>/<modèle>` liste
  les voix du modèle de synthèse.
- **Compatible Claude Code** — si `[anthropic].enabled`, le proxy parle
  aussi l'**API Messages d'Anthropic** : `POST /v1/messages` (JSON et
  flux SSE, outils compris), `/v1/messages/count_tokens`, et
  `GET /v1/models` à la forme Anthropic. Traduit vers
  `/v1/chat/completions` du backend visé — **uniquement dans ce sens**,
  aucun backend Anthropic. Voir [Claude Code](#claude-code).
- **Compatible Codex CLI** — si `[responses].enabled`, le proxy parle
  aussi l'**API Responses d'OpenAI** : `POST /v1/responses` (JSON et
  flux d'événements `response.*`, outils compris), traduit vers
  `/v1/chat/completions` du backend visé — aucun backend n'a besoin de
  servir `/v1/responses`. Voir [Codex CLI](#codex-cli).
- **Outils hébergés** — si `[tools.web_search].enabled`, le `web_search`
  que déclare un client Responses n'est plus ignoré : le proxy
  l'**exécute lui-même** (recherche par une instance SearXNG livrée dans
  le `docker-compose.yml`, lecture de pages par `web_fetch`), relance le
  backend avec le résultat et rend au client des éléments
  `web_search_call`. Même service pour l'outil serveur
  `web_search_20250305` d'un client Anthropic — celui par lequel passe
  le `WebSearch` de Claude Code —, rendu en blocs `server_tool_use` /
  `web_search_tool_result`. Si `[tools.image_generation].enabled`, même
  principe pour l'`image_generation` d'un client Responses : le proxy
  appelle la route images du backend configuré et rend un élément
  `image_generation_call` portant l'image. Voir
  [Outils hébergés](#outils-hébergés).
- **Plafond `max_tokens`** — optionnel, par backend : la valeur du
  client est ramenée au plafond (Claude Code en demande 32 000).
- **Observabilité** — `GET /healthz` expose l'état de chaque backend
  (URL, quotas restants par fenêtre, derniers modèles vus, réglages
  `tool_choice` / `max_tokens` / `images` / `tokenize_path`) et de la
  surface Anthropic ; un résumé périodique des compteurs est loggé
  (`quotas.status_interval`), et toute erreur upstream (4xx/5xx) l'est
  avec le début de son corps — le client, lui, ne voit souvent qu'un
  statut.
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
| `llm_proxy/anthropic_api.py` | La surface Anthropic : traduction Messages ↔ chat/completions, flux SSE compris ; `model_map` ; outil serveur `web_search_…` remplacé par la recherche hébergée, rendue et rejouée en blocs `server_tool_use` / `web_search_tool_result` |
| `llm_proxy/responses_api.py` | La surface Responses : traduction Responses ↔ chat/completions, flux d'événements compris ; outils hébergés par le proxy présentés au modèle et rejoués, les autres ignorés, `namespace` aplatis |
| `llm_proxy/tools/__init__.py` | Les outils hébergés, ce qui leur est commun : registre, exécution bornée (délai, taille du résultat, nombre d'appels par réponse), **mémoire des résultats** |
| `llm_proxy/tools/net.py` | Garde-fou réseau : résolution du nom par le proxy, adresses **publiques** seulement, connexion vers l'adresse vérifiée |
| `llm_proxy/tools/html_text.py` | HTML → texte lisible par un modèle, bibliothèque standard seule (titres, paragraphes, listes, liens, blocs de code) |
| `llm_proxy/tools/web_search.py` | L'outil `web_search` : requête JSON à SearXNG, résultats numérotés (titre, date, URL, extrait) — en texte pour le modèle, en liste structurée pour les blocs d'un client Anthropic ; filtre par domaines |
| `llm_proxy/tools/web_fetch.py` | L'outil `web_fetch` : lecture d'une page par son URL, redirections suivies saut par saut sous le garde-fou, tailles bornées |
| `llm_proxy/tools/image_generation.py` | L'outil `image_generation` : requête `/v1/images/generations` au backend du modèle d'image configuré ; un texte court pour le modèle, l'image en base64 pour l'élément du client |
| `llm_proxy/multipart.py` | Le champ `model` d'un corps multipart/form-data : lu pour router, réécrit pour retirer le préfixe |
| `llm_proxy/app.py` | L'application FastAPI : routes, auth, relais, `/v1/models` fusionné |
| `tests/` | Tests des traducteurs et des stats (`pytest`, `requirements-dev.txt`) — sur des octets et une base temporaire, sans réseau |
| `envTest/` | Validation avec de **vrais clients** en conteneurs jetables : Claude Code, pi et Codex CLI, scénarios PASS/FAIL — voir `envTest/README.md` |
| `llm_proxy/web/` | Le tableau de bord : `templates/index.html` (le gabarit Vue, servi tel quel) et `static/` (`dashboard.js`, `dashboard.css`, `vue.global.prod.js`) |
| `data/config.example.toml` | Le modèle de configuration, documenté — copié en `data/config.toml` au premier démarrage |
| `searxng/settings.yml` | Réglages de l'instance SearXNG du compose : les défauts de SearXNG, plus le format JSON. Monté en lecture seule dans le service `searxng` |

`app.py` ne connaît d'Albert que « un backend `quotas = true` passe par
sa `QuotaState` » ; toute la mécanique de quotas vit dans `albert.py`.

Chaque requête relayée est portée par un objet `Call` (backend, modèle
demandé, endpoint, dialecte du client) : c'est lui qui écrit la ligne de
stats — une fois, quel que soit le chemin de sortie — et construit les
erreurs à la forme attendue (`{"error": {…}}` ou, pour un client
Anthropic, `{"type": "error", …}`). La porte de quota (`gate`) et le
relais (`forward`) sont communs à toutes les routes ; `forward` fait
passer les octets upstream par un « robinet » — `stats.UsageCollector`
(identité, lit l'`usage` au passage), `anthropic_api.Translator` ou
`responses_api.Translator` (réécrivent la réponse). Ces deux-là portent
aussi les outils hébergés, par un même contrat (`pending`, `resolve`,
`next_turn`, `finalize`, `fail`) : une seule boucle, `hosted_loop`,
exécute les appels et relance le backend pour les deux surfaces.

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
- `input_cached_tokens` est **renseigné** quand le backend dit ce qu'il a
  servi depuis son cache de préfixe (`prompt_tokens_details.cached_tokens`
  — llama.cpp, vLLM, OpenAI) ; `0` sinon, jamais une valeur inventée.
  Inclus dans `input_tokens`, comme chez OpenAI ;
- chaque `result` porte, en plus des champs du schéma, ce que le proxy
  sait mesurer et qu'OpenAI n'expose pas : `num_errors`,
  `num_streamed_requests`, `num_estimated_requests`,
  `num_anthropic_requests` (arrivées par `/v1/messages`, donc Claude
  Code), `total_latency_seconds`, `avg_latency_seconds`,
  `max_latency_seconds`, `first_request_time`, `last_request_time`.

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
que le chiffre affiché reste honnête. Une requête **en erreur** sans
`usage` (500 upstream, 429 local, client parti) compte **0 token,
exact** : rien n'a été consommé de mesurable, et le corps envoyé n'a pas
à gonfler l'entrée.

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

Et **deux lectures du même trafic**, second sélecteur juste à côté :
*Requêtes* (<kbd>R</kbd>) ou *Tokens* (<kbd>T</kbd>). C'est la mesure que
porte la courbe **Trafic** — hauteur des barres, partage entre modèles,
pic annoncé. Les deux grandeurs sont déjà dans la réponse : basculer ne
redemande rien au proxy, les mêmes barres glissent simplement vers leur
nouvelle hauteur.

Les deux choix sont mémorisés (`localStorage`). **Un seul appel par
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

La courbe **Trafic** ne porte **qu'une mesure à la fois** — requêtes *ou*
tokens, au choix du sélecteur —, donc un seul axe, jamais deux échelles
superposées ; chaque barre est **empilée par modèle**, aux couleurs de la
répartition, le même modèle toujours au même étage d'un seau à l'autre.
L'autre grandeur et le détail par modèle sont dans l'infobulle, en CSS
pur.
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

La carte *Requêtes* et la ligne de chaque modèle comptent celles
arrivées par `/v1/messages` (Claude Code).

La colonne **Cache** dit quelle part de l'entrée le backend a servie
depuis son cache de préfixe, quand il le remonte (llama.cpp, vLLM) ;
`—` sinon (Albert). C'est la réponse à « le cache marche-t-il ? » : avec
Claude Code, un second tour qui rejoue les ~20 k tokens du prompt
système doit approcher 100 %.

En bas de page, le panneau repliable **Brancher un client** donne des
commandes prêtes à coller — catalogue, `curl`, SDK OpenAI, Claude Code
(avec l'état actif / inactif de la surface Anthropic) — dérivées de
l'URL de la page, de l'auth et des modèles connus (`/healthz`, lu une
fois, jamais dans la boucle) et du modèle le plus actif de la période.

## Claude Code

Avec `[anthropic].enabled = true` dans `config.toml`, Claude Code (ou
tout SDK Anthropic) se branche sur le proxy sans rien d'autre :

    export ANTHROPIC_BASE_URL=http://localhost:8000
    export ANTHROPIC_API_KEY=<clé de proxy.api_keys, ou n'importe quoi si ouvert>
    export ANTHROPIC_MODEL=albert/deepseek-v4-flash
    # tâches d'arrière-plan (titres, résumés…) sur un backend local :
    # export ANTHROPIC_SMALL_FAST_MODEL=bigchuck/qwen3-8b
    claude

Dans `~/.claude/settings.json`, `{"env": {"CLAUDE_CODE_ATTRIBUTION_HEADER":
"0"}}` retire l'attribution que Claude Code ajoute à ses requêtes —
variable d'une fois à l'autre, elle décale le préfixe et fait manquer le
cache du backend (colonne *Cache* du tableau de bord pour le vérifier).

Validé avec un vrai Claude Code et avec pi, en conteneurs, sur des
scénarios d'outils et de création de code — voir `envTest/`.

Ce qui se passe :

- `POST /v1/messages` est traduit en `/v1/chat/completions`, la réponse
  retraduite : objet `Message`, ou suite d'événements SSE
  (`message_start` → blocs → `message_delta` → `message_stop`), outils
  fragmentés compris. En flux, `stream_options.include_usage` est
  demandé : les stats sont **exactes**.

  | Côté Anthropic | Côté OpenAI |
  |---|---|
  | `system` (chaîne ou blocs) | message `system` en tête |
  | `system` **en cours** de conversation (rappels de Claude Code) | fondu en tête du message `user` suivant — les gabarits Qwen / Mistral refusent un `system` ailleurs qu'en tête (500) |
  | bloc `text` | texte |
  | bloc `image` | `image_url` (data URI) si le backend a `images = true` **et** que le modèle est multimodal à son catalogue ; sinon `[image ignorée : image/png, 12 Ko]` |
  | bloc `document` | le texte s'il en est ; un PDF → `[document ignoré : …]` |
  | `tool_use` (assistant) | `tool_calls[]`, arguments sérialisés |
  | `tool_result` (user) | un message `tool` **par résultat**, placés avant le reste du message ; une image dans le résultat suit dans un message `user` |
  | `thinking` / `redacted_thinking` | jetés (aucun backend ne les rejoue) |
  | `tools[{name, input_schema}]` | `tools[{type: function, …parameters}]` |
  | outil serveur `web_search_…` (`{"type": "web_search_20250305", "name": "web_search"}`) | la fonction `web_search` du proxy, exécutée par lui, si `[tools.web_search].enabled` — voir [Outils hébergés](#claude-code-et-loutil-serveur-web_search) ; ignoré sinon |
  | autres outils serveur (`web_fetch_…`, `code_execution_…`, `bash`…) | ignorés |
  | blocs `server_tool_use` + `web_search_tool_result` rejoués (assistant) | un appel `web_search` et son message `tool`, si la requête déclare encore l'outil ; ignorés sinon |
  | `tool_choice` `auto` / `any` / `tool` / `none`, `disable_parallel_tool_use` | `auto` / `required` / `{function}` / `none`, `parallel_tool_calls: false` |
  | `stop_sequences`, `metadata.user_id`, `temperature`, `top_p`, `max_tokens` | `stop`, `user`, idem (plafond `max_tokens` du backend appliqué) |
  | `top_k`, `cache_control`, `thinking`, `output_config`, `context_management`, paramètres d'URL (`?beta=true`) | ignorés |
  | `finish_reason` `stop` / `length` / `tool_calls` | `stop_reason` `end_turn` / `max_tokens` / `tool_use` |
  | `reasoning_content` du backend | bloc `thinking` (`reasoning_as_thinking`) |
  | erreur OpenAI `{"error": {…}}` | `{"type": "error", "error": {type, message}}`, type déduit du statut |
- **Le modèle passe par `[anthropic.model_map]`**. Claude Code envoie
  des noms Claude en dur pour ses tâches d'arrière-plan (titres,
  résumés…), même avec `ANTHROPIC_MODEL` défini : la table les traduit en
  noms préfixés, routés comme d'habitude. Un suffixe `[1m]` est ignoré ;
  un nom déjà préfixé passe tel quel ; `default` attrape le reste ; sans
  correspondance → 400. L'exemple livre tous les noms connus sur
  `albert/deepseek-v4-flash`.
- `POST /v1/messages/count_tokens` : **exact** si le backend visé a un
  `tokenize_path` (llama.cpp : `/tokenize`), sinon estimation locale
  (~4 caractères par token, comme le limiteur). Claude Code s'en sert
  pour sa jauge de contexte et le moment de son `/compact`.
- Images : «multimodal au catalogue» = type `image-text-to-text`, que
  le proxy dérive d'`architecture.input_modalities` chez llama.cpp —
  présent seulement si le modèle est chargé avec son `--mmproj`. Le
  catalogue est chargé à la demande à la première image.
- `GET /v1/models` : le même catalogue, à la forme Anthropic, quand la
  requête porte `anthropic-version` (le SDK Anthropic le pose toujours,
  le SDK OpenAI jamais).
- L'auth du proxy accepte `x-api-key` en plus de `Authorization: Bearer`
  ; les en-têtes `x-api-key`, `anthropic-version`, `anthropic-beta` ne
  sont jamais relayés. Toutes les erreurs (401, 400, 429 du limiteur,
  503 backend éteint…) sortent à la forme Anthropic.
- Le limiteur, les stats (`endpoint = /v1/messages`), `force_tool_choice`
  et `max_tokens` s'appliquent comme pour tout autre client. En flux,
  derrière un backend à quotas, le `200` part **tout de suite** et des
  `event: ping` (toutes les `ping_interval` s) tiennent la connexion
  pendant l'attente du limiteur — Claude Code coupe un flux muet. Un 429
  local devient alors un `event: error` (`rate_limit_error`) dans le
  flux ; un client qui raccroche pendant l'attente quitte la file sans
  consommer de quota (499), comme ailleurs.

- **`WebSearch`** : l'outil de Claude Code ne cherche pas lui-même, il
  compte sur l'outil serveur `web_search` d'Anthropic. Sans recherche
  hébergée, cet outil est ignoré : le modèle répond sans chercher et
  `WebSearch` ne rend aucun lien. Avec `[tools.web_search].enabled`, le
  proxy fait la recherche — voir
  [Claude Code et l'outil serveur `web_search`](#claude-code-et-loutil-serveur-web_search).
  `WebFetch`, lui, lit les pages depuis le poste du client : le proxy
  n'y est pour rien.

À savoir : le prompt système de Claude Code pèse plusieurs milliers de
tokens, renvoyés à chaque tour sans cache exploitable côté OpenAI — le
quota journalier Albert se consomme vite ; `ANTHROPIC_SMALL_FAST_MODEL`
vers un backend local soulage (les tâches d'arrière-plan sont
nombreuses). Hors périmètre : Batches, Files, les outils serveur autres
que la recherche (`web_fetch`, `code_execution`…), PDF.

## Codex CLI

Avec `[responses].enabled = true` dans `config.toml`, Codex CLI (ou tout
client de l'API Responses d'OpenAI) se branche sur le proxy par un
provider, dans `~/.codex/config.toml` :

    model = "bigchuck/qwen3.8-flash-next"
    model_provider = "llm-proxy"

    [model_providers.llm-proxy]
    name = "llm-proxy"
    base_url = "http://localhost:8000/v1"
    wire_api = "responses"
    # env_key = "LLM_PROXY_KEY"   # si proxy.api_keys est renseigné

Le modèle porte le **préfixe du backend**, comme pour un client OpenAI :
pas de table de correspondance. `POST /v1/responses` est traduit en
`/v1/chat/completions` — tous les backends le parlent, aucun n'a besoin
de servir `/v1/responses` — et la réponse retraduite, en objet
`response` ou en flux d'événements `response.*`.

Ce que le proxy fait de la requête, écrit sur les corps que Codex envoie
réellement (la tolérance reprend celle de gufo, gufo-org/gufo#434) :

- **Outils hébergés : `web_search` et `image_generation` exécutés par le
  proxy s'ils sont activés, les autres ignorés**. `file_search`,
  `code_interpreter`, `mcp`… ne peuvent être exécutés que par OpenAI :
  ils sont retirés, les outils `function` restent, et une ligne de log
  dit lesquels (`responses : file_search sans équivalent chat, ignoré(s)`).
  `web_search` subit le même sort tant que `[tools.web_search]` et
  `[tools.web_fetch]` sont inactifs, `image_generation` tant que
  `[tools.image_generation]` l'est ; activés, le proxy présente au
  modèle ses propres fonctions à la place et les exécute — voir
  [Outils hébergés](#outils-hébergés).
- **`namespace` aplatis** : un `namespace` groupe des fonctions exécutées
  par le client (les `multi_agent_v1` de Codex). Ses fonctions rejoignent
  la liste, appelées par leur nom simple ; le `namespace` d'origine est
  reposé sur l'appel rendu au client. Un nom présent deux fois → `400`.
- **Champs sans effet tolérés** : `include`, `reasoning.summary`,
  `text.verbosity`, `prompt_cache_key`, `client_metadata`, `store`… Le
  corps upstream est reconstruit, rien d'inconnu ne part vers le backend.
- **Consignes d'ouverture en un seul `system`** : `instructions` et les
  messages `developer` qui ouvrent la conversation sont réunis dans le
  message system de tête (beaucoup de gabarits n'en acceptent qu'un). Le
  préfixe reste identique d'un tour à l'autre : sur une session Codex
  réelle vers gufo, 99 % du prompt est repris du cache dès le troisième
  tour (colonne *Cache* du tableau de bord).
- **`reasoning`** : les éléments rejoués sont jetés ; à l'inverse, le
  `reasoning_content` d'un backend devient un élément `reasoning`
  (résumé) visible dans le client (`reasoning_as_summary`).
  `reasoning.effort` part en `reasoning_effort`.
- **Aucun état de conversation** : `previous_response_id`,
  `conversation`, `background` et `item_reference` → `400` explicite.
  Codex renvoie tout l'historique à chaque tour. Seule exception, et
  seulement si les outils hébergés sont activés : la
  [mémoire des résultats](#mémoire-des-résultats) de leurs appels.

Les statistiques comptent ces requêtes sous `/v1/responses`, avec
l'`usage` exact du backend.

## Outils hébergés

Un client de l'API Responses déclare des outils qu'il ne sait pas
exécuter lui-même : avec `{"type": "web_search"}`, Codex CLI compte
qu'OpenAI fera la recherche côté serveur. Derrière ce proxy il n'y a pas
d'OpenAI — sans rien faire, l'outil est retiré et le modèle n'a pas de
recherche web. Un outil **hébergé** est un outil que le proxy exécute
lui-même, à la place d'OpenAI. Il y en a deux pour le web, activés
ensemble par le `web_search` du client, et un troisième,
[`image_generation`](#génération-dimage-image_generation), décrit à part. Un client de l'API Messages d'Anthropic est dans
le même cas avec son outil serveur `web_search_20250305` ; pour lui,
seule la recherche est branchée — voir
[Claude Code et l'outil serveur `web_search`](#claude-code-et-loutil-serveur-web_search).

| Fonction présentée au modèle | Ce qu'elle fait | Par quoi |
|---|---|---|
| `web_search` (`query`, `recency`, `limit`) | Une recherche ; rend une liste numérotée — titre, date, URL, extrait de 240 caractères | Une instance **SearXNG** auto-hébergée (métamoteur libre, API JSON, sans clé), `GET <searxng_url>/search?q=…&format=json` |
| `web_fetch` (`url`, `offset`) | Lit une page ; HTML converti en texte, JSON et texte tels quels, tout autre type refusé | Une requête HTTP du proxy, sous le garde-fou réseau |

Le schéma de `web_search` et la forme de sa sortie sont repris de l'outil
`web_search` d'[oh-my-pi](https://github.com/can1357/oh-my-pi).

État de la validation au 05/10/2026, sur un déploiement réel (image
SearXNG et `settings.yml` du dépôt), vers gufo 0.8.0
(`bigchuck/qwen3.8-flash-next`). Une même question de recherche a été
posée par les quatre clients, et chacun a répondu avec l'URL d'une source
trouvée par SearXNG :

| Client | Chemin | Ce qui a été joué |
|---|---|---|
| Codex CLI 0.157.1 | `/v1/responses`, boucle du proxy | recherche ; lecture de deux pages dans une même réponse ; adresse locale refusée par le garde-fou |
| Claude Code 2.1.287 | `/v1/messages`, outil serveur `web_search` | recherche par son outil `WebSearch` ; six recherches en erreur enchaînées (moteur éteint), blocs d'erreur lus par Claude Code |
| pi 0.87.1 | `/v1/tools`, extension `llm-proxy-web.ts` | recherche ; lecture de page ; commandes `/web` et `/page` |
| omp 18.3.2 | `/v1/tools`, extension `llm-proxy-web.ts` | recherche ; lecture de page ; commandes `/web` et `/page` |

Ce jour-là SearXNG rendait 20 résultats, deux de ses moteurs étant
refusés par leur source (Brave en limite de débit, DuckDuckGo en
CAPTCHA) : la qualité dépend des moteurs que l'adresse de la machine peut
encore joindre.

Un backend à quotas est passé dans la boucle ce jour-là
(`albert/deepseek-v4-flash-0731` : recherche puis réponse), sans attente
de quota provoquée.

Pas encore joué : une attente de quota à un tour ultérieur, un client qui
rejoue des blocs `web_search_tool_result` sur `/v1/messages` (Claude Code
ne le fait pas), le service `searxng` du `docker-compose.yml` du dépôt
tel quel (le déploiement d'essai l'intègre dans un compose local, sans la
clé obligatoire), et pi ou omp avec tous leurs outils (les essais
limitaient le modèle aux deux outils web).

### Déroulé

1. Le client déclare `web_search` dans `tools` (les variantes
   `web_search_preview` et `web_search_2025_08_26` comptent aussi). Rien
   n'est ajouté à une requête qui ne le déclare pas.
2. Le proxy présente au modèle, à la place, les fonctions `web_search` et
   `web_fetch` — celles qui sont activées. Une fonction du client qui
   porterait déjà l'un de ces noms garde le sien : l'outil hébergé
   homonyme n'est alors pas présenté.
3. Quand le modèle appelle l'une d'elles, l'appel n'est **pas** rendu au
   client en `function_call` : le proxy l'exécute, ajoute le résultat à
   la conversation et **relance le backend**, jusqu'à un tour sans appel
   hébergé. Un tour qui mêle appels hébergés et appels du client
   s'arrête après les premiers : la main revient au client.
4. Le client ne voit de tout cela que des éléments `web_search_call`
   terminés — action `search` (la requête) ou `open_page` (l'URL) —, puis
   la réponse.
5. Au tour suivant, le client renvoie ces éléments **sans leur résultat**
   (OpenAI le garde côté serveur). Le proxy le relit dans sa mémoire et
   l'élément redevient, à l'identique, un appel suivi de son résultat.

Une erreur d'outil n'est jamais une erreur HTTP : SearXNG injoignable,
page en 404, délai dépassé, adresse refusée — le modèle reçoit un texte
`Error: …` et s'adapte. Chaque exécution laisse une ligne de log
(`outil hébergé web_search(…) → N car. en 1.2s`).

Une réponse = **une** ligne de statistiques, quel que soit le nombre de
tours upstream, avec l'usage cumulé ; chaque tour repasse par le limiteur
d'un backend à quotas. Les outils actifs sont dits au démarrage dans les
logs et dans `/healthz` (`tools` : fonctions actives, `max_calls`,
nombre d'entrées en mémoire).

### Claude Code et l'outil serveur `web_search`

L'outil `WebSearch` de Claude Code ne cherche pas lui-même. À chaque
appel il envoie une **sous-requête** `/v1/messages` à part — un court
`system`, un message « Perform a web search for the query: … », et pour
seul outil `{"type": "web_search_20250305", "name": "web_search",
"max_uses": 8}` — en comptant qu'Anthropic exécutera la recherche. Il lit
ensuite les blocs de la réponse. Avec `[tools.web_search].enabled`, le
proxy tient ce rôle :

1. L'outil serveur `web_search_…` (toute version datée) devient, pour le
   modèle, la fonction `web_search` du tableau ci-dessus. **`web_fetch`
   n'est pas présenté** sur cette surface : la lecture de page de Claude
   Code (`WebFetch`) se fait sur le poste du client. Une fonction du
   client nommée `web_search` garde son nom, l'outil hébergé n'est alors
   pas présenté. Les autres outils serveur restent ignorés.
2. Quand le modèle l'appelle, le proxy exécute la recherche et relance
   le backend — la même boucle que pour Codex. Le modèle reçoit le même
   texte de résultats que sur la surface Responses.
3. Le client reçoit, en flux SSE comme en JSON, ce qu'Anthropic rend :

       content_block_start  {"index": 1, "content_block": {"type": "server_tool_use",
                             "id": "srvtoolu_…", "name": "web_search", "input": {}}}
       content_block_delta  {"index": 1, "delta": {"type": "input_json_delta",
                             "partial_json": "{\"query\": \"…\"}"}}
       content_block_stop   {"index": 1}
       ping …                                    (tant que la recherche dure)
       content_block_start  {"index": 2, "content_block": {"type": "web_search_tool_result",
                             "tool_use_id": "srvtoolu_…", "content": [
                               {"type": "web_search_result", "title": "…", "url": "…",
                                "encrypted_content": "<l'extrait>", "page_age": "2026-10-03"}]}}
       content_block_stop   {"index": 2}
       … le texte de la réponse …
       message_delta        {"delta": {"stop_reason": "end_turn"}, "usage": {…,
                             "server_tool_use": {"web_search_requests": 1}}}

   Chaque résultat suit son appel, même quand le modèle lance deux
   recherches d'un coup. L'`input` reprend les arguments du modèle
   (`query`, et `recency` / `limit` s'il les a donnés). Aucun résultat :
   `content` est une liste vide. Une recherche en échec n'est pas une
   erreur HTTP : `content` est l'objet `{"type":
   "web_search_tool_result_error", "error_code": …}` — `max_uses_exceeded`,
   `invalid_tool_input` (pas de `query`), `unavailable` pour tout le
   reste — et le modèle, lui, lit le texte `Error: …` complet.

Ce qui vient de l'outil du client :

- **`max_uses`** est respecté, sans jamais dépasser `[tools].max_calls`.
- **`allowed_domains` / `blocked_domains`** filtrent les résultats avant
  la limite, aux règles d'Anthropic : domaine nu, sous-domaines couverts
  (`example.com` couvre `docs.example.com`), chemin optionnel
  (`example.com/blog`). C'est un filtre sur ce que SearXNG a rendu, la
  requête n'est pas réécrite : une liste étroite peut ne rien laisser.
  Les jokers de chemin ne sont pas lus (le chemin s'arrête au premier
  `*`), et les deux listes ensemble s'appliquent toutes les deux, là où
  Anthropic répond 400. `user_location` est ignoré.

Ce qui diffère d'Anthropic, à savoir :

- **`encrypted_content` porte l'extrait, en clair.** Chez Anthropic
  c'est un blob chiffré que le client doit renvoyer pour que l'API
  retrouve le résultat au tour suivant ; ici rien n'est à cacher, et le
  rôle est le même : avec l'extrait, le bloc porte tout ce que le modèle
  a lu. **`page_age`** est la date du résultat (`AAAA-MM-JJ`) ou `null`.
- **Aucune mémoire côté proxy pour cette surface.** Un client qui
  rejoue les blocs `server_tool_use` + `web_search_tool_result` dans ses
  `messages` les voit redevenir un appel et son message `tool`, dont le
  texte est reconstruit depuis le bloc — le même, à l'octet près, que
  celui que le modèle avait lu, donc le même préfixe pour le cache du
  backend. Seule exception : d'un résultat en erreur il ne reste que le
  code. (Dans les captures, Claude Code ne rejoue pas ces blocs : sa
  sous-requête n'a qu'un tour.) Si la requête ne déclare plus l'outil,
  les blocs rejoués sont ignorés, comme avant.
- **Pas de citations** : les blocs `text` ne portent pas de `citations`.
- **Recherche et outil du client dans le même tour** : la recherche est
  exécutée, puis la main revient au client (`stop_reason: "tool_use"`),
  le bloc de l'outil client précédant alors ceux de la recherche.
  Anthropic, lui, rend la main sans exécuter la recherche.
- **Jamais de `pause_turn`** : un modèle qui cherche sans conclure est
  arrêté par le garde-fou du nombre d'appels, en `end_turn`.
- En flux, des `event: ping` partent pendant l'exécution d'une recherche
  et pendant l'attente d'un quota aux tours suivants (`ping_interval`).
  En JSON rien ne part avant la fin : un backend qui tombe en cours de
  boucle donne son vrai statut d'erreur, pas un `200`.

### Mise en route

Le `docker-compose.yml` porte un service `searxng` à côté du proxy,
**sans port publié** : l'instance n'a pas de limiteur, elle n'est
joignable que du proxy, par le réseau du compose
(`http://searxng:8080`).

    # 1. la clé secrète de SearXNG, dans .env (jamais dans un fichier versionné)
    echo "SEARXNG_SECRET=$(openssl rand -hex 32)" >> .env

    # 2. dans data/config.toml (un config.toml qui existe déjà n'est pas
    #    réécrit : y recopier les tables depuis data/config.example.toml)
    [responses]
    enabled = true
    [tools.web_search]
    enabled = true
    searxng_url = "http://searxng:8080"
    [tools.web_fetch]
    enabled = true

    # 3.
    docker compose up -d --build

Ce qu'il faut savoir du service :

- **`SEARXNG_SECRET` est à poser dans `.env`, sans être bloquante.**
  SearXNG refuse de démarrer avec sa clé par défaut mais accepte une clé
  vide : absente, `docker compose` avertit et tout démarre quand même.
  C'est voulu — une variable exigée arrêterait `docker compose` en
  entier, et dans une pile à `include` tous les autres services avec.
  L'instance n'étant joignable que du proxy, la clé n'y signe rien
  d'exposé.
- **L'image est épinglée** sur un tag daté (`AAAA.M.J-<commit>`), pas
  sur `latest`. Mise à jour : changer le tag dans `docker-compose.yml`,
  puis `docker compose up -d searxng`.
- **`searxng/settings.yml`** part des défauts de SearXNG
  (`use_default_settings: true`) et n'y ajoute que le format `json` —
  sans lui, `format=json` est refusé en 403. Moteurs, langue et délais
  sont ceux de SearXNG ; les changer se fait dans ce fichier, puis
  `docker compose restart searxng`.
- **Le proxy ne dépend pas de SearXNG pour démarrer** (pas de
  `depends_on`) : instance arrêtée ou en panne, la recherche rend une
  erreur au modèle, tout le reste sert.
- **Hors compose** (proxy lancé par `uvicorn`, Coolify sans ce compose) :
  `searxng_url` doit pointer une instance que le proxy peut joindre, avec
  `json` dans ses `search.formats`.

Diagnostic, l'instance n'étant pas joignable de l'hôte :

    docker compose ps searxng          # « healthy » : /healthz répond
    docker compose logs searxng
    docker compose exec albert-proxy python -c "import urllib.request; \
      print(urllib.request.urlopen('http://searxng:8080/search?q=test&format=json').read()[:300])"

### Garde-fous

- **Adresses publiques seulement** (`web_fetch`). La cible est choisie
  par le modèle et la requête part du proxy : sans filtre, une page web
  ou un prompt pourrait lui faire lire un service de son réseau — le
  backend d'inférence, l'instance SearXNG, un routeur, les métadonnées
  d'un cloud. Le nom est résolu par le proxy ; **toutes** ses adresses
  doivent être publiques (ni privées, ni locales, ni lien local, ni CGNAT
  `100.64.0.0/10` — les adresses Tailscale —, ni réservées) ; la
  connexion part vers l'adresse vérifiée, pas vers le nom, qu'un DNS
  pourrait faire changer entre le contrôle et la connexion. Le contrôle
  est refait **à chaque saut de redirection** (5 au plus), les
  redirections n'étant jamais suivies par la bibliothèque HTTP. Seuls
  `http` et `https` sont lus. `allow_private = true` lève le filtre.
  L'adresse de SearXNG, elle, est une adresse de configuration : le
  filtre ne s'y applique pas.
- **Tailles** : `max_bytes` octets lus par page, le reste n'est pas
  téléchargé ; `max_chars` caractères rendus par appel (le modèle
  redemande la suite par `offset`) ; `max_result_chars` par résultat,
  quel que soit l'outil ; `limit` résultats par recherche, 20 au plus.
- **Délais** : `timeout` par requête, pour chaque outil ; `run_timeout`
  pour une exécution entière, redirections comprises.
- **Nombre d'appels** : `max_calls` exécutions par réponse. Au-delà, le
  modèle reçoit une erreur qui lui demande de conclure avec ce qu'il a ;
  s'il insiste encore (4 appels de plus), la réponse est close sans lui.

### Génération d'image (`image_generation`)

Avec `[tools.image_generation].enabled`, l'outil
`{"type": "image_generation"}` d'un client **Responses** n'est plus
ignoré. Joué une fois en réel le 05/10/2026, par une requête directe (pas un
client de l'API Responses) vers gufo 0.8.0 : Flash-Next appelle la
fonction, Qwen-Image rend un PNG 512x512 en 25 s, le modèle de
conversation est rechargé au tour suivant, 65 s en tout, la connexion
tenue par cinq `: ping`. **Pas encore joué avec un vrai client**, ni le
rejeu de l'élément au tour suivant. La forme de l'élément et des
événements a été relue ce jour-là dans le guide d'OpenAI et les types du
SDK `openai-python`.

1. Le proxy présente au modèle la fonction `image_generation` (`prompt`,
   et `size` parmi les tailles permises).
2. Quand le modèle l'appelle, le proxy envoie
   `POST /v1/images/generations` au backend que désigne le préfixe de
   `[tools.image_generation].model` — par le client HTTP et la clé de ce
   backend, préfixe retiré — avec `model`, `prompt`, `size` (et `steps`
   s'il est réglé) : le corps que gufo accepte. Réponse lue :
   `data[0].b64_json`. Une image par appel.
3. Le client reçoit, en flux :

       response.output_item.added                  {"item": {"id": "ig_…", "type": "image_generation_call", "status": "in_progress"}}
       response.image_generation_call.in_progress  {"output_index": 0, "item_id": "ig_…"}
       response.image_generation_call.generating   {"output_index": 0, "item_id": "ig_…"}
       : ping                                      (commentaire SSE, tant que la génération dure)
       response.image_generation_call.completed    {"output_index": 0, "item_id": "ig_…"}
       response.output_item.done                   {"item": {"id": "ig_…", "type": "image_generation_call",
                                                    "status": "completed", "result": "<base64>",
                                                    "revised_prompt": "<le prompt du modèle>",
                                                    "size": "512x512", "output_format": "png"}}

   En JSON, le même élément dans `output`. `output_format` est le format
   réel, lu dans les premiers octets de l'image. `revised_prompt` porte
   le prompt tel que le modèle l'a écrit (rien ne le réécrit).
4. Le **modèle** ne reçoit pas l'image mais une phrase :
   `Image generated (512x512, png) and shown to the user. …`, et le
   backend de conversation est relancé pour qu'il conclue.
5. **Échec** (backend en erreur, délai, limite atteinte) : le modèle lit
   un texte `Error: …` ; l'élément est rendu `"status": "failed"`,
   `"result": null`, sans événement `.completed`. La réponse, elle,
   aboutit.
6. **Rejeu.** Le client renvoie l'élément au tour suivant — avec son
   image, ou par son seul `id`. Il redevient l'appel et le texte que le
   modèle avait lu, relus dans la [mémoire des résultats](#mémoire-des-résultats),
   qui ne garde que ce texte : **l'image ne retourne jamais au modèle**
   et n'est conservée nulle part par le proxy. Mémoire perdue : l'appel
   est reconstruit du `revised_prompt` de l'élément, avec le même texte
   si l'élément porte encore `size` et `output_format`, un texte sans
   taille sinon.

Ce qui vient de l'outil du client : **`size`**, si elle figure dans
`sizes` (elle l'emporte alors sur le choix du modèle ; `auto` ou hors
liste = choix du modèle, puis défaut). **Ignorés**, faute d'équivalent :
`model` (c'est celui de la configuration qui dessine), `quality`,
`output_format`, `output_compression`, `background`, `moderation`,
`input_fidelity`, `input_image_mask`, `action` (pas d'édition d'image),
`partial_images` (aucun événement `partial_image`), et un `tool_choice`
qui forcerait l'outil (laissé à `auto`).

Bornes : `sizes`, `max_per_response` images par réponse, `timeout`
propre (à la place de `run_timeout`). **Sur un backend qui partage sa
mémoire entre modèle d'image et LLM (gufo), générer décharge le modèle
de la conversation** : à la durée de la génération s'ajoutent la bascule
(~30 s) et le rechargement au tour suivant de la boucle (~40 s), pendant
lesquels le flux ne porte que des `: ping`. En JSON rien ne part avant
la fin : le client doit patienter d'autant.

Cet outil n'est **pas** exposé sur `/v1/tools` (ces routes rendent un
texte ; pi et omp ont `gufo-media.ts`), ni présenté sur `/v1/messages`.
La génération ne compte pas dans les statistiques (elles comptent des
tokens) ni dans le limiteur d'un backend à quotas : elle laisse une
ligne de log (`image_generation : 512x512 png … en 16.2s, 412 Ko`), et
sa durée est comprise dans celle de la réponse.

    [tools.image_generation]
    enabled = true
    model = "bigchuck/Qwen-Image-2.1-heretic"

**Qui s'en sert.** Pas Codex CLI derrière ce proxy : dans son code
(0.157.1 comme la branche principale au 05/10/2026,
`codex-rs/core/src/tools/spec_plan.rs`), la génération d'image est une
fonction côté client (`image_gen.imagegen`) qui appelle le service
d'images d'OpenAI, et n'est proposée qu'à un compte connecté à OpenAI
(hors offre gratuite) avec un modèle à entrée image — jamais à un
provider tiers ; l'outil hébergé `{"type": "image_generation"}` n'y est
pas déclaré. Cet outil sert donc un client écrit contre l'API Responses
qui le déclare lui-même (SDK `openai`, Agents SDK, script `curl`).

### Appel direct : `/v1/tools` (pi, omp)

Un client qui parle `/v1/chat/completions` n'a pas d'outil « hébergé » à
déclarer, et garde ses appels d'outils dans son propre historique. Pour
lui, les mêmes outils s'appellent directement, derrière la clé du proxy
et avec les mêmes garde-fous :

    GET  /v1/tools            → les outils actifs dont le résultat est un
                                texte (nom, description, schéma) : pas
                                `image_generation`
    POST /v1/tools/<nom>      → corps : les arguments, en objet JSON
                                réponse : {"name", "result", "is_error"}

Le client déclare alors l'outil à son modèle comme n'importe quel outil
à lui, et exécute l'appel par cette route : le proxy n'a ni boucle ni
mémoire à tenir. Un échec de l'outil est un `200` avec un texte
`Error: …` et `is_error: true` (c'est un texte pour le modèle) ; un outil
inconnu ou désactivé, un `404` `unknown_tool`.

C'est ce que fait l'extension pi / omp `tools/llm-proxy-web.ts` du dépôt
[llmsetup](https://github.com/c4software/llmsetup) : elle lit
`GET /v1/tools` au démarrage et enregistre `proxy_web_search` et
`proxy_web_fetch`, plus les commandes `/web` et `/page`.

### Mémoire des résultats

C'est **la seule chose que le proxy conserve entre deux requêtes**, et
seulement pour la surface Responses (un client Anthropic renvoie le
résultat avec l'appel : rien n'est gardé pour lui). Elle
existe parce que le client rejoue l'élément `web_search_call` sans son
résultat : sans elle le modèle perdrait, au tour suivant, tout ce qu'il a
lu — et le début de la conversation changerait, ce qui fait manquer le
cache de préfixe du backend.

Elle garde, par identifiant d'élément : le nom de la fonction, ses
arguments tels que le modèle les a écrits, le texte du résultat (pour
une image générée : la phrase rendue au modèle, jamais l'image). Rien
d'autre, et rien qui identifie le client. Elle vit **en mémoire vive**,
jamais sur disque, bornée en nombre (`cache_entries`, les entrées les
moins récemment relues sortent) et en durée (`cache_ttl`). **Un
redémarrage du proxy la vide** : l'appel rejoué est alors reconstruit
depuis l'action de l'élément (la requête, l'URL), avec pour résultat un
mot qui dit au modèle que le contenu n'est plus disponible et qu'il peut
relancer l'outil.

### Ce qu'aucun garde-fou n'empêche

**Le contenu d'une page web entre dans le contexte d'un agent qui
exécute des commandes sur le poste du client.** Une page — ou un extrait
de résultat de recherche — peut porter des instructions écrites pour le
modèle : c'est l'injection de prompt. Le modèle qui les suit lance un
`shell`, modifie un fichier, envoie ailleurs ce qu'il a lu, avec les
droits que le client lui a donnés.

Une variante ne passe pas par le poste du client : la page demande au
modèle d'ouvrir `https://ailleurs/?d=<ce qu'il a en contexte>`, et
`web_fetch` l'emporte. La seule parade côté proxy est de restreindre ce
qu'il peut ouvrir : `allowed_domains` (liste blanche) ou
`blocked_domains` dans `[tools.web_fetch]`. Vides par défaut.

Les garde-fous ci-dessus protègent **le réseau du proxy** et bornent des
tailles. Aucun ne lit, ne filtre ni ne neutralise ce qu'une page dit, et
aucun ne le peut. Activer `web_search`, c'est faire lire du texte non
fiable à l'agent : la seule défense est du côté du client — ses
approbations de commandes, son bac à sable — et dans le choix de ne pas
l'activer là où l'agent travaille sans surveillance.

## Déploiement

### Docker Compose

    cp .env.example .env        # y mettre ALBERT_API_KEY et SEARXNG_SECRET
    docker compose up -d --build

`SEARXNG_SECRET` (`openssl rand -hex 32`) est la clé du service
`searxng`, le métamoteur des [outils hébergés](#outils-hébergés) : sans
elle `docker compose` avertit et démarre quand même.

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
`data/config.toml`, dans le volume. Le Dockerfile seul ne livre pas
SearXNG : pour la recherche web, déployer une instance à part et y
pointer `tools.web_search.searxng_url`.

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

| Champ | Défaut | Rôle |
|---|---|---|
| `url` | *(requis)* | Base du backend |
| `api_key` | *(aucune)* | **La clé du backend vit ici**, et nulle part ailleurs — clé Albert, ou `--api-key` de llama-server. Si définie, elle remplace l'`Authorization` du client |
| `quotas` | `false` | Active le limiteur Albert (fenêtres minute et jour, `/v1/me/info`) |
| `timeout` | `proxy.upstream_timeout` | Secondes, pour la génération |
| `meta_timeout` | `proxy.meta_timeout` | Secondes, pour `/v1/models`, `/v1/me/info`, `tokenize_path` |
| `connect_timeout` | `1` sans quota, `15` avec | Poignée de main TCP seule : un backend local éteint échoue en 1 s, un hôte vivant répond bien avant, même occupé |
| `verify_ssl` | `true` | `false` pour un certificat auto-signé |
| `force_tool_choice` | *(aucune injection)* | `true` → `proxy.tool_choice` ; `"auto"`, `"required"`… → cette valeur. Seule exception : `[backends]` absent du TOML → Albert par défaut avec `"auto"`, c'est lui que le correctif vise |
| `max_tokens` | `0` = aucun | Plafond : `max_tokens` / `max_completion_tokens` du client ramené au plafond, jamais augmenté ni ajouté |
| `images` | `false` | Les modèles multimodaux au catalogue du backend reçoivent les `image_url` d'un client Anthropic ; sinon texte de remplacement |
| `tokenize_path` | *(aucun)* | Endpoint de tokenisation pour un `count_tokens` exact — llama.cpp : `"/tokenize"` |

**Tout modèle doit être préfixé** : préfixe inconnu → 400
`unknown_backend_prefix`. Seules les requêtes sans champ `model`
(endpoints de compte, corps non JSON) partent vers le backend à quotas.

### `[proxy]`

| Clé | Défaut | Rôle |
|---|---|---|
| `api_keys` | `[]` | Clé(s) exigée(s) **des clients** pour appeler le proxy (`Authorization: Bearer <clé>` à la OpenAI, ou `x-api-key: <clé>` à l'Anthropic). Liste vide = proxy ouvert ; 401 sinon, `/healthz` exempté ; `/ui` accepte aussi `?key=<clé>` (puis cookie) |
| `upstream_timeout` | `600` | Secondes ; large pour les longues générations |
| `meta_timeout` | `5` | Secondes pour `/v1/models`, `/v1/me/info` — court, un backend lent ne doit pas bloquer le catalogue |
| `tool_choice` | `"auto"` | Valeur injectée quand `tools` est présent sans `tool_choice`, pour les backends ayant `force_tool_choice = true`. L'injection est désactivée par défaut : elle s'active par backend |
| `forward_post_paths` | `["/v1/completions", "/v1/embeddings", "/v1/rerank", "/v1/audio/transcriptions", "/v1/audio/speech", "/v1/images/generations", "/v1/images/edits", "/v1/ocr"]` | Routes POST relayées en plus des handlers dédiés ; le reste → 404 |
| `exempt_paths` | `["/embeddings", "/rerank", "/audio/transcriptions", "/ocr"]` | Suffixes de routes exclus du limiteur |
| `log_level` | `"INFO"` | `INFO` logue chaque injection et chaque mise en attente |

### `[stats]`

| Clé | Défaut | Rôle |
|---|---|---|
| `database` |  `"stats.db"` | Base SQLite ; chemin relatif = à côté de `config.toml` |
| `retention_days` | `90` | Purge des lignes plus anciennes (`0` = conservation illimitée) |

### `[anthropic]`

| Clé | Défaut | Rôle |
|---|---|---|
| `enabled` | `false` | Ouvre la surface Anthropic (`/v1/messages`…). Table absente = inactive, dit au démarrage dans les logs et dans `/healthz` |
| `model_map` | `{}` | Noms de modèles Anthropic → noms préfixés ; `default` attrape les inconnus. Voir [Claude Code](#claude-code) |
| `ping_interval` | `10` | Secondes entre deux `event: ping` pendant l'attente du limiteur, en flux. `0` = attendre avant de répondre |
| `reasoning_as_thinking` | `true` | `reasoning_content` du backend → bloc `thinking` pour le client |
| `trace` | `false` | Une ligne de log par réponse `/v1/messages` : `stop_reason`, outils appelés (nom + extrait des arguments), tokens. Pour voir ce qu'un agent fait — ou répète — derrière le proxy |

### `[responses]`

| Clé | Défaut | Rôle |
|---|---|---|
| `enabled` | `false` | Ouvre la surface Responses (`POST /v1/responses`). Table absente = inactive, dit au démarrage dans les logs et dans `/healthz` |
| `reasoning_as_summary` | `true` | `reasoning_content` du backend → élément `reasoning` (résumé) pour le client |

### `[tools]`

Les [outils hébergés](#outils-hébergés) : ce qui est commun aux deux.

| Clé | Défaut | Rôle |
|---|---|---|
| `max_calls` | `8` | Appels d'outils hébergés exécutés pour **une** réponse ; au-delà, le modèle reçoit une erreur qui lui demande de conclure. Le `max_uses` d'un outil serveur Anthropic peut l'abaisser pour sa requête, jamais le relever |
| `run_timeout` | `60` | Secondes pour une exécution, tout compris (redirections suivies incluses). Dépassé → erreur rendue au modèle. `image_generation` a son propre délai |
| `max_result_chars` | `24000` | Caractères d'un résultat rendu au modèle ; le surplus est coupé et marqué `[truncated]` |
| `cache_entries` | `512` | Appels gardés par la [mémoire des résultats](#mémoire-des-résultats) ; les moins récemment relus sortent |
| `cache_ttl` | `86400` | Secondes de vie d'une entrée de cette mémoire |

### `[tools.web_search]`

| Clé | Défaut | Rôle |
|---|---|---|
| `enabled` | `false` | Présente `web_search` au modèle quand le client déclare `web_search` (Responses) ou l'outil serveur `web_search_…` (Anthropic, Claude Code). Table absente = inactif : l'outil du client est ignoré |
| `searxng_url` | `""` | Base de l'instance SearXNG ; le proxy appelle `<searxng_url>/search?q=…&format=json`. Dans le compose : `"http://searxng:8080"`. Vide → le modèle reçoit « recherche non configurée ». Adresse de configuration : le garde-fou des adresses publiques ne s'y applique pas |
| `timeout` | `20` | Secondes pour la requête vers SearXNG |
| `limit` | `8` | Résultats rendus quand le modèle ne précise pas `limit` (20 au plus) |
| `language` | `""` | Paramètre `language` passé à SearXNG (`"fr"`, `"en-US"`…) ; vide = non envoyé, défaut de l'instance |
| `categories` | `""` | Paramètre `categories` passé à SearXNG (`"general"`, `"general,it"`…) ; vide = non envoyé |

### `[tools.web_fetch]`

| Clé | Défaut | Rôle |
|---|---|---|
| `enabled` | `false` | Présente `web_fetch` au modèle quand un client **Responses** déclare `web_search` — sans elle, la recherche ne rend que des extraits de 240 caractères. Jamais présenté à un client Anthropic |
| `timeout` | `20` | Secondes par requête (une par saut de redirection, 5 sauts au plus) |
| `max_bytes` | `2000000` | Octets lus au plus sur le corps d'une page |
| `max_chars` | `20000` | Caractères de texte rendus par appel ; la suite se demande par `offset` |
| `allow_private` | `false` | `false` : seules les adresses **publiques** sont jointes, contrôle refait à chaque redirection. `true` lève le filtre — à n'ouvrir que sur un proxy dont tous les clients sont de confiance, et jamais derrière un modèle qui lit le web |
| `allowed_domains` | `[]` | Non vide : **seuls** ces domaines sont lus par `web_fetch` (sous-domaines couverts, chemin facultatif : `example.com/blog`). Contrôlé à chaque redirection |
| `blocked_domains` | `[]` | Domaines jamais lus, mêmes règles |

### `[tools.image_generation]`

| Clé | Défaut | Rôle |
|---|---|---|
| `enabled` | `false` | Présente `image_generation` au modèle quand un client **Responses** déclare `image_generation`. Table absente = inactif : l'outil du client est ignoré. Jamais présenté à un client Anthropic, pas d'appel direct par `/v1/tools` |
| `model` | `""` | Modèle d'image, **préfixé** par son backend (`"bigchuck/Qwen-Image-2.1-heretic"`) ; appelé par `/v1/images/generations` de ce backend, avec sa clé. Vide ou préfixe inconnu → le modèle reçoit « non configuré », et un avertissement au démarrage |
| `size` | `"512x512"` | Taille quand ni le client ni le modèle n'en demandent une permise |
| `sizes` | `["512x512", "768x768", "1024x1024"]` | Tailles permises : celles que le modèle peut choisir, et parmi lesquelles le `size` de l'outil du client est respecté |
| `steps` | `0` | Étapes de diffusion envoyées au backend (`steps`, extension de gufo) ; `0` = non envoyé |
| `timeout` | `300` | Secondes pour la requête au backend, bascule de modèle comprise ; remplace `run_timeout` pour cet outil |
| `max_per_response` | `2` | Images générées pour **une** réponse ; au-delà, erreur rendue au modèle (en plus de `max_calls`) |

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
  (`Authorization: Bearer <clé>`) ou à l'Anthropic (`x-api-key`) — sans
  elle, 401. Quand l'auth est active, ni le Bearer du client ni ses
  cookies ne sont relayés aux backends (c'est la clé du proxy, pas de
  l'upstream) ; `x-api-key` ne l'est jamais. Le tableau de bord `/ui` est
  soumis à la même clé : un navigateur ne pouvant pas poser d'en-tête,
  elle s'y passe une fois en `?key=<clé>` puis est mémorisée dans un
  cookie `HttpOnly` (`SameSite=Strict`).
- **Le réseau** : Nginx Proxy Manager, Tailscale, réseau Docker partagé.

Avec les [outils hébergés](#outils-hébergés) activés, s'y ajoute que le
proxy **émet des requêtes vers des adresses choisies par le modèle** —
bornées aux adresses publiques — et fait entrer du texte du web dans le
contexte d'un agent : lire
[Ce qu'aucun garde-fou n'empêche](#ce-quaucun-garde-fou-nempêche) avant
de les ouvrir.

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

    # surface Anthropic (si [anthropic].enabled) : un Message, puis un flux
    curl -s http://localhost:8000/v1/messages \
      -H "Content-Type: application/json" -H "anthropic-version: 2023-06-01" \
      -d '{"model":"claude-opus-5","max_tokens":64,
           "messages":[{"role":"user","content":"Bonjour"}]}' \
      | jq '{model, stop_reason, text: .content[0].text}'

    # surface Responses (si [responses].enabled)
    curl -s http://localhost:8000/v1/responses \
      -H "Content-Type: application/json" \
      -d '{"model":"bigchuck/qwen3.8-flash-next","input":"Bonjour",
           "max_output_tokens":64}' \
      | jq '{model, status, text: .output[0].content[0].text}'

    # tests des traducteurs
    python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
    .venv/bin/python -m pytest -q tests

    # validation avec de vrais clients (Claude Code, pi, Codex), en conteneurs
    cd envTest && cp .env.example .env && docker compose run --rm claude && docker compose run --rm pi && docker compose run --rm codex

## Limites connues

- **Pas de cache côté proxy** : `cache_control` est ignoré, rien
  d'équivalent côté OpenAI, et le cache KV appartient au serveur
  d'inférence. Un backend à cache de préfixe (vLLM, llama.cpp) en
  profite implicitement quand le début de la conversation ne change pas
  d'un tour à l'autre — le proxy y veille (sérialisation stable,
  `system` en tête, injections toujours au même endroit) et le **mesure**
  (`input_cached_tokens`, colonne *Cache*). Albert, lui, facture le
  prompt système de Claude Code (~20 k tokens) à chaque tour. Un client
  Anthropic reçoit `cache_read_input_tokens` et un `input_tokens` qui
  l'exclut, comme chez Anthropic.
- **PDF** (`document` base64) : remplacé par un texte. Un backend qui
  accepterait la partie `file` d'OpenAI pourrait le recevoir — le jour
  où il y en a un.
- **Images** : seul le catalogue du backend décide ; un modèle vision
  servi sans `--mmproj` est un modèle texte.
- **Hors périmètre, volontairement** : Batches, Files, les outils
  serveur Anthropic autres que la recherche (`web_fetch`,
  `code_execution`…), et le sens proxy → backend Anthropic.
- **Surface Responses** : des outils hébergés, seuls `web_search` et
  `image_generation` sont exécutés par le proxy, et seulement s'ils sont
  activés ([Outils hébergés](#outils-hébergés)) ; les autres (`file_search`,
  `code_interpreter`, `mcp`…) sont toujours ignorés,
  pas exécutés (le modèle ne les voit pas), comme les outils intégrés au
  client sans équivalent chat (`custom`, `local_shell`…). Pas de `ping`
  pendant l'attente du quota du PREMIER tour : un flux vers un backend à
  quotas attend avant de répondre, comme pour un client OpenAI. Pendant
  une recherche et aux tours suivants de la boucle d'outils, un
  commentaire SSE (`: ping`) tient la connexion.
- **Outils hébergés : surfaces Responses et Anthropic.**
  `/v1/chat/completions` n'en profite pas (un client qui veut les
  déclarer lui-même a `/v1/tools`). Sur `/v1/messages`, seule la
  recherche est branchée ;
  pas de citations, pas de `pause_turn`, `user_location` ignoré, et les
  listes de domaines ne font que filtrer ce que SearXNG a rendu — voir
  [Claude Code et l'outil serveur `web_search`](#claude-code-et-loutil-serveur-web_search).
  Pas de rendu de JavaScript dans `web_fetch` (une page construite côté
  navigateur rend peu de texte), pas de PDF. La mémoire des résultats
  (surface Responses) ne survit pas à un redémarrage. État de la
  validation : voir [Outils hébergés](#outils-hébergés).
- **`image_generation` hébergé : pas de vrai client pour l'instant.**
  Génération seule (pas d'édition, pas d'image partielle), une image par
  appel, options de l'outil du client ignorées sauf `size`. L'image part
  deux fois en flux (`output_item.done`, puis `response.completed`),
  comme chez OpenAI. Un client qui raccroche annule la requête du proxy,
  pas forcément la génération déjà lancée chez le backend. Codex CLI ne
  déclare pas cet outil à un provider tiers (voir
  [Génération d'image](#génération-dimage-image_generation)).

## Côté clients

- Claude Code : `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY`,
  `ANTHROPIC_MODEL=<backend>/<modèle>` — voir [Claude Code](#claude-code).
- Codex CLI : un provider `wire_api = "responses"` dans
  `~/.codex/config.toml`, modèle `<backend>/<modèle>` — voir
  [Codex CLI](#codex-cli).
- Hermes : retirer `extra_body.tool_choice` du provider dans
  `~/.hermes/config.yaml`, pointer `api` sur le proxy.
- pi et omp, outils web : l'extension `tools/llm-proxy-web.ts` de
  llmsetup, qui appelle `/v1/tools` — voir
  [Appel direct](#appel-direct--v1tools-pi-omp).
- pi : un provider dans `~/.pi/agent/models.json` — `api:
  "openai-completions"` sur `http://…:8000/v1`, ou `api:
  "anthropic-messages"` sur `http://…:8000` (les deux marchent ; voir
  `envTest/pi/models.json.tpl`). `apiKey` reste obligatoire — `"unused"`
  suffit si le proxy est ouvert. L'ancienne extension
  `patchFetchForAlbert()` n'a plus lieu d'être.
