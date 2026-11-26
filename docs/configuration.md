# Configuration

Référence de `data/config.toml` : clés, valeurs par défaut et effets.
La version commentée ligne par ligne est `data/config.example.toml`.

## Le fichier et les secrets

Toute la configuration est définie dans `data/config.toml`. Au premier
démarrage, en l'absence de ce fichier, le proxy démarre sans configuration :
l'API répond 503 (`setup_required`) et `/ui` affiche la page d'installation.
Celle-ci génère `data/config.toml`, `.env` et
`docker-compose.override.yml`. Après création du fichier, un redémarrage
quitte ce mode. Le proxy n'écrit rien dans la configuration ;
`data/config.example.toml` décrit chaque réglage.

Les variables d'environnement servent uniquement à :

- **`CONFIG_PATH`** : l'emplacement du TOML.
  - Hors conteneur : `data/config.toml` à la racine du dépôt, d'où qu'on lance le proxy.
  - Dans l'image : `/app/data/config.toml`.
- **Les secrets** : toute chaîne du TOML peut contenir `${VAR}`. Elle est
  remplacée au chargement par la variable d'environnement. Les clés
  d'API restent ainsi hors du fichier et du dépôt. Une variable absente
  ou vide devient `""` ; le démarrage la signale dans un avertissement.
  Sous Docker, une clé ajoutée au `.env` doit aussi être transmise au
  conteneur (`environment:` du service, dans `docker-compose.yml` ou
  l'override) ; sinon le backend démarre sans clé.

Un 401 du backend (clé manquante ou refusée) est renvoyé au client en
502 `upstream_auth_error`. Le 401 n'est jamais transmis tel quel : le
client pourrait l'interpréter comme un refus de sa propre clé, ce qui
déconnectait la page web.

`data/config.toml` et `data/stats.db` sont dans `.gitignore`. Le dépôt
ne garde que l'exemple.

## Enregistrer depuis la page

*Configuration → Proxy → Enregistrer* (`POST /ui/config/save`) **met à
jour** `data/config.toml` sur le proxy, puis le recharge à chaud. Seul un
**admin OIDC** écrit le fichier ; sans comptes OIDC, la page lit la
configuration mais ne l'écrit pas (403) : *Copier* ou *Télécharger*, puis
remplacer le fichier à la main.

Seules les valeurs qui changent sont écrites : commentaires, ordre,
`${VAR}` et secrets écrits en clair restent tels quels ; une valeur
affichée « défaut » n'est pas ajoutée ; une valeur retirée du fichier de
la page en est retirée. Un secret écrit en clair est montré masqué, par un
condensé qui le désigne sans le révéler : déplacé ou réordonné dans la
page, il garde sa valeur ; un masque qui ne désigne aucune valeur du
fichier (la page ouverte avant un redémarrage) est refusé.
*Prompts* et *Extensions* écrivent de la même façon, dans `[prompts]` et
`[extensions.<nom>].active`.

- L'ancien fichier est gardé dans `config-history/`, à côté de
  `config.toml` (les 20 dernières versions).
- Avant d'écrire, chaque réglage est évalué à blanc sur le fichier
  proposé : une valeur que le proxy refuserait au démarrage (un entier qui
  n'en est pas un), rechargeable ou non, est refusée.
- Un TOML illisible, une clé que le proxy ne lit pas, une valeur refusée,
  ou une valeur que le rechargement refuse (le fichier est alors remis,
  s'il n'a pas changé entre-temps) : rien ne change (400).
- Le fichier modifié depuis que la page l'a lu (une autre page, une
  modification à la main) : refusé (409) ; la page relit la
  configuration, les changements sont à refaire. Sans `config.toml` (mode
  installation) : 409.
- Le proxy doit pouvoir écrire le dossier de `config.toml` (`data/`, monté
  en écriture).

*Copier* et *Télécharger* donnent le fichier complet de la page, sans
l'écrire.

Au premier démarrage après la mise à jour, d'anciens `data/prompts.json`
et `data/extensions.json` sont reportés dans `config.toml`, puis renommés
`*.migre-AAAAMMJJ`. Sans comptes OIDC, une extension ne s'active plus
depuis la page : `[extensions.<nom>].active` se règle à la main et prend
effet au redémarrage (l'état existant est migré).

## Rechargement à chaud

Le proxy relit `data/config.toml` sans redémarrer :

- par **SIGHUP** : `docker compose kill -s HUP albert-proxy` (ou
  `kill -HUP <pid>` hors conteneur) ;
- par la page : **Configuration → Proxy → Recharger**
  (`POST /ui/config/reload`, admins seulement avec OIDC), ou après
  [*Enregistrer*](#enregistrer-depuis-la-page).

Seuls ces réglages s'appliquent à chaud ; ils sont lus à chaque usage :

| Table | Clés |
|---|---|
| `[proxy]` | `api_keys` (retirer toutes les clés ouvre le proxy, en ajouter le ferme) |
| `[anthropic]` | `model_map`, `ping_interval`, `trace`, `reasoning_as_thinking` |
| `[responses]` | `reasoning_as_summary` |
| `[chat]` | `always`, `always_except`, `annotations` |
| `[fair_share]` | `tokens_per_hour` |
| `[tools]` | `max_calls`, `run_timeout`, `max_result_chars`, `direct_calls_per_minute` |
| `[quotas]` | `max_queue_seconds` |
| `[mcp_server]` | `allowed_origins` |
| `[prompts]` | toute la table ([Prompts](prompts.md)) |
| `[ui]` | `page_tools_disabled`, `chat_max_rounds` (la page la reçoit avec sa session suivante), `linux_relay_max_bytes`, `linux_relay_idle_seconds` |

Tout autre changement (backends, outils activés, chemins, OIDC…) n'est
pris en compte qu'après redémarrage. Le journal et la page indiquent
« attend un redémarrage » ; la configuration affichée reste celle en
vigueur. Un fichier invalide (TOML illisible, valeur du mauvais type) est
refusé en entier : rien ne change. Les variables `${VAR}` sont relues dans
l'environnement du processus, qui ne change pas : un secret modifié dans
le `.env` nécessite de recréer le conteneur.

## `[backends.<nom>]`

Une table par backend. **Le nom de la table est le préfixe de routage.**
Sans aucune table, le proxy n'a aucun backend du TOML : il démarre et sert
les [backends des comptes](backends.md).

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
| `api` | `"openai"` | Protocole du backend : `"openai"` (`/v1/chat/completions`) ou `"anthropic"` (`/v1/messages`). Voir [Backend Anthropic](#backend-anthropic) |
| `headers` | `{}` | En-têtes ajoutés à chaque requête vers le backend (relais, catalogue, limites, outils hébergés) : `[backends.<nom>.headers]`, `"X-Equipe" = "${EQUIPE}"`, `"Authorization" = "Bearer ${TOKEN}"`. Ils passent en dernier et l'emportent sur la clé et sur les en-têtes du client ; un `anthropic-beta` propre est complété, pas remplacé. Une valeur vide (variable absente du `.env`) n'est pas envoyée, le journal le signale. Noms : `A-Z a-z 0-9 . _ -`, 64 caractères au plus ; valeurs en ASCII imprimable, 2 000 au plus ; 20 en-têtes au plus. Refusés : ceux que règle le proxy (`Host`, `Content-Length`, `Content-Type`, `Connection`…) |
| `api_key` | *(aucune)* | Champ de configuration de la clé du backend (clé Albert, `--api-key` de llama-server). Si définie, elle remplace l'`Authorization` du client |
| `quotas` | `false` | Active le limiteur : fenêtres minute et jour, attente puis `429` local. Voir [`[quotas]`](#quotas) |
| `limits` | `[]` | Avec `quotas` : règles `[[backends.<nom>.limits]]`, `models = ["claude-*", …]` (motifs, casse ignorée, nom sans préfixe) et des plafonds `rpm`, `tpm`, `rpd`, `tpd` (absent = illimité). La première règle qui correspond s'applique ; ses modèles partagent ses compteurs. Sans `quotas`, ignorées |
| `timeout` | `proxy.upstream_timeout` | Secondes pour la génération |
| `meta_timeout` | `proxy.meta_timeout` | Secondes pour `/v1/models`, `/v1/limits`, `tokenize_path` |
| `connect_timeout` | `1` sans quota, `15` avec quotas ou `api = "anthropic"` | Délai limité à l'établissement de la connexion TCP. Un backend local arrêté échoue en 1 s ; un hôte joignable répond rapidement, même s'il est occupé |
| `verify_ssl` | `true` | `false` pour un certificat auto-signé |
| `force_tool_choice` | *(aucune injection)* | Injecte `tool_choice` quand `tools` est présent sans lui. `true` → `proxy.tool_choice` ; `"auto"`, `"required"`… → cette valeur. Albert en a besoin (`"auto"`) : son schéma a `"none"` pour défaut |
| `max_tokens` | `0` (aucun) | Plafond : le `max_tokens` / `max_completion_tokens` du client y est ramené. Jamais augmenté ni ajouté |
| `images` | `false` (`true` avec `api = "anthropic"`) | Les modèles multimodaux du backend reçoivent les `image_url` d'un client Anthropic. Sinon, texte de remplacement |
| `effort_map` | `{}` (niveaux tels quels) | Niveau de réflexion demandé → niveau envoyé, pour s'en tenir à ceux que le backend accepte : `{ minimal = "low", medium = "high", xhigh = "high", none = "" }` (DeepSeek V4 Flash : `low`, `high`, `max`). Niveaux : `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max` ; `""` : `reasoning_effort` n'est pas envoyé. S'applique au `reasoning_effort` de la requête comme à celui de `chat_template_kwargs` (backend à gabarit ou non). Sans effet avec `api = "anthropic"` |
| `model_efforts` | `{}` (non dits) | Les niveaux de réflexion que proposent ses modèles, par motif de noms sans préfixe (jokers shell, casse ignorée) : `{ "qwen3.8-*" = ["none", "low", "medium", "high", "xhigh"], "deepseek-*" = ["none", "low", "high", "max"] }`. Le premier motif qui vise un modèle l'emporte. Niveaux : ceux d'`effort_map`, dans l'ordre donné ; 100 motifs au plus, de 200 caractères au plus. Rendus par `/ui/models` (`efforts`) pour le [catalogue de Codex](tableau-de-bord.md#longlet-configuration) ; un modèle sans motif y propose `low`, `medium` et `high`. Ne change rien aux requêtes |
| `tokenize_path` | *(aucun)* | Endpoint de tokenisation pour un `count_tokens` exact. llama.cpp : `"/tokenize"` |
| `chat_template` | `false` | Serveur à gabarit (llama.cpp, vLLM) : `chat_template_kwargs`, `store` et le `reasoning_content` / `reasoning` des messages de l'historique lui sont transmis. `false` : ils sont retirés, une API stricte (Mistral) refusant la requête qui les porte (`422`) ; un niveau de réflexion `chat_template_kwargs.reasoning_effort` devient `reasoning_effort` si la requête n'en a pas, puis passe par `effort_map`. Un `enable_thinking: false` n'a pas d'équivalent : le modèle garde son comportement par défaut. Sans effet avec `api = "anthropic"`. Mise à jour : un backend llama.cpp ou vLLM déjà déclaré reçoit `chat_template = true` (case *Gabarit* pour un backend de compte) |
| `fair_share` | `true` | Le backend relève du [partage équitable](tableau-de-bord.md#partage-équitable). `false` : ses requêtes ne comptent dans la part de personne et n'attendent jamais |
| `models` | `[]` (tous) | Les modèles exposés : motifs de noms sans préfixe (`["qwen3-*", "gpt-oss-120b"]`, jokers shell, casse ignorée). Les autres ne figurent ni dans `/v1/models` ni dans `/ui/health` ; une requête qui les vise reçoit `404` `model_not_found`, et un outil hébergé réglé sur l'un d'eux est indisponible. Un alias du catalogue suit son modèle, une fois le catalogue du backend lu |
| `model_types` | `{}` | Type imposé à des modèles, par motif (`"*-vision-*" = "image-text-to-text"`). Sinon : le catalogue du backend, ou, à défaut (llama-swap), le nom du modèle (`image`, `tts`, `asr`, `embed`…). Détermine les modèles proposés à un client de chat et ceux qui reçoivent les images. Chez Mistral, dont le `type` vaut `base` ou `fine-tuned`, le type vient des `capabilities` (`completion_chat`, `vision`, `ocr`, `audio_transcription`, `audio_speech`, `moderation`) ; sans aucune capacité, embeddings ; une capacité que le proxy ne sert pas (transcription temps réel) donne `other`, proposé nulle part |

**Catalogue multi-protocoles.** Quand le catalogue d'un backend dit, par
modèle, les API qui le servent (champ `api`), seuls restent les modèles
que son protocole sait parler : `chat-completions`, `embeddings` et
`images` pour `api = "openai"`, `messages` pour `api = "anthropic"`. Les
types `chat`, `embedding` et `image` d'un tel catalogue sont ramenés à
ceux d'Albert. Exemple, OpenCode Zen : deux backends avec la même clé,
`https://opencode.ai/inference/openai` (`openai`) et
`https://opencode.ai/inference/anthropic` (`anthropic`). Ses modèles GPT
(API Responses) et Gemini ne sont pas servis ; ses modèles gratuits
répondent `403` à tout client autre qu'OpenCode.

**Contenu en morceaux.** Un backend `openai` dont la réponse de chat porte
un `content` en liste de morceaux (Mistral : `text` et `thinking`, en flux
comme en JSON) est ramené à la forme habituelle : le texte en chaîne dans
`content`, la réflexion dans `reasoning_content`. Les clients et les
traductions du proxy (`/v1/messages`, `/v1/responses`, outils hébergés)
lisent alors l'une et l'autre ; un client qui attend les morceaux de
Mistral ne les reçoit pas. Une réponse ordinaire passe inchangée.

### Backend Anthropic

```toml
[backends.claude]
url = "https://api.anthropic.com"
api_key = "${ANTHROPIC_API_KEY}"
api = "anthropic"
```

Les modèles se nomment `claude/claude-opus-5-5`. La communication du
proxy utilise le format chat/completions : les requêtes sont traduites en
Messages avant envoi et les réponses (JSON ou flux) retraduites à
réception. Toutes les surfaces utilisent cette conversion, y compris la
surface Anthropic (Claude Code : Messages → chat/completions → Messages ;
outils hébergés du proxy actifs).

- **Clé** transmise dans `x-api-key`, avec `anthropic-version`.
- **Catalogue** lu sur `/v1/models` d'Anthropic : contexte
  (`max_input_tokens`), images et sortie maximale (`max_tokens` par
  défaut, plafonné à 32 000).
- **Cache de prompt** demandé à chaque requête (`cache_control`
  automatique) : un agent relit son historique au tarif du cache. Les
  tokens lus du cache sont comptés (`cached_tokens`).
- **Réflexion** : sur les modèles à réflexion adaptative, elle est
  demandée en résumé et renvoyée dans `reasoning_content`. Les tours avec
  appel d'outil sont conservés en mémoire tels qu'Anthropic les a rendus
  (4 096 tours, perdus au redémarrage). Si le client les renvoie sans
  modification, ils sont rejoués tels quels, y compris la réflexion signée
  et son ordre ; un bloc de réflexion déplacé est écarté. Chaque bloc est
  lié à la conversation qui l'a produit. Si le système ou les outils ont
  changé depuis, Anthropic écarte le bloc (`drop_block`, bêta
  `thinking-binding-controls-2026-08-01`) au lieu de refuser la requête.
- **`reasoning_effort`** devient `output_config.effort` si le modèle
  accepte ce niveau (`minimal` et `none` deviennent `low`). À défaut, le
  niveau vient de `chat_template_kwargs` (pi, omp) ; `enable_thinking:
  false` donne `low` (la réflexion ne se désactive pas sur les modèles
  récents).
- **Paramètres supprimés** : `temperature`, `top_p`, `top_k` (refusés par
  les modèles récents), `logprobs`, `seed`, `n`… Les messages système et
  développeur sont remontés en tête.
- **Outils** : un `tool_choice` qui force l'appel (`"required"`, fonction
  nommée) devient `auto`, laissant l'appel au choix du modèle. Opus 5.5,
  Sonnet 5.5 et Fable 5.1+ refusent le forçage (400).
- **Seul `/v1/chat/completions`** : embeddings, voix et images → 404. L'OCR
  et la transcription utilisent un modèle d'un autre backend.

**Tout modèle doit être préfixé.** Un préfixe inconnu donne `400
unknown_backend_prefix`, un modèle que le compte ne peut pas employer
`403 model_not_allowed` (voir [modèles permis](tableau-de-bord.md#comptes-oidc)),
un modèle que son backend n'expose pas `404 model_not_found` (voir
[`models`](#backendsnom)). Une requête sans champ `model` (corps non JSON
compris) n'a rien à router : `400 model_required`.

## `[proxy]`

| Clé | Défaut | Rôle |
|---|---|---|
| `api_keys` | `[]` | Clés exigées des clients : `Authorization: Bearer <clé>` (OpenAI) ou `x-api-key: <clé>` (Anthropic). Liste vide = proxy ouvert. Sinon 401, sauf `/healthz` (réduit à `{"status": "ok"}`). `/ui` demande la clé à l'entrée. Voir [Sécurité](securite.md) |
| `upstream_timeout` | `600` | Secondes. Large pour les longues générations |
| `meta_timeout` | `5` | Secondes pour `/v1/models`, `/v1/limits` et `tokenize_path`. Délai court pour ne pas bloquer le catalogue sur un backend lent |
| `catalog_ttl` | `30` | Durée de mise en cache du catalogue d'un backend, réponse ou silence. À expiration, le proxy le relit en arrière-plan et continue de servir l'ancien |
| `tool_choice` | `"auto"` | Valeur injectée lorsque `force_tool_choice = true` pour un backend |
| `forward_post_paths` | `["/v1/completions", "/v1/embeddings", "/v1/rerank", "/v1/audio/transcriptions", "/v1/audio/speech", "/v1/images/generations", "/v1/images/edits", "/v1/ocr"]` | Routes POST relayées telles quelles, en plus des handlers dédiés. Toute autre route sans handler répond 404 |
| `exempt_paths` | `["/embeddings", "/rerank", "/audio/transcriptions", "/ocr"]` | Suffixes exclus du limiteur de quotas et du partage équitable |
| `log_level` | `"INFO"` | `INFO` enregistre chaque injection et chaque mise en attente |

## `[stats]`

| Clé | Défaut | Rôle |
|---|---|---|
| `database` | `"stats.db"` | Base SQLite. Relatif = à côté de `config.toml` |
| `retention_days` | `90` | Purge des lignes plus anciennes. `0` = conservation illimitée |

## `[anthropic]`

Sans cette table, la surface est inactive ; l'état est signalé au
démarrage et dans `/healthz`.

| Clé | Défaut | Rôle |
|---|---|---|
| `enabled` | `false` | Ouvre la surface Anthropic (`/v1/messages`…) |
| `model_map` | `{}` | Noms Anthropic → noms préfixés. `default` attrape les inconnus. Voir [API Anthropic Messages](api-anthropic.md#noms-de-modèles--anthropicmodel_map) |
| `ping_interval` | `10` | Secondes entre deux pings pendant une attente en flux (limiteur, partage équitable, outil hébergé), sur les trois surfaces : `event: ping` pour `/v1/messages`, commentaire SSE ailleurs. `0` = attendre avant de répondre |
| `reasoning_as_thinking` | `true` | `reasoning_content` du backend → bloc `thinking` |
| `trace` | `false` | Une ligne de log par réponse (toutes surfaces) : `stop_reason`, outils appelés (nom, extrait des arguments), tokens. Sert à diagnostiquer les actions ou répétitions d'un agent |

## `[responses]`

Sans cette table, la surface est inactive ; l'état est signalé au
démarrage et dans `/healthz`.
Voir [API OpenAI Responses](api-responses.md).

| Clé | Défaut | Rôle |
|---|---|---|
| `enabled` | `false` | Ouvre la surface Responses (`POST /v1/responses`) |
| `reasoning_as_summary` | `true` | `reasoning_content` du backend → élément `reasoning` (résumé) |

## `[chat]`

Configuration des outils hébergés utilisés sur `/v1/chat/completions` :
[Outils hébergés](outils-heberges.md).

| Clé | Défaut | Rôle |
|---|---|---|
| `hosted_tools` | `false` | Le proxy boucle lui-même une requête qui déclare `{"type": "web_search"}` dans `tools` (ou `web_search_options`). Inactif : relais brut au backend. Voir [Client chat/completions](outils-heberges.md#client-chatcompletions--déclarer-loutil) |
| `always` | `[]` | Outils présentés d'office à toute requête, sans déclaration (`["web_search", "web_fetch"]`). `mcp:<serveur>` = tous les outils de ce serveur. Nom inconnu ou outil désactivé : ignoré, avec avertissement au démarrage. Sans effet si `hosted_tools` est faux. Voir [Outils présentés d'office](outils-heberges.md#outils-présentés-doffice) |
| `always_except` | `[]` | Modèles exclus de `always`. Motifs à jokers sur `<backend>/<modèle>`, casse ignorée (`["bigchuck/qwen3.8-nano*"]`). Utile pour le modèle de tâches d'une interface. Une déclaration du client reste honorée |
| `annotations` | `true` | Annotations `url_citation` en fin de réponse, pour les URL rendues par un outil et écrites par le modèle |
| `memory` | `true` | Garde l'échange caché de chaque réponse (appels hébergés, résultats) et le réinsère à la requête suivante. `false` : le modèle ne retrouve que sa réponse. Taille et durée : `[tools].cache_entries`, `cache_ttl`. Voir [Mémoire des résultats](outils-heberges.md#mémoire-des-résultats) |
| `memory_chars` | `8000000` | Caractères gardés par cette mémoire, en tout (8 à 32 Mo de RAM selon le texte). Au-delà, les moins récemment relus sortent |

## `[tools]`

Réglages communs aux [outils hébergés](outils-heberges.md).

| Clé | Défaut | Rôle |
|---|---|---|
| `max_calls` | `8` | Appels hébergés par réponse. Au-delà, le modèle reçoit une erreur qui lui demande de conclure, et le tour suivant ne lui présente plus ces outils. Le `max_uses` d'un outil serveur Anthropic peut l'abaisser, jamais le relever. Un outil peut avoir son propre compte (`Tool.max_calls`, voir [outils.md](outils.md)) |
| `run_timeout` | `60` | Secondes par exécution, redirections comprises. Dépassé → erreur rendue au modèle. Un outil peut avoir son propre délai (`Tool.timeout`) |
| `max_result_chars` | `24000` | Caractères d'un résultat. Le surplus est coupé et marqué `[truncated]` |
| `cache_entries` | `512` | Appels gardés par la [mémoire des résultats](outils-heberges.md#mémoire-des-résultats). Les moins récemment relus sortent |
| `cache_ttl` | `86400` | Secondes de vie d'une entrée de cette mémoire |
| `web_cache_ttl` | `600` | Cache web : secondes pendant lesquelles une page ou une recherche est resservie. `0` = pas de cache |
| `web_cache_entries` | `256` | Cache web : nombre d'entrées |
| `web_cache_bytes` | `64000000` | Cache web : taille cumulée, en octets |
| `direct_calls_per_minute` | `120` | Appels directs d'outils (`/v1/tools/<nom>`, `tools/call` de `/mcp`, `/ui/tools/<nom>` du Code Mode de la page) par client et par minute glissante. Au-delà : `429` avec `Retry-After`. `0` = sans limite. La boucle des surfaces n'est pas concernée |

## `[tools.web_search]`

| Clé | Défaut | Rôle |
|---|---|---|
| `enabled` | `false` | Présente `web_search` quand le client déclare `web_search` (Responses) ou l'outil serveur `web_search_…` (Anthropic). Inactif : l'outil du client est ignoré |
| `searxng_url` | `""` | Base de l'instance SearXNG, appelée en `<searxng_url>/search?q=…&format=json`. Compose : `"http://searxng:8080"`. Vide → « recherche non configurée ». Adresse de configuration : peut être privée |
| `timeout` | `20` | Secondes pour la requête vers SearXNG |
| `limit` | `8` | Résultats rendus si le modèle ne précise pas `limit` (20 au plus) |
| `language` | `""` | Paramètre `language` de SearXNG (`"fr"`, `"en-US"`…). Vide = défaut de l'instance |
| `categories` | `""` | Paramètre `categories` de SearXNG (`"general"`, `"general,it"`…). Vide = non envoyé |

## `[tools.web_fetch]`

Adresses auxquelles l'outil peut se connecter : [`[tools.net]`](#toolsnet).

| Clé | Défaut | Rôle |
|---|---|---|
| `enabled` | `false` | Présente `web_fetch` quand un client Responses déclare `web_search` (sans lui, la recherche ne rend que des extraits de 240 caractères). À un client Anthropic, seulement s'il déclare l'outil serveur `web_fetch_…` |
| `timeout` | `20` | Secondes par requête (une par redirection, 5 au plus) |
| `max_bytes` | `2000000` | Octets lus au plus sur le corps d'une page |
| `pdf_max_bytes` | `20000000` | Taille maximale d'un PDF (reconnu au `Content-Type` ou aux octets `%PDF-`). Plus gros : refusé |
| `max_chars` | `20000` | Caractères rendus par appel. La suite se demande par `offset` |

## `[tools.web_grep]`

| Clé | Défaut | Rôle |
|---|---|---|
| `enabled` | `false` | Présente `web_grep` avec `web_search` (comme `web_fetch`), et l'expose par `/v1/tools` et `/mcp`. Garde-fou, conversion et cache : ceux de `web_fetch` |
| `max_matches` | `20` | Lignes rendues si le modèle n'en précise pas le nombre (50 au plus) |
| `context` | `1` | Lignes non vides de chaque côté d'une ligne trouvée (5 au plus) |
| `regex_timeout` | `1.0` | Secondes pour la recherche du motif, lecture non comprise. Au-delà, coupée |

## `[tools.ocr]`

Adresses auxquelles l'outil peut se connecter : [`[tools.net]`](#toolsnet).

| Clé | Défaut | Rôle |
|---|---|---|
| `enabled` | `false` | Active l'outil `ocr` (`/v1/chat/completions` et `/v1/tools`) |
| `model` | `""` | Modèle de vision `"<backend>/<modèle>"`, sur un backend à `images = true`. Vide, préfixe inconnu, backend sans images ou modèle non multimodal → « ocr non configuré », raison dans le journal |
| `timeout` | `50` | Secondes par exécution, téléchargement compris. Passé ce délai, les pages déjà lues sont rendues. Remplace `[tools].run_timeout` (plus 10 s de marge) |
| `max_bytes` | `20000000` | Taille maximale du fichier téléchargé. Plus gros : refusé |
| `max_image_bytes` | `5000000` | Taille maximale d'une image envoyée au modèle de vision |
| `max_pages` | `4` | Pages d'un PDF lues par appel, une requête chacune. La suite se demande par `pages` |
| `concurrency` | `2` | Pages lues en même temps |
| `max_chars` | `20000` | Caractères rendus par appel |
| `max_tokens` | `4096` | `max_tokens` de chaque requête de vision, ramené au plafond du backend s'il est plus bas |

## `[tools.transcribe]`

Adresses auxquelles l'outil peut se connecter : [`[tools.net]`](#toolsnet).

| Clé | Défaut | Rôle |
|---|---|---|
| `enabled` | `false` | Active l'outil `transcribe` (`/v1/chat/completions` et `/v1/tools`) |
| `model` | `""` | Modèle de transcription `"<backend>/<modèle>"`. Vide ou préfixe inconnu → « transcription non configurée » |
| `formats` | tous | Formats lus tels quels par le modèle, parmi `mp3`, `wav`, `flac`, `ogg`, `m4a`, `aac`, `webm`, `amr`, `mp4`. Qwen3-ASR sous gufo : `["wav"]` |
| `convert` | `true` | Convertit en WAV 16 kHz mono, par ffmpeg, les formats absents de `formats` (qui doit contenir `wav`). Voir la note ffmpeg ci-dessous |
| `ffmpeg` | `"ffmpeg"` | Le binaire : nom cherché dans `PATH`, ou chemin |
| `convert_timeout` | `60` | Secondes par conversion. S'ajoute au délai de l'outil |
| `convert_max_bytes` | `100000000` | Taille maximale du WAV produit (1,92 Mo/min, soit environ 52 min). Au-delà : refusé, pas coupé |
| `response_format` | `"verbose_json"` | Demandé au backend. `verbose_json` rend aussi langue et durée. `json` : texte seul, pour un backend qui refuse l'autre |
| `timeout` | `300` | Secondes pour la requête de transcription |
| `download_timeout` | `60` | Secondes pour le téléchargement, redirections comprises |
| `max_bytes` | `25000000` | Taille maximale du fichier. Plus gros : **refusé** (une transcription partielle passerait pour entière) |
| `max_chars` | `20000` | Caractères rendus par appel. La suite se demande par `offset`, sans retranscrire |
| `language` | `""` | Langue (ISO 639-1) si l'appel n'en donne pas. Vide = détection |
| `cache_ttl`, `cache_entries` | `3600`, `64` | Cache des transcriptions (texte seul, jamais l'audio), par URL et langue. `cache_ttl = 0` = pas de cache |

Délai de l'outil : `timeout` + `download_timeout`, plus `convert_timeout`
quand ffmpeg convertit. Il remplace `[tools].run_timeout`.

**ffmpeg.** L'image Docker l'embarque. Hors conteneur sans ffmpeg, le
proxy démarre et le signale. Les formats à convertir sont alors refusés
avant l'envoi, comme avec `convert = false`. ffmpeg décode des octets
venus du web. Il tourne avec arguments fixes, format d'entrée imposé,
protocole `file` seul, dossier temporaire supprimé, délai et taille
bornés. Mais il n'est **pas isolé** du proxy.

## `[tools.image_generation]`

Adresses auxquelles l'outil peut se connecter (image à retoucher ou
`url` hors de l'origine du backend) : [`[tools.net]`](#toolsnet).

| Clé | Défaut | Rôle |
|---|---|---|
| `enabled` | `false` | Active l'outil `image_generation` (`/v1/chat/completions` et `/v1/tools`). Exige `[files].public_url` : sinon rien n'est généré et le modèle est prévenu |
| `model` | `""` | Modèle d'images `"<backend>/<modèle>"`. Vide ou préfixe inconnu → « génération d'images non configurée ». Adresse de configuration : `[tools.net]` ne s'y applique pas |
| `sizes` | `["512x512", "768x768", "1024x1024"]` | Tailles autorisées (`LxH`). Une autre est refusée sans appeler le backend |
| `size` | `"512x512"` | Taille par défaut. Doit figurer dans `sizes` |
| `steps` | `{}` | Passes de débruitage proposées par le chat de la page, par modèle : `{ "<motif>" = [passes…] }`, motif sur `<backend>/<modèle>` (`*` = n'importe quoi). Un modèle qu'aucun motif ne couvre n'a pas de choix et ne reçoit jamais `steps` (champ hors API OpenAI, que lit gufo ; Qwen-Image-2.1-Turbo n'accepte que ses 8 passes). Le choix « auto » laisse le défaut du backend |
| `loras` | `{}` | LoRA proposés par le chat de la page, par modèle : `{ "<motif>" = [noms…] }`, même motif que `steps`, noms sous lesquels le backend les a enregistrés (gufo : `--lora NOM=CHEMIN`, Qwen-Image-2.1). Un modèle qu'aucun motif ne couvre n'a pas de choix et ne reçoit jamais `loras` (champ hors API OpenAI). Choisi : `"loras": [{"name": …, "scale": 1}]`, en génération comme en retouche ; « sans LoRA » n'envoie rien (le défaut du backend) |
| `upscale_model` | `""` | Bouton *Agrandir* du chat de la page : modèle `"<backend>/<modèle>"` qui RETOUCHE l'image à une plus grande taille (`/v1/images/edits`). Il redessine l'image : ce n'est pas un super-résolveur. Consigne : le prompt `image_generation.upscale` ([Prompts](prompts.md)). Vide : pas de bouton |
| `upscale_factors` | `[1.5, 2]` | Facteurs proposés, tous > 1 |
| `upscale_max_pixels` | `4194304` | Surface rendue au plus (2048², limite de Qwen-Image-2.1) ; côtés en multiples de 32, 4096 au plus |
| `upscale_strengths` | Détaillé 1, Équilibré 0,6, Fidèle 0,4 | Forces de la retouche proposées par le chat de la page : `[{ label = "…", value = … }, …]`, affichées « libellé (valeur) », la première choisie par défaut ; `value` de 0,01 à 1 (`strength`, image vers image de gufo 0.35.0). Sous 1, le modèle part de l'image agrandie et ne joue que les dernières passes (passes × force), l'image étant aussi envoyée en référence. Mesuré le 17/11/2026 (Qwen-Image-2.1-Turbo, 512² vers 1024²) : à 0,6 et 0,4, plus fidèle (PSNR 31,5 et 32,1 dB contre 28,9) mais nettement plus mou (2,5 à 3 fois moins de détail ajouté) ; 1 ajoute le plus de détail en gardant la composition, champ non envoyé. Une seule force : pas de sélecteur |
| `timeout` | `300` | Secondes pour la requête au backend, chargement du modèle compris |
| `download_timeout` | `30` | Secondes par téléchargement (image rendue par `url`, image à retoucher). Délai de l'outil : `timeout` + 2 × `download_timeout`, à la place de `[tools].run_timeout` |
| `max_calls` | `2` | Images par réponse (une par appel), comptées à part de `[tools].max_calls` |
| `max_bytes` | `10000000` | Taille maximale de l'image remise. `[files].max_file_bytes` s'applique aussi |
| `edits` | `false` | Retouche d'une image donnée par `image_url`, via `POST /v1/images/edits`. Non testée contre un vrai backend |
| `max_input_bytes` | `10000000` | Taille maximale de l'image à retoucher. Plus grosse : refusée |

## `[tools.code_execution]`

Voir [Exécution de code](execution-de-code.md).

| Clé | Défaut | Rôle |
|---|---|---|
| `enabled` | `false` | Active l'outil `code_execution` (`/v1/chat/completions` et `/v1/tools`). Sans exécuteur joignable, l'outil rend `unavailable` au modèle |
| `url` | `""` (exemple : `"http://executor:8080"`) | Base du service exécuteur. Vide → « non configuré ». Adresse de configuration, privée : `[tools.net]` ne s'y applique pas |
| `token` | `""` (exemple : `"${EXECUTOR_TOKEN}"`) | Jeton partagé avec l'exécuteur. Vide → « non configuré » |
| `packages` | `false` | Annonce au modèle que le bac peut installer des paquets (pip, `npm install -g`, cargo, modules Go). Va avec `SANDBOX_EGRESS` côté exécuteur. Voir [Installer des paquets](execution-de-code.md#installer-des-paquets) |
| `timeout` | `30` | Secondes par programme, compilation comprise. Annoncé au modèle. Délai de l'outil : `timeout` + 120 s + `download_timeout` + 45 s, à la place de `[tools].run_timeout` |
| `max_calls` | `8` | Appels par réponse, comptés à part de `[tools].max_calls` |
| `max_output_chars` | `12000` | Caractères de sortie par appel. Au-delà : le début et la fin |
| `max_files` | `8` | Fichiers d'entrée (`files`) par appel. `0` : paramètre ni présenté ni accepté |
| `max_file_bytes` | `20000000` | Taille maximale d'un fichier d'entrée. Au-delà : refusé, jamais coupé |
| `max_files_bytes` | `40000000` | Taille maximale de tous les fichiers d'entrée d'un appel |
| `download_timeout` | `60` | Secondes pour tous les téléchargements d'un appel |

Les bornes d'un bac (mémoire, CPU, processus, `/work`, `/tmp`, fichiers
rendus, nombre de bacs, durées) se règlent côté exécuteur, par les
variables `SANDBOX_*` de son service dans `docker-compose.override.yml`.
L'exécuteur applique aussi ses propres limites de fichiers d'entrée
(`SANDBOX_MAX_INPUTS`, `SANDBOX_MAX_INPUT_BYTES`,
`SANDBOX_MAX_INPUT_TOTAL_BYTES`, mêmes défauts). Il ne se fie pas au
proxy : montez-les en même temps que celles-ci.

## `[tools.mcp]` et `[tools.mcp.<serveur>]`

Une sous-table par serveur MCP. Les valeurs simples de `[tools.mcp]` sont
les réglages communs. Détails et mises en garde :
`data/config.example.toml` et [outils.md](outils.md#serveurs-mcp).

| Clé | Défaut | Rôle |
|---|---|---|
| `timeout` | `30` | Secondes par appel d'outil et par découverte. Remplace `[tools].run_timeout`. Commun ou par serveur |
| `refresh` | `300` | Secondes entre deux découvertes (`tools/list`). `0` = au démarrage seulement. Commun ou par serveur |
| `startup_wait` | `5` | Attente maximale de la première découverte au démarrage du proxy |
| `max_tools` | `64` | Outils exposés par serveur. Commun ou par serveur |
| `description_chars`, `schema_chars` | `1024`, `8000` | Description coupée au-delà. Outil écarté si son schéma est plus gros |
| `max_bytes` | `4000000` | Octets lus au plus sur une réponse d'un serveur |
| `<serveur>.url` | — | Point d'accès MCP (requis). Adresse de configuration : peut être privée |
| `<serveur>.enabled` | `true` | `false` : le serveur n'est pas joint |
| `<serveur>.headers` | `{}` | En-têtes statiques (secrets par `${VAR}`) |
| `<serveur>.tools`, `<serveur>.exclude` | `[]` | Outils exposés / jamais exposés, par leur nom chez le serveur (jokers) |
| `<serveur>.prefix` | nom de la table | Préfixe des noms de fonction. `""` = aucun |
| `<serveur>.verify_ssl` | `true` | `false` : certificat TLS non vérifié |

## `[tools.memory]`

| Clé | Défaut | Rôle |
|---|---|---|
| `enabled` | `false` | Offre l'outil `memory` et la partie *Mémoire* de la configuration de `/ui` |
| `database` | `"memory.db"` | Base SQLite. Relatif = à côté de `config.toml` |
| `max_files` | `500` | Fichiers par clé |
| `max_file_chars` | `100000` | Caractères d'un fichier |
| `max_total_chars` | `2000000` | Caractères par clé, tous fichiers confondus |

## `[tools.net]`

Garde-fou **commun** à tout ce que le proxy télécharge à la demande du
modèle : `web_fetch` et tout outil qui lit un fichier par son URL. Les
bornes de taille et de durée restent propres à chaque outil.

| Clé | Défaut | Rôle |
|---|---|---|
| `allow_private` | `false` | `false` : seules les adresses **publiques** sont jointes, contrôle refait à chaque redirection. `true` lève le filtre. À réserver à un proxy dont tous les clients sont de confiance, jamais derrière un modèle qui lit le web |
| `allowed_domains` | `[]` | Non vide : **seuls** ces domaines sont joints (sous-domaines compris, chemin facultatif : `example.com/blog`). Contrôlé à chaque redirection |
| `blocked_domains` | `[]` | Domaines jamais joints, mêmes règles |

Ces clés sont encore lues dans `[tools.web_fetch]` si `[tools.net]` ne
les pose pas, avec un avertissement au démarrage qui demande de les
déplacer. Posées aux deux endroits, `[tools.net]` fait foi (avertissement
aussi).

## `[files]`

| Clé | Défaut | Rôle |
|---|---|---|
| `public_url` | `""` | Adresse sous laquelle le navigateur du client joint le proxy, sans `/` final. Vide : aucun lien écrit, aucun fichier gardé, le modèle est prévenu |
| `ttl` | `14400` | Durée de vie d'un fichier, en secondes |
| `incognito_ttl` | `900` | Durée de vie d'un fichier rendu à une conversation incognito du chat (au plus `ttl`). Voir [Le chat](chat.md) |
| `max_bytes` | `128000000` | Octets gardés en mémoire vive, tous fichiers confondus. Les plus anciens sortent |
| `max_file_bytes` | `10000000` | Taille maximale d'un fichier. Au-delà, il n'est pas gardé et le modèle en est informé |

## `[ui]`

| Clé | Défaut | Rôle |
|---|---|---|
| `chat` | `false` | Ouvre l'onglet *Chat* du tableau de bord (`POST /ui/chat/completions`). Le cookie de `/ui` y dépense des tokens, comptés pour la clé connectée. Voir [Le chat](chat.md) |
| `chat_model` | `""` | Modèle présélectionné (`<backend>/<modèle>`) pour chaque nouvelle conversation, s'il est au catalogue. Vide : le dernier choisi dans le navigateur, sinon le premier du catalogue |
| `chat_max_rounds` | `50` | Tours d'outils au plus par réponse du chat de la page, et questions à l'utilisateur au plus ; au-delà, la page arrête la réponse. 1 au moins. Modifiable dans *Configuration → Proxy*, section *Avancé* |
| `conversations_database` | `"conversations.db"` | Base SQLite de l'historique du chat (conversations et images, par compte). Relatif = à côté de `config.toml` |
| `conversation_max_bytes` | `20000000` | Taille maximale d'une conversation (texte, artifacts, fichiers produits) |
| `asset_max_bytes` | `20000000` | Taille maximale d'une image gardée |
| `history_max_bytes` | `2000000000` | Taille maximale de tout l'historique d'un compte |

## `[webhooks]`

Les webhooks entrants et sortants de chaque compte. Voir [Webhooks](webhooks.md).

| Clé | Défaut | Rôle |
|---|---|---|
| `enabled` | `false` | Ouvre `POST /hooks/<jeton>`, la partie *Webhooks* de la configuration de `/ui` et l'outil hébergé `webhook`. Exige `[ui].chat` |
| `max_per_user` | `20` | Webhooks entrants au plus par compte, et autant de sortants |
| `allow_private` | `false` | `true` : un sortant hors Apprise (une URL `http(s)://` qu'Apprise ne reconnaît pas) peut viser une adresse privée ou locale. Ne s'applique ni aux URL Apprise ni au `response_url` d'un message reçu ([Webhook sortant](webhooks.md#webhook-sortant)) |
| `timeout` | `10` | Délai (s) d'un envoi sortant ; borne aussi les délais de connexion et de lecture d'une URL Apprise (`cto=`, `rto=`) |
| `calls_per_minute` | `30` | Messages reçus au plus par minute, par webhook entrant. Au-delà : `429 rate_limited` |
| `max_running` | `3` | Réponses menées en même temps, par compte. Au-delà : `429 rate_limited` |
| `max_text_chars` | `100000` | Caractères au plus du texte d'un message reçu. Au-delà : `400 msg_too_long` |

## `[repos]`

L'outil `repository` et les sources de dépôts. Voir [Dépôts](depots.md).

| Clé | Défaut | Rôle |
|---|---|---|
| `enabled` | `false` | Offre l'outil `repository` et la partie *Dépôts* de la configuration de `/ui`. Exige l'exécuteur de `[tools.code_execution]` (`url`, `token`), où les dépôts sont lus ; l'outil `code_execution` peut rester inactif |
| `max_per_user` | `20` | Sources au plus par compte |
| `max_bytes` | `20000000` | Taille maximale d'une archive (compressée). C'est un fichier d'entrée du bac : `SANDBOX_MAX_INPUT_BYTES` de l'exécuteur au moins aussi haut |
| `max_unpacked_bytes` | `100000000` | Taille maximale d'un projet décompressé dans le bac, à tenir dans son `/work` (`SANDBOX_WORK_SIZE`). Un fichier de plus de 2 Mo est listé, mais ni lu ni cherché |
| `timeout` | `120` | Délai (s) du téléchargement entier, ou du fetch git |
| `cache_ttl` | `1800` | Durée (s) de vie d'un dépôt téléchargé après sa dernière lecture, dans le proxy (l'archive, purgée chaque minute) comme dans le bac (détruit après cette inactivité). Voir [Ce qui est gardé](depots.md#ce-qui-est-gardé-et-combien-de-temps) |
| `cache_bytes` | `200000000` | Taille maximale de toutes les archives gardées (RAM du proxy) |
| `sources.<nom>` | — | Une source partagée : `forge` (`gitlab`, `github`, `gitea`, `git`), `url`, `token` (en `${VAR}`), `username` (forge `git`), `projects` (motifs), `enabled` |

## `[user_backends]`

Les backends que chaque compte déclare. Voir [Backends des comptes](backends.md).

| Clé | Défaut | Rôle |
|---|---|---|
| `enabled` | `true` | Offre la partie *Backends* de la configuration de `/ui` et route les modèles de ces backends. Aucune règle d'adresse : voir [Sécurité](securite.md#backends-des-comptes) |
| `max_per_user` | `10` | Backends au plus par compte |

## `[extensions]` et `[extensions.<nom>]`

Des outils du proxy (Python) et du navigateur (JavaScript), un
sous-dossier par extension, chargés au démarrage. Voir
[Extensions](extensions.md).

| Clé | Défaut | Rôle |
|---|---|---|
| `path` | `""` | Le dossier des extensions, un sous-dossier chacune, hors d'atteinte du proxy (montage `:ro`). Vide : aucune extension |
| `<nom>.enabled` | `true` | `false` : l'extension `<nom>/` n'est pas chargée, et la page ne peut pas la réactiver. Une extension chargée est désactivée jusqu'à ce qu'un admin l'active dans Configuration → Extensions |
| `<nom>.active` | absent | Écrit par *Configuration → Extensions* : `true` (tous ses outils actifs) ou la liste de ses outils actifs ; absent, l'extension est désactivée. Modifié à la main, pris en compte au redémarrage. Voir [Activer et désactiver](extensions.md#activer-et-désactiver) |
| `<nom>.*` | — | Les réglages de l'extension, lus par `ext.settings("<nom>")` (sans `active`). Ceux de l'extension d'exemple `pull_request` : voir [pull_request](extensions.md#pull_request--proposer-une-modification) |

## `[prompts]`

Les textes du [catalogue des prompts](prompts.md) modifiés par l'admin :
clé du catalogue = texte (`"web_fetch.description" = "…"`). Écrite par
*Configuration → Prompts* ; une clé absente garde son texte par défaut.
Rechargeable à chaud. Les `${VAR}` n'y sont pas substitués, et un texte
qui en contient est ignoré : un prompt ne lit pas l'environnement.

## `[fair_share]`

| Clé | Défaut | Rôle |
|---|---|---|
| `tokens_per_hour` | `0` | Part par défaut de chaque client, en tokens (entrée + sortie) par heure glissante. Au-delà, ses requêtes attendent. `0` = aucune limite. Réglable par compte dans la page. Voir [Partage équitable](tableau-de-bord.md#partage-équitable) |

## `[oidc]`

Voir [Comptes OIDC](tableau-de-bord.md#comptes-oidc).

| Clé | Défaut | Rôle |
|---|---|---|
| `issuer` | `""` | Émetteur OIDC (`https://idp.example.org/realms/x`). Vide, ou `client_id` vide = pas de comptes |
| `client_id` | `""` | Identifiant du client déclaré chez le fournisseur |
| `client_secret` | `""` | Son secret, en `${VAR}` |
| `scopes` | `["openid", "email", "profile"]` | Portées demandées |
| `redirect_uri` | `""` | Adresse de retour. Vide : `[files].public_url` + `/ui/auth/callback`, sinon déduite de la requête |
| `new_accounts` | `"pending"` | Rôle d'un nouveau compte (le premier est admin) : `pending` ou `user` |
| `session_days` | `7` | Durée d'une session de la page, en jours |
| `max_keys_per_account` | `50` | Clés actives au plus par compte |
| `database` | `"accounts.db"` | Base SQLite des comptes et des condensés de clés. Relatif = à côté de `config.toml` |
| `label` | `"Se connecter"` | Libellé du bouton de connexion |

## `[mcp_server]`

| Clé | Défaut | Rôle |
|---|---|---|
| `enabled` | `false` | Sert `POST /mcp` : les outils hébergés actifs, au Model Context Protocol. Voir [Serveur MCP](outils-heberges.md#serveur-mcp--mcp) |
| `allowed_origins` | `[]` | Origines (en-tête `Origin`, envoyé par les navigateurs) admises en plus de celle du proxy. Une autre → `403` |

## `[quotas]`

Pour les backends à `quotas = true`, du TOML ou des
[comptes](backends.md). Le limiteur fait attendre une requête plutôt que la
refuser, sous des fenêtres d'une minute et d'un jour. Le coût d'une requête
est estimé à l'entrée (4 caractères par token, plus le `max_tokens`
demandé hors chat ; une requête pour les images et la transcription), puis
recalé sur les tokens réellement consommés, entrée et sortie. Une requête
que le backend refuse sans `usage` (`4xx`, par exemple un `422` sur un champ
inconnu) rend sa place ; un `429` du backend, un client parti ou une erreur
`5xx` gardent l'estimation. Une pointe peut donc dépasser un plafond ; les
fenêtres repartent de zéro au redémarrage.

Les limites d'un modèle, par priorité :

1. les règles `limits` du backend ([`[backends.<nom>]`](#backendsnom)) :
   posées par l'admin, elles l'emportent sur ce que le backend publie ;
2. celles du compte, lues sur `/v1/limits` (Albert, voir
   [plus bas](#limites-du-compte-albert)) ; un backend sans clé, ou dont
   `/v1/limits` répond `404` ou `405`, n'en a pas et n'est plus interrogé ;
3. les familles `[quotas.family_limits]` ;
4. la limite générique, par modèle, ou celle que les réponses du backend
   annoncent pour ce modèle dans leurs en-têtes (Mistral, OpenAI, Groq,
   Anthropic, voir [plus bas](#limites-annoncées-par-les-en-têtes)).

`margin` s'applique à toutes. Les limites du compte sont lues en tâche de
fond au démarrage ; en attendant, les suivantes s'appliquent.

| Clé | Défaut | Rôle |
|---|---|---|
| `margin` | `0.9` | Fraction des limites réellement utilisée (marge de sécurité) |
| `max_queue_seconds` | `900` | Attente maximale avant un 429 local (quota journalier épuisé), file derrière les requêtes précédentes comprise. Un client qui raccroche pendant l'attente quitte la file sans consommer de quota |
| `limits_refresh` | `3600` | Période de rechargement de `/v1/limits` et du [forfait mensuel](#forfait-mensuel-mistral). `0` : une seule lecture. Si les limites n'ont pas pu être lues au démarrage (Albert injoignable), nouvel essai toutes les 60 s jusqu'à la première lecture |
| `generic_rpm` / `generic_tpm` | `30` / `128000` | Limites d'un modèle hors règles, compte, en-têtes et familles, chacun son compteur |
| `status_interval` | `600` | Période du résumé des compteurs dans les logs |
| `[quotas.family_limits.<famille>]` | *(aucune)* | `rpm`, `tpm`, `models = [préfixes]` : limites par famille de noms de modèles, pour tous les backends à quotas. `data/config.example.toml` donne celles d'Albert |

### Limites du compte (Albert)

`GET /v1/limits` donne, pour chaque modèle permis au compte, ses plafonds
(`rpm`, `tpm`, `rpd`, `tpd`) et ce qu'il en reste. Les modèles d'un même
routeur Albert partagent un compteur, comme un modèle et ses alias
(`/v1/models`). Un modèle absent de `/v1/limits` relève des règles, des
familles ou du générique.

### Limites annoncées par les en-têtes

Les réponses relayées par un backend à quotas (chat, complétions…, hors
`[proxy].exempt_paths`, voir [`[proxy]`](#proxy)) sont lues pour les
en-têtes de limites qu'elles portent, `429` compris. Celles des outils
hébergés d'OCR, de transcription et d'images ne le sont pas.

| Fournisseur | En-têtes | Fenêtres |
|---|---|---|
| Mistral | `x-ratelimit-{limit,remaining}-{req,tokens}-{minute,day}` | minute, jour |
| OpenAI | `x-ratelimit-{limit,remaining,reset}-{requests,tokens}` | minute |
| Groq | les mêmes noms | requêtes : jour ; tokens : minute |
| Anthropic | `anthropic-ratelimit-{requests,tokens}-{limit,remaining}` | minute |

Groq se distingue d'OpenAI par son `x-ratelimit-reset-requests` : au-delà
d'une minute, les requêtes du backend sont comptées à la journée jusqu'au
redémarrage. Un compte Groq de 1 440 requêtes par jour ou plus annonce des
délais plus courts : il reste lu comme OpenAI, à la minute.

Les plafonds annoncés remplacent, type par type, la limite générique du
modèle de la requête, une fois la première réponse reçue. Ils ne remplacent
ni une règle `limits`, ni une famille, ni les limites du compte Albert, ni
le générique commun des modèles au-delà de 64 buckets.

Le restant annoncé vaut pour tous les buckets qui ont au moins un plafond :
l'amont compte aussi ce que le proxy ne voit pas (un autre client de la
même clé). Moins la marge
`margin`, il borne les requêtes admises après celle qui l'a rapporté. Une
fois épuisé, elles attendent le délai de libération annoncé
(`x-ratelimit-reset-*` d'OpenAI et de Groq, pour les requêtes et les tokens
séparément), une minute au plus, fenêtre du jour comprise. Une seule
requête part alors sonder l'amont ; les suivantes attendent sa réponse, ou
une minute de plus. Chaque réponse remplace l'annonce précédente
(plafonds, restant), sauf si elle date d'une requête admise avant.

### Forfait mensuel (Mistral)

`GET /v1/billing/budget`, lu avec la clé (ou les en-têtes propres) d'un
backend à quotas `api = "openai"`, donne le forfait du compte Mistral :
pour l'API et pour Vibe & Studio, le montant alloué pour le mois, la part
consommée, la date de réinitialisation et si le paiement à l'usage prend le
relais. Il s'affiche dans *Configuration → Quotas*, sans retenir aucune
requête.

Il est lu à l'affichage de la page, au plus une fois par
`[quotas].limits_refresh` (3 600 s par défaut ; `0` : une seule lecture),
au plus toutes les 60 s tant qu'il n'a pas pu l'être, et par *Relire les
limites* au plus une fois par 30 s. Un backend dont la route répond `401`,
`403`, `404`, `405`, ou un corps illisible ou sans forfait, n'en a pas et
n'est plus interrogé, jusqu'à un changement de ses en-têtes propres.

L'état du limiteur se lit dans
[*Configuration → Quotas*](tableau-de-bord.md#longlet-configuration).
