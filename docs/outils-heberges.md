# Outils hébergés

Un outil **hébergé** est un outil que le proxy exécute lui-même, à la place
d'OpenAI ou d'Anthropic. Un client de l'API Responses déclare `{"type": "web_search"}`
en comptant que le fournisseur fera la recherche : sans outil hébergé, l'outil
est retiré et le modèle n'a pas de recherche web. Pour l'activer : une instance
SearXNG et les tables `[tools.web_search]` et `[tools.web_fetch]` (voir
[Mise en route](#mise-en-route)). D'autres outils (OCR, transcription, code,
mémoire, images) se présentent sur `/v1/chat/completions`,
[`/v1/tools`](#appel-direct--v1tools) et [`/mcp`](#serveur-mcp--mcp).

Le contrat d'un outil côté code (prompt, codes d'erreur par surface, liaisons
aux protocoles) et comment en ajouter un : [`docs/outils.md`](outils.md).

## Mise en route

Le `docker-compose.yml` porte un service `searxng`, **sans port publié** :
l'instance n'a pas de limiteur, seul le proxy la joint (`http://searxng:8080`).

1. Clé secrète de SearXNG, dans `.env` (jamais dans un fichier versionné) :

   ```bash
   echo "SEARXNG_SECRET=$(openssl rand -hex 32)" >> .env
   ```

2. Dans `data/config.toml` (un fichier existant n'est pas réécrit : y recopier
   les tables depuis `data/config.example.toml`) :

   ```toml
   [responses]
   enabled = true
   [tools.web_search]
   enabled = true
   searxng_url = "http://searxng:8080"
   [tools.web_fetch]
   enabled = true
   ```

3. `docker compose up -d --build`

À savoir :

- **`SEARXNG_SECRET` n'est pas bloquante.** SearXNG refuse sa clé par défaut
  mais accepte une clé vide. Absente, `docker compose` avertit et tout démarre
  (une variable exigée arrêterait tout le compose, et toute une pile à
  `include`). L'instance n'étant joignable que du proxy, la clé ne signe rien
  d'exposé.
- **Image épinglée** sur un tag daté (`AAAA.M.J-<commit>`). Mise à jour :
  changer le tag dans `docker-compose.yml`, puis `docker compose up -d searxng`.
- **`services/searxng/settings.yml`** garde les défauts de SearXNG
  (`use_default_settings: true`) et ajoute le format `json` (sans lui,
  `format=json` répond 403). Moteurs, langue et délais s'y changent, puis
  `docker compose restart searxng`.
- **Pas de `depends_on`** : SearXNG arrêté, la recherche rend une erreur au
  modèle et le reste sert.
- **Hors compose** (`uvicorn`, Coolify) : `searxng_url` pointe une instance
  joignable, avec `json` dans ses `search.formats`.
- **La qualité dépend des moteurs** que l'adresse de la machine peut joindre
  (Brave limite le débit, DuckDuckGo oppose un CAPTCHA).

Diagnostic (l'instance n'est pas joignable de l'hôte) :

```bash
docker compose ps searxng          # « healthy » : /healthz répond
docker compose logs searxng
docker compose exec albert-proxy python -c "import urllib.request; \
  print(urllib.request.urlopen('http://searxng:8080/search?q=test&format=json').read()[:300])"
```

## Les outils

### Outils web

Les trois sont activés ensemble par le `web_search` d'un client de l'API
Responses. Un client de l'API Messages déclare, lui, les outils serveur
`web_search_20250305` et `web_fetch_20250910`, et chaque fonction n'est
présentée que si son outil est déclaré : voir
[L'outil serveur `web_search`](#loutil-serveur-web_search)
et [L'outil serveur `web_fetch`](#loutil-serveur-web_fetch).

| Fonction | Ce qu'elle fait | Par quoi |
|---|---|---|
| `web_search` (`query`, `recency`, `limit`) | Liste numérotée : titre, date, URL, extrait de 240 caractères | **SearXNG** auto-hébergé (métamoteur libre, sans clé) : `GET <searxng_url>/search?q=…&format=json` |
| `web_fetch` (`url`, `offset`) | Lit une page : HTML en texte ; JSON et texte tels quels ; texte d'un PDF (pas d'OCR : scan et PDF chiffré refusés) ; autre type refusé | Requête HTTP du proxy, sous le garde-fou réseau |
| `web_grep` (`url`, `pattern`, `case_sensitive`, `context`, `max_matches`) | Cherche une regex dans une page (PDF compris) ; rend les lignes trouvées, un peu de contexte et l'**offset** de chaque ligne pour `web_fetch`. Action `find_in_page` de `web_search_call`. Absent de `/v1/messages` | La lecture de `web_fetch` (même garde-fou, même cache), puis `regex`, coupée à `regex_timeout` |

Le schéma de `web_search` et sa sortie viennent de
[oh-my-pi](https://github.com/can1357/oh-my-pi).

### Autres outils

Ils ne remplacent l'outil d'aucun fournisseur. Ils se présentent sur
`/v1/chat/completions`, déclarés par leur nom (`{"type": "transcribe"}`) ou
d'office par `[chat].always`, et s'appellent par
[`/v1/tools`](#appel-direct--v1tools) et [`/mcp`](#serveur-mcp--mcp).

| Fonction | Ce qu'elle fait | Par quoi |
|---|---|---|
| `ocr` (`url`, `pages`) | Texte d'une image (PNG, JPEG, GIF, WebP) ou d'un PDF scanné (4 pages par appel). Recours de `web_fetch`, dont la description y renvoie | Téléchargement sous garde-fou, puis une requête chat/completions par page au modèle de vision `[tools.ocr].model` |
| `transcribe` (`url`, `language`, `offset`) | Transcrit un audio (mp3, wav, flac, ogg, m4a, aac, webm, amr, mp4 ; 25 Mo max), reconnu aux premiers octets ou au type. Texte sans horodatage, langue et durée si le backend les donne. Hors de `formats` : converti en WAV par ffmpeg, refusé sans ffmpeg (backend WAV seul : `formats = ["wav"]`) | Téléchargement sous garde-fou, puis `POST /v1/audio/transcriptions` à `[tools.transcribe].model` |
| `code_execution` (`language`, `code`, `files`) | Python, bash, JavaScript, PHP (Go, Rust après `devbox dev-env`) dans un bac à sable sans réseau. Rend code de sortie et sortie, remet les fichiers produits. `files` : URL déposées dans le bac (garde-fou `[tools.net]`). Fichiers gardés d'un appel à l'autre dans une conversation. Exécuteur injoignable : `unavailable`. Voir [Exécution de code](execution-de-code.md) | `POST /v1/execute` au service `executor` (podman sans root) |
| `memory` (`command`, `path`…) | Fichiers texte persistants sous `/memories`, cloisonnés par clé. Voir [Mémoire persistante](#mémoire-persistante-memory) | SQLite (`data/memory.db`) |
| `image_generation` (`prompt`, `size`) | Génère une image remise au client ; le modèle n'en reçoit que nom, type et dimensions. 2 par réponse. Avec `edits`, retouche `image_url`. Voir [Génération d'images](#génération-dimages) | `POST /v1/images/generations` à `[tools.image_generation].model` |

Les outils d'un serveur de [`[tools.mcp]`](outils.md#serveurs-mcp) s'ajoutent
sous le nom `<serveur>_<outil>`, ou tous ensemble par
`{"type": "mcp:<serveur>"}`. Un `mcp:<serveur>` inconnu déclaré dans `tools`
donne `400`.

## Déroulé

Sur l'API Responses :

1. Le client déclare `web_search` (ou `web_search_preview`,
   `web_search_2025_08_26`). Rien n'est ajouté sans déclaration, sauf
   `[chat].always` sur `/v1/chat/completions` (voir
   [Outils présentés d'office](#outils-présentés-doffice)).
2. Le proxy présente à la place `web_search` et `web_fetch` (ceux activés). Une
   fonction du client du même nom garde le sien ; l'outil hébergé n'est alors
   pas présenté.
3. Un appel hébergé n'est **pas** rendu en `function_call` : le proxy
   l'exécute, ajoute le résultat et **relance le backend**, jusqu'à un tour
   sans appel hébergé. Un tour mêlant appels hébergés et appels du client
   s'arrête après les premiers et rend la main.
4. Le client ne voit que des `web_search_call` terminés (action `search` ou
   `open_page`), puis la réponse.
5. Au tour suivant, il renvoie ces éléments **sans résultat** ; le proxy le
   relit dans sa [mémoire](#mémoire-des-résultats).

Sur toutes les surfaces :

- **Une erreur d'outil n'est jamais une erreur HTTP** (SearXNG injoignable,
  404, délai, adresse refusée) : le modèle reçoit `Error: …` et s'adapte.
- Log par exécution : `outil hébergé web_search(…) → N car. en 1.2s`.
- **Une** ligne de statistiques par réponse, usage cumulé. Chaque tour repasse
  par le limiteur d'un backend à quotas.
- L'`usage` **rendu au client** est celui du **dernier** tour, pas le cumul :
  son prompt relit tous les tours précédents, c'est le contexte occupé, ce
  que lit la jauge d'un client (le cumul la ferait déborder, et déclencher à
  tort un résumé automatique). Le cumul reste aux statistiques et aux quotas.
- Une ligne par **exécution d'outil** (outil, route, modèle, issue, durée,
  taille, **jamais le contenu**), lue par `GET /v1/organization/usage/tools`
  et la section *Outils* du tableau de bord : voir
  [Usage des outils hébergés](statistiques.md#usage-des-outils-hébergés).
- Logs de démarrage et `/healthz` (`tools`) listent les outils actifs,
  `max_calls` et les entrées en mémoire (un état, pas des compteurs).

## Garde-fous

**Adresses publiques seulement** (`[tools.net]`, pour `web_fetch` et tout outil
qui télécharge une cible du modèle). Sinon une page ou un prompt pourrait faire
lire au proxy son réseau : backend, SearXNG, routeur, métadonnées cloud.

- **Toutes** les adresses du nom doivent être publiques : ni privées, ni
  locales, ni lien local, ni CGNAT `100.64.0.0/10` (Tailscale), ni réservées.
- La connexion part vers l'adresse vérifiée, pas vers le nom (anti-rebinding
  DNS).
- Contrôle refait **à chaque redirection** (5 au plus). `http` et `https`
  seulement.
- `allow_private = true` lève le filtre. L'adresse de SearXNG (configuration)
  n'est pas filtrée.

| Réglage | Effet |
|---|---|
| `max_bytes` | octets lus par page, le reste n'est pas téléchargé |
| `pdf_max_bytes` | PDF refusé au-delà (coupé, il ne se lit pas) ; 500 pages extraites max, dans un fil à part |
| `max_chars` | caractères rendus par appel ; la suite par `offset` |
| `max_result_chars` | taille d'un résultat, tout outil |
| `limit` | résultats par recherche, 20 au plus |
| `timeout` | délai par requête, par outil |
| `run_timeout` | délai d'une exécution entière, redirections comprises |
| `max_calls` | exécutions par réponse ; au-delà, le modèle est prié de conclure ; 4 appels de plus et la réponse est close sans lui |

## Cache web

Pages lues et résultats de recherche sont gardés `[tools].web_cache_ttl`
(10 minutes par défaut). Les morceaux d'une page (`offset`) ne la
retéléchargent pas ; une recherche identique ne repart pas chez les moteurs,
qui bloquent vite une adresse trop pressante.

- Succès seulement : ni erreur, ni recherche vide, ni moteurs indisponibles.
- En mémoire vive, borné en entrées et en taille, commun à tous les clients.
  Les listes de domaines sont vérifiées avant la lecture du cache.
- État dans `/healthz` (`tools.web_cache` : entrées, octets, succès, échecs).

Il garde le web ; les [mémoires de conversation](#mémoire-des-résultats)
gardent ce qu'un modèle a lu.

## L'outil serveur `web_search`

Un client de l'API Messages qui déclare
`{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}` compte
qu'Anthropic fera la recherche (Claude Code le fait par une sous-requête
`/v1/messages` qui n'a que cet outil). Avec `[tools.web_search].enabled`, le
proxy l'exécute :

1. L'outil serveur `web_search_…` (toute version datée) devient la fonction
   `web_search`. **`web_fetch` n'est présenté** que si le client déclare aussi
   [`web_fetch_…`](#loutil-serveur-web_fetch). Une fonction du client nommée
   `web_search` garde son nom. Les autres outils serveur sont ignorés.
2. Même boucle que sur l'API Responses, même texte de résultats pour le
   modèle.
3. Le client reçoit, en SSE comme en JSON, ce qu'Anthropic rend :

   ```text
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
   ```

- Chaque résultat suit son appel, même pour deux recherches d'un coup.
  L'`input` reprend `query`, et `recency` / `limit` si donnés.
- Aucun résultat : `content` vide.
- Échec, sans erreur HTTP : `content` vaut
  `{"type": "web_search_tool_result_error", "error_code": …}`, avec
  `max_uses_exceeded`, `invalid_tool_input` (pas de `query`) ou `unavailable`
  (le reste). Le modèle lit le `Error: …` complet.

Réglages de l'outil du client :

- **`max_uses`** : respecté, sans dépasser `[tools].max_calls`. Recherche et
  lecture ont chacune leur compte ; `max_calls` borne le total.
- **`allowed_domains` / `blocked_domains`** : filtrent les résultats avant la
  limite, aux règles d'Anthropic (domaine nu, sous-domaines couverts, chemin
  optionnel comme `example.com/blog`). La requête n'est pas réécrite : une
  liste étroite peut ne rien laisser. Pas de jokers de chemin (coupé au premier
  `*`). Les deux listes ensemble s'appliquent (Anthropic répond 400).
- `user_location` : ignoré.

Différences avec Anthropic :

- **`encrypted_content` porte l'extrait en clair** : le bloc porte tout ce que
  le modèle a lu. **`page_age`** : date `AAAA-MM-JJ` ou `null`.
- **Aucune mémoire côté proxy.** Des blocs `server_tool_use` +
  `web_search_tool_result` rejoués redeviennent un appel et son message
  `tool`, à l'octet près (même préfixe de cache). D'une erreur, il ne reste que
  le code. Sans outil déclaré, ils sont ignorés.
- **Pas de `citations`** dans les blocs `text`.
- **Recherche et outil du client dans le même tour** : la recherche est
  exécutée, puis la main revient (`stop_reason: "tool_use"`), le bloc client
  avant ceux de la recherche. Anthropic, lui, n'exécute pas la recherche.
- **Jamais de `pause_turn`** : la limite d'appels arrête le modèle, en
  `end_turn`.
- En flux, `event: ping` pendant une recherche ou l'attente d'un quota
  (`ping_interval`). En JSON, rien avant la fin : une panne du backend en
  boucle donne son vrai statut, pas un `200`.

## L'outil serveur `web_fetch`

Un client de l'API Messages qui déclare
`{"type": "web_fetch_20250910", "name": "web_fetch"}` (ou `web_fetch_20260209`,
`…_20260309`, `…_20260318`) compte qu'Anthropic lira la page. Avec
`[tools.web_fetch].enabled`, le proxy le fait et rend, en SSE comme en JSON :

```json
{"type": "server_tool_use", "id": "srvtoolu_…", "name": "web_fetch",
 "input": {"url": "https://example.org/notes"}}
{"type": "web_fetch_tool_result", "tool_use_id": "srvtoolu_…", "content": {
   "type": "web_fetch_result", "url": "https://example.org/notes",
   "content": {"type": "document", "title": "Notes",
               "source": {"type": "text", "media_type": "text/plain",
                          "data": "URL: https://example.org/notes\nTitle: Notes\n…\n\n---\n<le texte>"}},
   "retrieved_at": "2026-10-07T09:30:00Z"}}
… "usage": {…, "server_tool_use": {"web_fetch_requests": 1}}
```

Chaque outil n'est présenté que s'il est déclaré, et la description de l'un ne
renvoie à l'autre que s'il est là.

- **`data` porte tout le texte rendu au modèle, en-tête compris** (URL lue,
  titre, type, plage). Rejouée, elle redevient le message `tool` à l'octet
  près, sans mémoire côté proxy. `url` (après redirections) et `title` (ou
  `null`) en sont tirés. D'une erreur rejouée, il ne reste que le code.
- **`offset` reste dans la fonction du modèle** (absent chez Anthropic), sinon
  il ne lirait jamais au-delà de `max_chars`. L'`input` peut donc porter
  `offset` ; un rejeu doit le redonner.
- **Erreurs**, jamais en HTTP : `content` vaut
  `{"type": "web_fetch_tool_result_error", "error_code": …}`, le modèle lit le
  `Error: …` complet.

  | `error_code` | Quand |
  |---|---|
  | `invalid_tool_input` | pas d'`url`, arguments illisibles, URL invalide ou non http(s) |
  | `url_not_allowed` | domaine hors des listes (du client ou de `[tools.net]`), adresse privée ou locale |
  | `url_not_accessible` | HTTP ≥ 400, hôte introuvable, connexion en échec, trop de redirections |
  | `too_many_requests` | la page a répondu 429 |
  | `unsupported_content_type` | ni texte, ni HTML, ni JSON, ni PDF lisible |
  | `max_uses_exceeded` | au-delà du `max_uses` de l'outil ou de `[tools].max_calls` |
  | `unavailable` | délai dépassé, tout autre échec |

- **`url_too_long` et `url_not_in_prior_context` ne sont jamais rendus.** Pas
  de borne à 250 caractères, et **aucune vérification que l'URL figurait déjà
  dans la conversation** : le modèle peut ouvrir toute adresse publique que les
  listes laissent passer (voir [Sécurité](securite.md)).
- **`max_uses`** : par outil, lectures en échec comprises.
- **`allowed_domains` / `blocked_domains`** : sur l'URL et chaque redirection,
  **en plus** de `[tools.net]` (ils ne font que restreindre). Les deux ensemble
  s'appliquent (Anthropic répond 400).
- **`max_content_tokens`** : borne un morceau à 4 caractères par token, sans
  dépasser `max_chars` ; la suite par `offset`.
- **Ignorés** : `citations`, `use_cache` (le [cache web](#cache-web) sert
  toujours), `response_inclusion`, et le filtrage dynamique des versions
  `web_fetch_20260209` et suivantes (lecture simple).
- **PDF** : texte extrait en `text/plain`, pas de document base64.
- `retrieved_at` : heure de la réponse, même depuis le cache.
  `web_fetch_requests` ne compte que les lectures abouties.

## Client chat/completions : déclarer l'outil

Avec `[chat].hosted_tools = true`, le proxy reconnaît sur
`/v1/chat/completions` la déclaration de l'API Responses, **telle quelle dans
`tools`** : `{"type": "web_search"}` et ses variantes. `web_search_options`
(champ racine d'OpenAI, le seul que son SDK écrit ici) en est un synonyme ; ses
réglages sont ignorés et le modèle peut ne pas chercher.

```bash
curl -sN http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "bigchuck/qwen3.8-flash-next", "stream": true,
       "stream_options": {"include_usage": true},
       "tools": [{"type": "web_search"}],
       "messages": [{"role": "user", "content":
         "Quelle est la dernière version de llama.cpp ? Cite ta source."}]}'
```

Même boucle, mêmes garde-fous, même limite d'appels qu'ailleurs.

- `tool_choice` forcé : ramené à `auto` après le premier tour. À la forme
  Responses (`{"type": "web_search"}`), il est traduit vers la fonction ; s'il
  vise un outil non déclaré ou désactivé : `400`.
- Outil déclaré mais désactivé (`[tools.<nom>].enabled`) : **`400`**, plutôt
  qu'une réponse sans recherche. `n` > 1 aussi.
- Type inconnu du proxy : laissé au backend.
- Fonction du client nommée `web_search` ou `web_fetch` : garde son nom.

Le client reçoit **une** réponse chat/completions ordinaire :

- Jamais d'appel hébergé en `tool_calls`.
- En flux : même `id`, deltas des tours à la suite (ligne vide entre deux
  textes), **un** `finish_reason`, **un** `usage`, celui du dernier
  tour (si `stream_options.include_usage`), puis `[DONE]`. `: ping` pendant une
  recherche ou un quota. Erreur en boucle : `{"error": …}` puis `[DONE]` (le
  `200` est parti).
- En JSON : rien avant la fin, contenu de tous les tours, usage du dernier ; une panne
  du backend donne son vrai statut.
- Sources en annotations `url_citation` (`message.annotations`, ou
  `delta.annotations` juste avant la fin) : une par **occurrence** d'une URL
  rendue par un outil et écrite **en entier** par le modèle, avec
  `start_index` / `end_index` en caractères. Recouvrement
  (`…/llama.cpp` et `…/llama.cpp/releases`) : la plus longue l'emporte. URL
  prolongée par le modèle : pas une source. Rien d'autre ne dit ce qui a été
  cherché. `[chat].annotations = false` les retire.

OpenRouter (`plugins`, `:online`, `openrouter:web_search`) et LiteLLM
(`web_search_options`) déclarent autrement ; seul le retour `url_citation` est
commun.

### Tour mixte

Le modèle appelle dans un même tour un outil hébergé et un outil du client. Le
**premier appel du tour décide** :

| Premier appel | Ce qui se passe | Coût |
|---|---|---|
| hébergé | appels hébergés exécutés, ceux du client non transmis ; le backend est relancé et le modèle les réémet | leurs arguments générés deux fois |
| du client | ses appels partent au fil de l'eau ; les hébergés suivants ne sont pas exécutés, le modèle les redemandera | une requête de recherche |

### Le tour suivant de la conversation

Le client renvoie son historique **sans** les appels hébergés. Le proxy les
**retrouve tant que sa mémoire les garde** (`[chat].memory`, actif par
défaut) : l'échange caché (messages `tool_calls` et `tool` tels que reçus par
le backend) est réinséré juste avant la réponse qu'il a produite. Le modèle
relit ce qu'il avait lu ; le cache de préfixe du backend sert.

La conversation est reconnue **à son contenu** (condensé des messages envoyés,
rôles, textes, identifiants d'appels, et de la réponse finale). Conditions :

- La requête **déclare** encore un outil hébergé, ou en reçoit un par
  `[chat].always`. Sinon relais brut (la mémoire reste pour plus tard).
- La réponse revient **avec son texte**, les messages précédents inchangés.
  Sans effet : espaces en bord, contenu en parties `text`, champs ajoutés ou
  retirés (`annotations`, `reasoning_content`, `images`…), messages `system` /
  `developer` (hors de la clé : bien des clients y écrivent l'heure).
- Entrée ni expirée (`[tools].cache_ttl`) ni évincée
  (`[tools].cache_entries`, `[chat].memory_chars`), pas de redémarrage, même
  client (cloisonnement de la [mémoire des résultats](#mémoire-des-résultats)).

Sinon (réponse modifiée ou régénérée, historique tronqué ou compacté, contenu
assistant non textuel), **rien n'est réinséré** : le modèle relira la page si
besoin, et le cache ne sert que jusqu'avant la recherche. Une conversation peut
porter plusieurs échanges cachés. Une réponse close par la limite d'appels ne
range rien. Pour garder soi-même ce que le modèle a lu :
l'[appel direct](#appel-direct--v1tools).

## Outils présentés d'office

Une interface de chat ne déclare pas d'outil. `[chat].always` nomme les outils
hébergés présentés à **toute** requête `/v1/chat/completions` :

```toml
[chat]
hosted_tools = true
always = ["web_search", "web_fetch", "mcp:deepwiki"]
# Le modèle de tâches de l'interface (titres, tags) n'en reçoit pas.
always_except = ["bigchuck/qwen3.8-nano*"]
```

- Tout outil hébergé s'y nomme, dont les outils MCP (`<serveur>_<outil>`, ou
  `mcp:<serveur>` pour tous, y compris les futurs). Pas de `mcp` nu pour tous
  les serveurs.
- `always_except` : **modèles** sans outils d'office. Jokers sur
  `<backend>/<modèle>`, casse ignorée (syntaxe des `model_types`). Ce qu'un
  client leur déclare reste honoré.
- Réglage global à la route (pas par clé). Vide par défaut : rien ne change.
- La requête est traitée comme si elle avait déclaré les outils (boucle,
  `url_citation`, [mémoire des échanges](#le-tour-suivant-de-la-conversation)).

| Requête | Présenté d'office |
|---|---|
| sans `tools` | la liste ; `tool_choice` par défaut du backend (`force_tool_choice` s'applique) |
| avec des fonctions du client | la liste, **à la suite** |
| qui déclare aussi (`{"type": "web_search"}`, `web_search_options`) | chaque outil une fois, à la place de la déclaration ; le reste à la suite |
| fonction nommée `web_search` ou `web_fetch` | pas l'homonyme (la fonction garde nom et appels) ; l'autre reste |
| `tool_choice: "none"` | rien : relais brut |
| `tool_choice` forçant une fonction du client, ou `required` | la liste ; `tool_choice` tel quel (puis `auto` après un tour hébergé) |
| `n` > 1 | rien : relais brut (pas de `400`) |
| modèle non conversationnel au catalogue (image, voix, embeddings : `type` de `/v1/models`, `model_types`) | rien : relais brut. Catalogue non lu ou modèle inconnu : présentés |
| modèle de `always_except` | rien d'office |

Un nom qui ne désigne aucun outil actif, ou un `mcp:<serveur>` absent de
`[tools.mcp]`, est **ignoré** avec un avertissement au démarrage (pas de
`400`). La liste est relue à chaque requête : un outil ajouté plus tard, ou un
serveur MCP éteint au démarrage, apparaît dès qu'il existe. Journal de
démarrage et `/healthz` : `chat.always` (serveurs développés),
`chat.always_except`.

### Limites

- **Requêtes de service** (titre, tags, suggestions, autocomplétion,
  recherche) : indiscernables d'une conversation, elles reçoivent les outils
  (tokens en plus, voire une recherche web pour un titre). Parade : le modèle
  de tâches de l'interface dans `always_except`.
- **Modèle sans appel d'outils** : le catalogue ne le dit pas. Il reçoit
  `tools` et les ignore, ou le backend refuse (erreur relayée telle quelle).
- **Tous les clients de la route** reçoivent les outils. Une interface qui
  présente aussi les siens (recherche web, exécution de code, serveurs
  d'outils) donne au modèle deux jeux d'outils ; un client qui prend ceux du
  proxy par le [serveur MCP](#serveur-mcp--mcp) les aurait deux fois.
- **Injection de prompt** dans toutes les conversations : voir
  [Ce qu'aucun garde-fou n'empêche](#ce-quaucun-garde-fou-nempêche).

## Appel direct : `/v1/tools`

Pour garder appels et résultats dans son propre historique, un client appelle
les outils directement, derrière la clé du proxy et avec les mêmes garde-fous.
Il les déclare à son modèle comme les siens ; le proxy n'a ni boucle ni mémoire.

```text
GET  /v1/tools            → les outils actifs (nom, description, schéma) :
                            leur prompt en vigueur, tel que le modèle le lit
POST /v1/tools/<nom>      → corps : les arguments, en objet JSON
                            réponse : {"name", "result", "is_error",
                                       "error", "sources", "meta"}
```

- Échec de l'outil : `200`, `Error: …` et `is_error: true`.
- Outil inconnu ou désactivé : `404` `unknown_tool`.
- `error` : code d'échec (`null` si succès) ; `sources` : pages citées ;
  `meta` : URL lue, titre, plage. Voir
  [`docs/outils.md`](outils.md#v1tools--lenveloppe).
- Pas de ligne de requête : seulement l'[usage des outils](statistiques.md#usage-des-outils-hébergés),
  route `/v1/tools`, sans modèle. Un `404` ou un corps refusé n'y laisse rien.

## Serveur MCP : `/mcp`

Les outils de `/v1/tools` au [Model Context Protocol](https://modelcontextprotocol.io),
transport « streamable HTTP », route `POST /mcp` (`[mcp_server].enabled`).
L'inverse de [`[tools.mcp]`](outils.md#serveurs-mcp), où le proxy est client
(et dont il relaie ici les outils). Cas visés : un client de l'API Messages,
qui sinon ne reçoit du proxy que la recherche et la lecture web (outils
serveur), et tout agent MCP distant.

- Méthodes : `initialize` (2025-11-25, 2025-06-18, 2025-03-26 ou 2024-11-05),
  `ping`, `tools/list` (par `Tool.spec`), `tools/call` (par `Hosted.run`).
  Notification : `202` sans corps. Autre méthode : erreur JSON-RPC `-32601`.
- **Sans état** : pas de `Mcp-Session-Id`, `GET /mcp` → `405`, toujours du
  JSON.
- Échec de l'outil : `isError: true` avec `Error: …`. Fichiers produits
  (`code_execution`, `image_generation`) : `resource_link` vers `/v1/files/…`.
- `Origin` contrôlé (celui du proxy ou `[mcp_server].allowed_origins`, sinon
  `403`). Authentification du proxy ; la clé dit qui appelle (`call.client`).
- Une ligne par exécution dans l'[usage des outils](statistiques.md#usage-des-outils-hébergés),
  route `/mcp`, sans modèle.

### `[chat].always` ou MCP

| | `[chat].always` (boucle cachée) | MCP |
|---|---|---|
| Config côté client | aucune | une déclaration du serveur MCP |
| Appels visibles dans le client | non | oui, trace et historique |
| Lu au tour suivant | réinséré depuis la RAM du proxy ; perdu au redémarrage ou à la compaction | dans l'historique de l'agent |
| Outil du proxy + outil local | un tour de plus | en parallèle |
| Choix des outils | tous, pour tous | l'agent filtre |

Ne pas cumuler : chaque outil serait présenté deux fois. Garder
`[chat].always` vide, sauf pour une interface qui ne déclare jamais d'outils.

## Génération d'images

`image_generation` fait générer une image par un backend et la **remet à
l'utilisateur**. Le modèle écrit le prompt ; le proxy appelle
`POST /v1/images/generations`, range l'image dans le magasin des fichiers
(`[files]`) et l'ajoute en markdown à la fin de la réponse. Le modèle ne reçoit
ni base64 ni URL, seulement :

```text
Image generated: image-eecf50.png (image/png, 512x512, 515 kB). It is
shown to the user with your answer; do not write a link or a markdown
image yourself. You cannot see it: do not describe details the prompt
does not state.
```

Mise en route, sur `/v1/chat/completions`, `/v1/tools` et `/mcp` :

```toml
[tools.image_generation]
enabled = true
model = "<backend>/<modèle d'images>"
```

`[files].public_url` est requis : sans lui, l'outil répond d'emblée que l'image
ne pourrait pas être remise. Déclarer ensuite `{"type": "image_generation"}`,
ou `always = ["image_generation"]` pour une interface de chat.

- **Envoyé** : `model` (sans préfixe), `prompt`, `size` ; ni `n` ni
  `response_format`. Une image par appel.
- **Reçu** : `data[0].b64_json` ou `data[0].url`. Une `url` de l'origine du
  backend (ou relative) est lue avec sa clé ; d'une **autre** origine, elle
  passe par `[tools.net]` (adresses publiques). Type et dimensions lus dans
  les octets ; seuls PNG, JPEG, GIF et WebP sont remis.

| Réglage | Défaut | Effet |
|---|---|---|
| `sizes` | | tailles permises au modèle |
| `max_calls` | 2 | images par réponse, comptées à part |
| `max_bytes` | 10 Mo | taille de l'image (et `[files].max_file_bytes`) |
| `timeout` | 300 s | requête ; délai de l'outil : `timeout` + 2 × `download_timeout` |
| `edits` | `false` | active la retouche |

| Erreur | Quand |
|---|---|
| `invalid_input` | prompt, taille hors liste, refus du backend en 400/413/415/422 |
| `too_many_requests` | 429, quota |
| `unsupported` | image trop grosse pour être remise |
| `unavailable` | non configuré, pas d'adresse publique, backend éteint ou en erreur, réponse sans image |

- Le corps d'une erreur du backend va au journal, jamais au modèle.
- **Coût** : une ligne « requête » du modèle d'images (endpoint
  `/v1/tools/image_generation`) en plus de celle de l'outil ; un backend à
  quotas la compte. Un backend qui ne tient qu'un modèle en mémoire décharge
  celui de la conversation : deux rechargements à attendre.
- **Durée** : une réponse avec image peut dépasser 2 minutes. Sans flux, elle
  dépasse les 60 s par défaut d'un reverse proxy nginx (504). Demander le flux
  (les pings tiennent la connexion) ou relever ce délai.
- **Retouche** (`edits = true`) : la fonction prend `image_url`, téléchargée
  sous `[tools.net]`, puis envoyée à `POST /v1/images/edits` (multipart :
  `model`, `prompt`, `image`, `size` si demandée). URL publique seulement : une
  image remise par le proxy ne se retouche que si `[files].public_url` n'est
  pas privée vue du proxy. **Jamais jouée contre un vrai backend.**

## Mémoire persistante (`memory`)

Des fichiers texte sous `/memories`, **qui survivent aux conversations et aux
redémarrages**. Commandes et arguments de l'outil memory d'Anthropic (les
modèles Claude y sont entraînés, les autres suivent la description) :

| Commande | Arguments | Effet |
|---|---|---|
| `view` | `path`, `view_range` | répertoire : fichiers (taille, date) et occupation ; fichier : contenu, lignes numérotées |
| `create` | `path`, `file_text` | crée ou remplace |
| `str_replace` | `path`, `old_str`, `new_str` | remplace une occurrence **unique** |
| `insert` | `path`, `insert_line`, `insert_text` | insère après la ligne N (0 = en tête) |
| `delete` | `path` | un fichier ou un répertoire entier |
| `rename` | `old_path`, `new_path` | renomme ou déplace |

**Cloisonnée par clé** :

- Un fichier appartient à la clé qui l'a écrit (`call.client`, condensé de
  `tools.owner`) et ne se relit qu'avec elle ; le modèle ne choisit pas le
  client. La clé n'est jamais écrite ; le condensé est stable (secret dans
  `data/client.secret`). Changer de clé, c'est changer de mémoire.
- **Avec les [comptes OIDC](tableau-de-bord.md#comptes-oidc)**, c'est le
  **compte** : ses clés et le chat de sa page partagent la mémoire ; une clé
  révoquée puis remplacée ne perd rien. De même pour une clé du TOML
  rattachée par un admin (ce qu'elle avait écrit avant n'est pas déplacé).
- **Proxy ouvert** (`api_keys` vide) : une mémoire commune.
- Bornes par clé (ou compte) : nombre de fichiers, taille d'un fichier, taille
  totale. Au-delà, écriture refusée, le modèle est invité à faire de la place.
- Tout `..` est refusé.

Présentée sur `/v1/chat/completions` (`{"type": "memory"}` ou `[chat].always`),
`/v1/tools` et le [serveur MCP](#serveur-mcp--mcp). Rien sur `/v1/messages` :
l'outil memory d'Anthropic s'exécute chez le client.

**Tableau de bord** : *Configuration → Mémoire* montre les fichiers de la clé
connectée à `/ui` (lecture, édition, renommage, suppression), jamais ceux d'une
autre clé. Routes, même cookie : `GET /ui/memory`,
`GET|PUT|DELETE /ui/memory/file`, `POST /ui/memory/rename`.

> Ce qu'un modèle range ici revient dans les conversations suivantes : une page
> web lue par un agent peut lui faire écrire une consigne durable. Relisez-la
> dans *Configuration → Mémoire*.

## Mémoire des résultats

Deux mémoires gardent ce qu'ont rendu les outils. Rien pour un client
Anthropic, qui renvoie le résultat avec l'appel.

### Surface Responses

Le client rejoue `web_search_call` sans résultat ; sans cette mémoire, le
modèle perdrait ce qu'il a lu et le cache de préfixe manquerait.

- Par identifiant d'élément : nom de la fonction, arguments tels qu'écrits,
  texte du résultat. Rien d'autre.
- **En mémoire vive**, jamais sur disque. Bornes : `cache_entries` (les moins
  récemment relues sortent) et `cache_ttl`.
- **Un redémarrage la vide** : l'appel est reconstruit depuis l'action
  (requête, URL), avec un résultat disant que le contenu n'est plus disponible
  et que l'outil peut être relancé.
- **Cloisonnée par client** (clé de `proxy.api_keys`, ou compte
  [OIDC](tableau-de-bord.md#comptes-oidc)). L'identifiant (`ws_…`, 96 bits
  aléatoires) ne suffit pas : sous une autre clé, il est traité comme inconnu,
  comme après un redémarrage.
- La clé n'est gardée nulle part : un condensé salé par un aléa du démarrage,
  jamais journalisé. Proxy ouvert : tous les clients sont le même, quel que
  soit leur `Bearer`. Clé partagée, mémoire partagée.
- `cache_entries` est **commune** : un client très actif peut évincer les
  entrées d'un autre (dégradation, pas fuite).

### Échanges cachés de `/v1/chat/completions`

Mémoire séparée (`[chat].memory`), pour le
[tour suivant](#le-tour-suivant-de-la-conversation).

- Clé : condensé BLAKE2b des messages et de la réponse finale. Contenu :
  `tool_calls` hébergés, texte de leurs résultats, texte du dernier tour.
  Jamais les messages du client.
- Mêmes règles : mémoire vive, rien du contenu dans journaux et statistiques,
  cloisonnement par clé, `cache_entries` et `cache_ttl`. Les journaux disent
  combien d'échanges sont réinsérés, `/healthz` combien sont gardés.
- En plus, `[chat].memory_chars` borne le total en caractères (une entrée peut
  porter 8 résultats de 24 000 caractères).
- L'entrée évincée n'est plus réinsérée. Un redémarrage la vide.

## Ce qu'aucun garde-fou n'empêche

**Le contenu d'une page web entre dans le contexte d'un agent qui exécute des
commandes sur le poste du client.** Une page, ou un extrait de résultat, peut
porter des instructions pour le modèle : c'est l'injection de prompt. Le modèle
qui les suit lance un `shell`, modifie un fichier, envoie ailleurs ce qu'il a
lu, avec les droits que le client lui donne.

Variante sans le poste du client : la page demande d'ouvrir
`https://ailleurs/?d=<ce qu'il a en contexte>`, et `web_fetch` l'emporte. Seule
parade côté proxy : restreindre ce qu'il ouvre avec `allowed_domains` (liste
blanche) ou `blocked_domains` dans `[tools.net]`. Vides par défaut.

Les [garde-fous](#garde-fous) protègent **le réseau du proxy** et bornent des
tailles. Aucun ne filtre ce qu'une page dit, et aucun ne le peut. Activer
`web_search`, c'est faire lire du texte non fiable à l'agent. La défense est
côté client (approbation des commandes, bac à sable), et dans le choix de ne
pas l'activer là où l'agent travaille sans surveillance.

## Validation

- **07/10/2026**, proxy local vers gufo : `ocr`, `transcribe` (WAV, FLAC
  converti par ffmpeg), serveurs MCP, `[chat].always` et `always_except`,
  refus du garde-fou, `code_execution` sans exécuteur, `image_generation`
  (direct, déclaré, et en flux par `[chat].always` sur le déploiement).
- Des bancs de vrais clients, en conteneurs : voir
  [bench/README.md](../bench/README.md).

Pas encore joués : l'outil serveur `web_fetch` contre un client réel (forme
tirée de la
[documentation d'Anthropic](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-fetch-tool)
et des tests), la mémoire des échanges cachés et `[chat].always` derrière une
vraie interface de chat (tests `proxy/tests/test_chat_api.py` seulement), une attente de quota à un tour
ultérieur, un backend à quotas derrière `ocr`, `transcribe` ou les images, un
long enregistrement, MP3/Ogg, WebP/GIF, un serveur MCP à session ou
authentifié, un backend d'images qui rend une `url`, la retouche.
