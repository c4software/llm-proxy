# Statistiques (Usage API)

Chaque requête servie est enregistrée dans `data/stats.db` (SQLite). Les
compteurs persistent après redémarrage et peuvent être agrégés sur toute
période. L'Usage API d'OpenAI expose ces données ; le SDK officiel et le
[tableau de bord](tableau-de-bord.md) les utilisent. Aucune activation n'est
requise.

| Réglage | Défaut | Effet |
|---|---|---|
| `stats.retention_days` | `90` | purge des lignes plus anciennes (`0` = illimité) |

## Lire l'usage des requêtes

```http
GET /v1/organization/usage/completions
    ?start_time=<epoch>        (obligatoire)
    &end_time=<epoch>
    &bucket_width=1m|1h|1d|all
    &group_by[]=model
    &models[]=albert/openweight-large
    &limit=<n>&page=<curseur>
```

La réponse suit le schéma OpenAI exact : `page` → `bucket` → `result`.

```python
from openai import OpenAI
c = OpenAI(base_url="http://localhost:8000/v1", api_key="x", admin_api_key="x")
page = c.admin.organization.usage.completions(
    start_time=..., bucket_width="1d", group_by=["model"], limit=7)
```

Toute clé valide lit l'usage de **toutes** les clés par cette route.
Seul le tableau de bord se restreint à la clé connectée.

### Ajouts du proxy

Ils sont additifs : un SDK ignore ce qu'il ne connaît pas.

- `bucket_width=all` : un seul seau sur toute la plage, pour un total.
- `input_cached_tokens` est renseigné quand le backend dit ce qu'il a
  servi depuis son cache de préfixe (llama.cpp, vLLM, OpenAI), `0`
  sinon. Il est inclus dans `input_tokens`, comme chez OpenAI.
- Champs en plus dans chaque `result` : `num_errors`,
  `num_streamed_requests`, `num_estimated_requests`,
  `num_anthropic_requests` (arrivées par `/v1/messages`),
  `total_latency_seconds`, `avg_latency_seconds`, `max_latency_seconds`,
  `first_request_time`, `last_request_time`.

### Dimensions et filtres

- `model` : le nom **préfixé** (`albert/openweight-large`), réutilisable
  tel quel dans une requête.
- `api_key_id` : le **condensé** de la clé du proxy (jamais la clé),
  stable d'un démarrage à l'autre. `""` sur un proxy ouvert.
  `group_by[]=api_key_id` sépare par clé, `api_key_ids[]=<condensé>`
  filtre.
- `project_id`, `user_id`, `batch` : toujours `null`. Filtrer dessus
  rend une page vide, pour ne pas sur-déclarer l'usage.

### Agrégation

Toutes les grandeurs s'additionnent (requêtes, tokens, somme des
latences) ou se maximisent (latence max). Des seaux fins recomposent
donc exactement un seau plus large, à condition d'aligner `start_time`
sur la largeur des seaux (ils se calent sur l'epoch). Il n'y a **pas de
percentile**, qui n'a pas cette propriété.

### Comptage des tokens

- Le bloc `usage` du backend quand il existe, streaming compris.
- Sinon, une estimation à ~4 caractères par token, comptée à part
  (`num_estimated_requests`).
- Une requête en erreur sans `usage` (500 du backend, 429 local, client
  parti) compte **0 token**.

### Vitesse de génération

Chaque ligne d'une réponse en flux relayée en un seul tour porte la durée
entre le premier token et le dernier morceau reçus du backend : la
génération seule, hors attente (quotas, partage équitable) et traitement
de l'invite ; un commentaire SSE d'avant le premier token ne compte pas.
La vitesse d'un modèle est la médiane, en tokens par seconde, de ses 20
dernières réponses des 7 derniers jours, réussies, à l'usage exact (le
bloc `usage` du backend), d'au moins 32 tokens et 0,5 s de flux, tous
clients confondus. La liste d'[Omarchy](omarchy.md) la publie
(`decode_tok_s`).

- Une réponse passée par la boucle des outils hébergés (un outil déclaré,
  ou `[chat].always`) n'est pas mesurée : ses tours se mêlent à
  l'exécution des outils.
- Un backend qui génère un raisonnement sans le diffuser le compte dans
  `completion_tokens` avant le premier morceau : sa vitesse paraît plus
  élevée.

## Usage d'un compte (Omarchy)

```http
GET /v1/usage/agent?tz=Europe/Paris
```

