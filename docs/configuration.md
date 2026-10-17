# Configuration

Cette page est la référence de `data/config.toml`, table par table :
chaque clé, sa valeur par défaut et son effet. Les explications longues
sont dans les pages thématiques, en lien. La version commentée ligne à
ligne est `data/config.example.toml`.

## Le fichier et les secrets

Toute la configuration vit dans `data/config.toml`. Au premier démarrage,
le proxy le crée à partir de `data/config.example.toml`.

L'environnement ne sert qu'à deux choses :

- **`CONFIG_PATH`** : l'emplacement du TOML.
  - Hors conteneur : `data/config.toml` à la racine du dépôt, d'où qu'on lance le proxy.
  - Dans l'image : `/app/data/config.toml`.
- **Les secrets** : toute chaîne du TOML peut contenir `${VAR}`. Elle est
  remplacée au chargement par la variable d'environnement. Les clés
  d'API restent ainsi hors du fichier et hors du dépôt.

`data/config.toml` et `data/stats.db` sont dans `.gitignore`. Le dépôt
ne garde que l'exemple.

## `[backends.<nom>]`

Une table par backend. **Le nom de la table est le préfixe de routage.**
Ces tables sont la seule source des URLs.

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
| `api_key` | *(aucune)* | La clé du backend, et le seul endroit où elle vit (clé Albert, `--api-key` de llama-server). Si définie, elle remplace l'`Authorization` du client |
| `quotas` | `false` | Active le limiteur Albert (fenêtres minute et jour, `/v1/me/info`). Voir [`[quotas]`](#quotas) |
| `timeout` | `proxy.upstream_timeout` | Secondes, pour la génération |
| `meta_timeout` | `proxy.meta_timeout` | Secondes, pour `/v1/models`, `/v1/me/info`, `tokenize_path` |
| `connect_timeout` | `1` sans quota, `15` avec | Poignée de main TCP seule. Un backend local éteint échoue en 1 s ; un hôte vivant répond bien avant, même occupé |
| `verify_ssl` | `true` | `false` pour un certificat auto-signé |
| `force_tool_choice` | *(aucune injection)* | Injecte `tool_choice` quand `tools` est présent sans lui. `true` → `proxy.tool_choice` ; `"auto"`, `"required"`… → cette valeur. Exception : sans aucune table `[backends]`, Albert par défaut avec `"auto"` |
| `max_tokens` | `0` (aucun) | Plafond : le `max_tokens` / `max_completion_tokens` du client y est ramené. Jamais augmenté ni ajouté |
| `images` | `false` | Les modèles multimodaux du backend reçoivent les `image_url` d'un client Anthropic. Sinon, texte de remplacement |
| `tokenize_path` | *(aucun)* | Endpoint de tokenisation pour un `count_tokens` exact. llama.cpp : `"/tokenize"` |
| `fair_share` | `true` | Le backend relève du [partage équitable](tableau-de-bord.md#partage-équitable). `false` : ses requêtes ne comptent dans la part de personne et n'attendent jamais |
| `model_types` | `{}` | Type imposé à des modèles, par motif (`"*-vision-*" = "image-text-to-text"`). Sinon : le catalogue du backend, ou à défaut (llama-swap) le nom du modèle (`image`, `tts`, `asr`, `embed`…). Décide de ce qui est proposé à un client de chat et de qui reçoit les images |

**Tout modèle doit être préfixé.** Un préfixe inconnu donne `400
unknown_backend_prefix`. Seules les requêtes sans champ `model` (endpoints
de compte, corps non JSON) partent vers le backend à quotas.

## `[proxy]`

| Clé | Défaut | Rôle |
|---|---|---|
| `api_keys` | `[]` | Clés exigées des clients : `Authorization: Bearer <clé>` (OpenAI) ou `x-api-key: <clé>` (Anthropic). Liste vide = proxy ouvert. Sinon 401, sauf `/healthz` (réduit à `{"status": "ok"}`). `/ui` demande la clé à l'entrée. Voir [Sécurité](securite.md) |
| `upstream_timeout` | `600` | Secondes. Large pour les longues générations |
| `meta_timeout` | `5` | Secondes pour `/v1/models`, `/v1/me/info`. Court : un backend lent ne doit pas bloquer le catalogue |
| `catalog_ttl` | `30` | Secondes de garde du catalogue d'un backend (réponse ou silence). Passé ce délai, il est relu en arrière-plan et l'ancien reste servi. Seule la première lecture attend un backend éteint. Les lectures simultanées n'en font qu'une |
| `tool_choice` | `"auto"` | Valeur injectée pour les backends à `force_tool_choice = true`. L'injection est désactivée par défaut |
| `forward_post_paths` | `["/v1/completions", "/v1/embeddings", "/v1/rerank", "/v1/audio/transcriptions", "/v1/audio/speech", "/v1/images/generations", "/v1/images/edits", "/v1/ocr"]` | Routes POST relayées en plus des handlers dédiés. Les autres → 404 |
| `exempt_paths` | `["/embeddings", "/rerank", "/audio/transcriptions", "/ocr"]` | Suffixes de routes exclus du limiteur |
| `log_level` | `"INFO"` | `INFO` logue chaque injection et chaque mise en attente |

## `[stats]`

| Clé | Défaut | Rôle |
|---|---|---|
| `database` | `"stats.db"` | Base SQLite. Relatif = à côté de `config.toml` |
| `retention_days` | `90` | Purge des lignes plus anciennes. `0` = conservation illimitée |

## `[anthropic]`

Table absente = surface inactive, signalé au démarrage et dans `/healthz`.

| Clé | Défaut | Rôle |
|---|---|---|
| `enabled` | `false` | Ouvre la surface Anthropic (`/v1/messages`…) |
| `model_map` | `{}` | Noms Anthropic → noms préfixés. `default` attrape les inconnus. Voir [API Anthropic Messages](api-anthropic.md#noms-de-modèles--anthropicmodel_map) |
| `ping_interval` | `10` | Secondes entre deux `event: ping` pendant l'attente du limiteur, en flux. `0` = attendre avant de répondre |
| `reasoning_as_thinking` | `true` | `reasoning_content` du backend → bloc `thinking` |
| `trace` | `false` | Une ligne de log par réponse `/v1/messages` : `stop_reason`, outils appelés (nom, extrait des arguments), tokens. Pour voir ce qu'un agent fait ou répète |

## `[responses]`

Table absente = surface inactive, signalé au démarrage et dans `/healthz`.
Voir [API OpenAI Responses](api-responses.md).

| Clé | Défaut | Rôle |
|---|---|---|
| `enabled` | `false` | Ouvre la surface Responses (`POST /v1/responses`) |
| `reasoning_as_summary` | `true` | `reasoning_content` du backend → élément `reasoning` (résumé) |

## `[chat]`

Les outils hébergés sur `/v1/chat/completions`. Voir
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

Ce que l'outil peut joindre : [`[tools.net]`](#toolsnet).

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

Ce que l'outil peut joindre : [`[tools.net]`](#toolsnet).

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

Ce que l'outil peut joindre : [`[tools.net]`](#toolsnet).

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

Ce que l'outil peut joindre (l'image à retoucher, une `url` hors de
l'origine du backend) : [`[tools.net]`](#toolsnet).

| Clé | Défaut | Rôle |
|---|---|---|
| `enabled` | `false` | Active l'outil `image_generation` (`/v1/chat/completions` et `/v1/tools`). Exige `[files].public_url` : sinon rien n'est généré et le modèle est prévenu |
| `model` | `""` | Modèle d'images `"<backend>/<modèle>"`. Vide ou préfixe inconnu → « génération d'images non configurée ». Adresse de configuration : `[tools.net]` ne s'y applique pas |
| `sizes` | `["512x512", "768x768", "1024x1024"]` | Tailles autorisées (`LxH`). Une autre est refusée sans appeler le backend |
| `size` | `"512x512"` | Taille par défaut. Doit figurer dans `sizes` |
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
| `max_file_bytes` | `10000000` | Taille maximale d'un fichier. Au-delà, il n'est pas gardé et le modèle le sait |

## `[ui]`

| Clé | Défaut | Rôle |
|---|---|---|
| `chat` | `false` | Ouvre l'onglet *Chat* du tableau de bord (`POST /ui/chat/completions`). Le cookie de `/ui` y dépense des tokens, comptés à la clé connectée. Voir [Le chat](chat.md) |
| `chat_model` | `""` | Modèle présélectionné (`<backend>/<modèle>`) pour chaque nouvelle conversation, s'il est au catalogue. Vide : le dernier choisi dans le navigateur, sinon le premier du catalogue |
| `conversations_database` | `"conversations.db"` | Base SQLite de l'historique du chat (conversations et images, par compte). Relatif = à côté de `config.toml` |
| `conversation_max_bytes` | `20000000` | Taille maximale d'une conversation (texte, artifacts, fichiers produits) |
| `asset_max_bytes` | `20000000` | Taille maximale d'une image gardée |
| `history_max_bytes` | `2000000000` | Taille maximale de tout l'historique d'un compte |

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

Pour les backends à `quotas = true`.

| Clé | Défaut | Rôle |
|---|---|---|
| `margin` | `0.9` | Fraction des limites réellement utilisée (marge de sécurité) |
| `max_queue_seconds` | `900` | Attente maximale avant un 429 local (quota journalier épuisé) |
| `limits_refresh` | `3600` | Période de rechargement de `/v1/me/info`. `0` = jamais |
| `generic_rpm` / `generic_tpm` | `30` / `128000` | Limites des modèles hors familles connues |
| `status_interval` | `600` | Période du résumé des compteurs dans les logs |
| `[quotas.family_limits.<famille>]` | *intégré* | `rpm`, `tpm`, `models = [préfixes]` : limites statiques de repli par famille |
| `[quotas.router_models]` | *(vide)* | `<router_id> = ["préfixe", …]` : association manuelle routeurs ↔ modèles, prioritaire sur la détection |

### Association routeurs ↔ modèles (Albert)

`/v1/me/info` donne les limites par `router_id`, sans dire quels modèles
chaque routeur sert. Le proxy les associe par **signature** : chaque
famille de `[quotas.family_limits]` est rattachée au routeur qui porte
ses (rpm, tpm).

- Association ambiguë : la famille garde ses limites statiques.
  `[quotas.router_models]` permet de la fixer.
- Un id et son alias (`openai/gpt-oss-120b` ↔ `openweight-large`)
  partagent le même compteur.