Rend l'usage et les limites du client de la clé au format des
enregistrements du panneau d'agents d'Omarchy (`schemaVersion: 1`) : le
corps se dépose tel quel dans `~/.local/state/omarchy/agents/usage/` sous
le nom `llm-proxy.json`, que le panneau affiche. La [vue
Usage](tableau-de-bord.md#la-vue-usage) montre le même enregistrement ;
*Configuration → Clients* donne un service et un minuteur systemd de
l'utilisateur qui le déposent toutes les 2 minutes.

- **Périmètre** : toutes les clés du compte et sa page ; la clé seule pour
  une clé du TOML non rattachée ; tout pour un proxy ouvert
  (`"scope": "account"`).
- **`limits`** : la [part équitable](tableau-de-bord.md#partage-équitable)
  du compte, consommée sur l'heure glissante (`title: "Session"`, sans
  `resetsAt`), et le [forfait
  mensuel](configuration.md#forfait-mensuel-mistral) de chaque backend à
  quotas du compte, ceux du TOML aussi pour un admin (`title: "Monthly"`,
  `resetsAt` de Mistral).
- **Statistiques** : les tokens par jour et par modèle préfixé sur toute la
  base (`[stats].retention_days`) ; l'entrée servie du cache est
  `cacheReadInputTokens`, le reste de l'entrée `inputTokens`. Une requête
  compte pour un prompt (`hasPromptStats: false`), sans session.
- **`tz`** : le fuseau IANA des jours (défaut : l'heure locale du proxy) ;
  un fuseau inconnu rend `400`. L'usage est découpé à la demi-heure : un
  fuseau décalé de 45 min date au plus 15 min de travers.

## Usage des outils hébergés

Chaque exécution d'un [outil hébergé](outils-heberges.md) laisse une
ligne dans la même base, sous la même purge. Cela vaut pour tous les
chemins : boucle de `/v1/responses`, boucle de `/v1/messages`, appel
direct `POST /v1/tools/<nom>`, Code Mode de la page
(`/ui/tools/<nom>`). Une réponse qui a utilisé des outils
garde **une** ligne côté requêtes, avec ses tokens cumulés.

Par exécution : horodatage, outil, route, modèle préfixé (aucun pour
l'appel direct), issue, durée, caractères rendus au modèle. **Rien du
contenu** : ni recherche, ni URL, ni arguments, ni résultat. Le journal
applicatif, lui, montre 160 caractères d'arguments et 200 d'une erreur.

| Issue | Sens |
|---|---|
| `ok` | l'outil a rendu son résultat |
| `error` | l'outil a rendu un code d'erreur, et au modèle un texte `Error: …` — moteur injoignable, page en 404, délai dépassé, adresse refusée, arguments illisibles (`num_errors`) |
| `limit` | refus par limite d'appels (`max_calls`, `max_uses` du client, limite propre à l'outil) : **rien n'a été exécuté**, durée nulle (`num_limited`) |

### Lire l'usage des outils

```http
GET /v1/organization/usage/tools
    ?start_time=<epoch>        (obligatoire)
    &end_time=<epoch>
    &bucket_width=1m|1h|1d|all
    &group_by[]=tool&group_by[]=endpoint&group_by[]=model
    &models[]=bigchuck/qwen3.8-flash-next
    &limit=<n>&page=<curseur>
```

```json
{"object": "page", "has_more": false, "next_page": null, "data": [
  {"object": "bucket", "start_time": 1791158400, "end_time": 1791244800,
   "results": [
    {"object": "organization.usage.tools.result", "num_requests": 4,
     "project_id": null, "user_id": null, "api_key_id": null,
     "model": null, "tool": "web_search", "endpoint": null,
     "num_errors": 1, "num_limited": 1,
     "total_duration_seconds": 22.0, "avg_duration_seconds": 7.333,
     "max_duration_seconds": 20.0, "result_chars": 9438,
     "first_request_time": 1791220947, "last_request_time": 1791220990}]}]}
```

- `model` : `null` pour un appel direct.
- `endpoint` : `/v1/responses`, `/v1/messages`, `/v1/chat/completions`
  ou `/v1/tools`.
- `avg_duration_seconds` porte sur les appels lancés
  (`num_requests - num_limited`).
- Tout s'additionne ou se maximise : pas de percentile.

**Forme OpenAI ou propre au proxy ?** La page, le seau, les paramètres,
les filtres (`project_ids`, `user_ids`, `api_key_ids`), les champs
`project_id`, `user_id`, `api_key_id`, `model` et `num_requests` suivent
l'Usage API d'OpenAI. Validation, alignement et pagination sont ceux de
`/completions`. Sont propres au proxy : la route `/usage/tools`, le type
`organization.usage.tools.result`, les dimensions `tool` et `endpoint`,
`num_errors`, `num_limited`, les trois durées, `result_chars`,
`first_request_time`, `last_request_time` et `bucket_width=all`.

Le proxy ne sert pas `/v1/organization/usage/web_search_calls`
d'OpenAI, qui ne compte que des recherches, sans issue ni durée : une
seule route couvre tous ses outils.

**Non mesuré** :

- le contenu (ce qui est cherché ou lu) ;
- les tokens d'un résultat d'outil (comptés dans la ligne de la requête) ;
- le lien entre une exécution et sa requête ;
- le poids d'une image générée (`result_chars` compte le texte rendu au
  modèle) ;
- les outils **du client** (un `function_call` rendu au client n'est pas
  exécuté par le proxy) ;
- une exécution interrompue parce que le client a raccroché.
