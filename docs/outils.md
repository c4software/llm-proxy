# Outils hébergés : le contrat

Un outil **hébergé** est un outil que le proxy exécute lui-même, à la place
du fournisseur : la recherche web qu'OpenAI ferait pour
`{"type": "web_search"}`, l'outil serveur `web_search_20250305` d'Anthropic.
Cette page dit ce qu'est un outil pour le code du proxy, et comment en
écrire un. Le point de vue du client (usage, garde-fous, mise en route) est
dans [Outils hébergés](outils-heberges.md).

Le code vit dans `proxy/llm_proxy/tools/` :

- huit outils : `web_search`, `web_fetch`, `web_grep`, `ocr`, `transcribe`,
  `code_execution`, `image_generation`, `memory` ;
- un **fournisseur**, `mcp.py`, qui ajoute les outils des
  [serveurs MCP](#serveurs-mcp) configurés ;
- le contrat, `contract.py`, réexporté par le paquet :
  `from llm_proxy import tools`, puis `tools.Tool`, `tools.Result`,
  `tools.ToolName`…

Ce que le contrat prévoit sans qu'aucune surface le fasse encore :
[Prévu, pas construit](#prévu-pas-construit).

## En une page

```text
client ──déclare──▶ surface ──spec(present)──▶ modèle
                                                  │ appelle
client ◀──summary / bloc── surface ◀──Result── Hosted.run ──▶ outil.run(args, call)
                              │                    │
                              └─ texte seul ──▶ mémoire      └─▶ une ligne de statistiques
```

- Un outil est un **objet** : un nom, un **prompt** (`prompt`,
  `parameters`, assemblés en la fonction présentée par `spec`), une
  exécution (`run`), un résumé de l'appel pour le client (`summary`), et
  ses **liaisons** aux protocoles, en données.
- Il rend un **`Result`** : le texte du modèle, un code d'erreur à côté,
  des sources, des faits pour l'affichage, des fichiers. C'est le seul
  format d'échange avec le reste du proxy.
- **Rien d'autre ne connaît l'outil.** Ni les surfaces, ni `/v1/tools`, ni
  les statistiques ne relisent son texte, ne testent son nom ou ne devinent
  ce qu'il sait faire.

## Le résultat : `Result`

```python
@dataclass(frozen=True)
class Result:
    text: str                           # ce que le modèle lit
    error: str | None = None            # un code de la liste, None = succès
    sources: tuple[Source, ...] = ()    # les pages citées
    meta: Mapping = {}                  # des faits pour l'affichage du client
    files: tuple[Artifact, ...] = ()    # des fichiers produits
```

| Membre | Pour | Règle |
|---|---|---|
| `text` | le modèle | **Seul ce texte est mémorisé et rejoué.** Tout ce que le modèle doit savoir y est, en anglais. Borné par `[tools].max_result_chars` : l'exécuteur coupe et ajoute `[truncated]` |
| `error` | proxy, client | Un code de la [liste fermée](#codes-derreur), `None` = succès. Le texte d'une erreur commence par `Error: ` et dit au modèle quoi faire. Le code voyage **à côté** : il ne se déduit jamais du texte (une page lue peut commencer par « Error: ») |
| `sources` | le client | `Source(url, title, date, snippet)`. Seule `url` est requise ; `date` est `AAAA-MM-JJ` ou vide. Deviennent les annotations `url_citation` (chat/completions) et les blocs `web_search_result` (Anthropic). Pas de sources sur une erreur |
| `meta` | le client | Valeurs JSON pour l'affichage : URL réellement lue, titre, plage de caractères… Ni lu par le modèle, ni gardé. Clés propres à l'outil |
| `files` | le client | `Artifact(name, media_type, data)` : un fichier **produit**. Nom sans chemin, type MIME, octets. Rendu en lien (ci-dessous) |

Une troncature ne touche ni `sources`, ni `meta`, ni `files`.

**Un échec** se construit par `tools.failure(code, message)`, qui rend
`Result("Error: <message>", code)`, ou en levant
`tools.ToolError(code, message)` depuis `run`. C'est équivalent. `message`
est une phrase en anglais, ponctuée, sans le préfixe.

### Les fichiers produits

- Le magasin `proxy/llm_proxy/storage/files.py` les garde quelques heures, en
  mémoire vive.
- Il les sert par `GET <public_url>/v1/files/<jeton>/<nom>`, sans clé du
  proxy (un navigateur n'en envoie pas). Le jeton, imprévisible, vaut droit
  d'accès.
- Seules les images matricielles, reconnues à leurs octets, sont servies en
  ligne. Le reste part en téléchargement, sous un type inerte.
- `/v1/chat/completions` ajoute les liens en **fin** de réponse
  (`Stored.markdown`), en flux comme en JSON. L'enveloppe de `/v1/tools`
  les liste. Responses et Anthropic ne les rendent pas : aucun outil lié
  n'en produit.
- Sans `[files].public_url`, aucun lien possible, donc rien n'est gardé.
  Un outil le sait d'avance par `files.refusal(taille)`.
- Aucune mémoire ne les garde : `tools.Memory` et la mémoire des échanges
  cachés ne retiennent que `text`.

Un outil qui produit un fichier le **nomme donc dans son texte** : son nom,
jamais son URL. Le proxy écrit le lien ; le modèle n'a pas de jeton à
recopier.

## L'outil : `Tool`

À écrire : `name`, `prompt`, `run`, et `parameters` si l'outil prend des
arguments. Le reste a un défaut.

| Membre | Rôle |
|---|---|
| `name` | Nom de la fonction présentée au modèle et de `POST /v1/tools/<nom>`. Unique dans le registre |
| `enabled` | `True` par défaut. Les outils du dépôt le lisent dans `[tools.<nom>].enabled` |
| `prompt(present) -> str` | Le **[prompt](#le-prompt-dun-outil)**, en anglais : la `description` de la fonction. `present` = les noms des outils hébergés présentés **avec lui** dans la requête, le sien compris. Sans défaut : `NotImplementedError` dès qu'on le présente |
| `parameters(present) -> dict` | Schéma JSON des arguments. Ses `description` **font partie du prompt**. Un schéma neuf à chaque appel, jamais partagé. Défaut : `{"type": "object", "properties": {}}` |
| `spec(present) -> dict` | `{"type": "function", "function": {name, description, parameters}}`, **assemblée** par la classe de base. Seule fabrique de la définition envoyée au modèle (trois surfaces) et listée par `GET /v1/tools`. Un outil ne l'écrit pas |
| `async run(args, call) -> Result` | L'exécution. `args` : objet JSON du modèle, déjà lu, **rien de validé**. `call` : voir [`Call`](#ce-que-run-reçoit--call). Rend un `Result` ou lève `ToolError` |
| `summary(args, result=None) -> dict` | Ce que le client affiche : un `type` et ce qui distingue l'appel (`{"type": "search", "query": …}`). C'est l'`action` d'un élément Responses. `result=None` pour un élément rejoué dont la mémoire a perdu le résultat. Défaut : `{"type": <name>}` |
| `timeout` | Délai (s) d'**une** exécution. **Remplace** `[tools].run_timeout`, plus long ou plus court. `None` (défaut) = `run_timeout` |
| `max_calls` | Plafond d'appels par réponse, **compté à part** du plafond commun, dans les deux sens. `None` (défaut) = le plafond commun `[tools].max_calls`, partagé |
| `family` | Mot du refus par plafond commun : `"web"` → « the limit of 8 web tool calls », `""` (défaut) → « the limit of 8 tool calls ». Un outil à `max_calls` est nommé : « the limit of 3 code_execution calls » |
| `responses`, `anthropic` | Les [liaisons](#liaisons-aux-protocoles), ou `None` |
| `kinds` | Calculé : les types qui le déclarent dans `tools` en chat/completions. Ceux de sa liaison Responses, sinon son nom |
| `render(args, sources) -> str` | Refait le texte depuis des sources. Seulement pour un outil lié au bloc Anthropic `web_search_tool_result`, dont le client rejoue les sources et non le texte |
| `extra` | Les membres publics qu'un outil **fourni** ajoute à l'interface (l'état d'un outil MCP : `server`, `schema`…). Vide pour un outil natif |

### Les noms : `ToolName`

Les outils **natifs** sont nommés par une liste fermée, `ToolName` (une
`StrEnum`) : `WEB_SEARCH`, `WEB_FETCH`, `OCR`, `WEB_GREP`, `TRANSCRIBE`,
`CODE_EXECUTION`, `IMAGE_GENERATION`, `MEMORY`. Le reste du proxy les
nomme par elle (`ToolName.MEMORY`), jamais par une chaîne. Un membre vaut
sa chaîne partout : en JSON, en SQLite, en comparaison.

Les outils **fournis** (ceux des serveurs MCP) n'y sont pas : leurs noms
se découvrent. Ils ne peuvent pas prendre un nom natif.

### L'interface est fermée

`register(tool, provider="")` vérifie l'outil avant de l'inscrire
(`tools.faults`) et refuse :

- un objet qui n'hérite pas de `Tool` ;
- un **membre public que `Tool` ne déclare pas**, comme `sumary` au lieu
  de `summary`. Une aide de l'outil est `_privée`, ou une fonction du
  module ; un outil fourni déclare son état dans `extra` ;
- un `prompt` ou un `run` non écrit ;
- un nom natif hors de `ToolName`, ou un outil fourni qui prend un nom
  natif ; un nom déjà pris ;
- une liaison `responses` ou `anthropic` qui n'est pas un `Responses`
  (avec ses `kinds`) ou un `Anthropic`.

Au démarrage, chaque entrée de `ToolName` doit avoir son outil inscrit.
Une dérive arrête donc le proxy au lieu de passer en silence ; pytest la
joue (`test_le_registre_refuse_un_outil_hors_contrat`).

### Ce que `run` reçoit : `Call`

Ce qui ne vient **pas** du modèle, qui ne peut donc pas s'en affranchir.

| Champ | Contenu |
|---|---|
| `settings` | Réglages du **client** sur cet outil. Clés connues : `allowed_domains`, `blocked_domains` (outil serveur Anthropic), `max_chars` (tiré de `max_content_tokens`). Un outil ignore les autres |
| `endpoint` | La route d'arrivée (`/v1/responses`, `/v1/tools`…) |
| `model` | Le modèle préfixé ; vide pour l'appel direct |
| `client` | Condensé de la clé (`tools.owner`), jamais la clé ; vide pour un proxy ouvert. **Stable** entre démarrages (secret dans `data/client.secret`) : `memory` y attache sa mémoire persistante |
| `session` | Identifiant de la **conversation**, pour garder un état entre appels (le bac de `code_execution`). Seule `/v1/chat/completions` en fournit un : tiré à chaque requête, retrouvé avec l'échange caché que la mémoire reconnaît (`[chat].memory`), valable tant que le client renvoie ses réponses inchangées. `""` ailleurs : un outil à état doit marcher sans (un état par appel). Ne sort jamais du proxy |

### Ce que `run` peut faire, et ce qui lui arrive

| `run`… | Effet |
|---|---|
| rend un `Result` | tel quel |
| lève `ToolError(code, message)` | `failure(code, message)` |
| lève une autre exception | panne : `Error: <nom> failed (<Exception>).`, code `unavailable`, trace au journal, rien au client |
| dépasse son délai | coroutine annulée, code `timeout` |
| rend un code hors liste, ou pas un `Result` | `unavailable`, avertissement au journal |

## Le prompt d'un outil

Un modèle ne connaît d'un outil que ce qu'il en **lit** avant d'appeler.
Le prompt décide si l'outil est appelé, quand, et avec quels arguments.

| Membre | Devient | Contenu |
|---|---|---|
| `prompt(present)` | `description` | Ce que l'outil fait, quand l'appeler, ce qu'il rend, ses bornes |
| `parameters(present)` | `parameters` | Le schéma ; la `description` de chaque argument dit quoi y mettre |

L'outil n'écrit rien d'autre : ni le nom hors de `name`, ni l'enveloppe
`{"type": "function", …}`. Le prompt est calculé **à chaque requête**, selon
`present` et les réglages : `image_generation` n'annonce `image_url` que si
`edits` est actif, `code_execution` n'annonce `files` que si
`max_files` > 0, `transcribe` cite les formats réellement lus. Celui d'un
outil MCP vient **du serveur** : un texte tiers, borné par le proxy.

**Le relire** : `GET /v1/tools` rend le prompt **en vigueur** de chaque
outil actif, sorti de `spec` avec les réglages du déploiement. Mais
`present` y vaut *tous les outils actifs* : un renvoi (« Use web_fetch… »)
y figure même s'il manque au modèle qui n'a pas `web_fetch`.

### Tout ce que le modèle lit

Liste complète, aussi en tête de `contract.py`. Tout est en anglais.

| Texte | Écrit par | Où | Lu |
|---|---|---|---|
| Prompt (`description`) | l'outil | `Tool.prompt(present)` | À chaque requête où l'outil est présenté |
| Descriptions de paramètres | l'outil | `Tool.parameters(present)` | Idem |
| Texte d'un résultat | l'outil | `Result.text` | Après l'appel, et à chaque rejeu |
| Texte d'une erreur | l'outil | `failure`, `ToolError` : `Error: <message>` | Idem |
| Limite d'appels | l'exécuteur | `Hosted._refusal` (`tools/__init__.py`) : `Error: the limit of 8 web tool calls for one answer is reached. Answer now with what you already have.` | À la place de l'exécution |
| Délai dépassé | l'exécuteur | `Hosted._execute` : `Error: <nom> timed out after 60 s.` | À la place du résultat |
| Panne | l'exécuteur | `Hosted._execute` : `Error: <nom> failed (<Exception>).` | Idem |
| Arguments illisibles, outil inconnu | l'exécuteur | `Hosted._execute` : `Error: the tool arguments are not a JSON object.` ; `Hosted.run` : `Error: unknown tool <nom>.` | Idem |
| Troncature | l'exécuteur | `Hosted._execute` : coupe à `[tools].max_result_chars`, puis `[truncated]` | Fin d'un résultat trop long |
| Résultat expiré | l'exécuteur | `EXPIRED` (`tools/__init__.py`) : `[result no longer available: the proxy was restarted or the entry expired; run the tool again if you still need it]` | Rejeu d'un appel dont la mémoire a perdu le résultat |

Les textes de l'exécuteur sont les mêmes pour tous ; l'outil n'y apporte
que `name` et `family`. Les noms de la fonction et des paramètres sont lus
aussi : choisissez-les parlants.

### L'écrire

Règles tirées des prompts du dépôt et du comportement observé des modèles.

- **En anglais.**
- **Première phrase : ce que l'outil fait et quand l'appeler.** « Search
  the web. », « Read the text of an image […] or of a scanned PDF, given
  its URL ». Puis ce qu'il **rend** (« Returns a numbered list of results
  (title, date, URL, snippet) ») et ce qu'il **ne fait pas** (« The text
  has no timestamps and no speaker names », « There is NO network »).
- **Ne renvoyer à un autre outil que s'il est dans `present`.** Sinon le
  modèle appelle une fonction absente.
- **Un texte d'erreur ne nomme aucun autre outil.** `run` ignore ce qui est
  présenté, et le texte, mémorisé, sera relu à un tour où l'outil nommé
  peut manquer.
- **Dire ce qui est remis sans que le modèle l'écrive.** « The image is
  delivered to the user automatically, with your answer […] you must not
  write a link or a markdown image for it » ; « never write a link or a
  path to a delivered file ». Sinon il invente un lien ou recopie un
  chemin du bac.
- **Être impératif là où un modèle se trompe.** Une possibilité se lit
  comme une option. Avec « list it in `files` », un modèle a téléchargé le
  fichier depuis le bac (sans réseau), puis l'a lu par `web_fetch` pour le
  recopier dans son code. Le prompt dit désormais « you MUST list its URL
  in `files` » et « Never download it in the program, and never paste its
  content into the code ». Les capitales ne servent qu'à ces endroits.
- **Chiffrer bornes et délais**, depuis la configuration : « A program is
  killed after 30 s », « 2 at most in one answer », « up to 25 MB », « A
  PDF is read 4 pages at a time ». Sinon le modèle les découvre par une
  erreur, qui coûte un appel.
- **Dire comment continuer** un résultat borné : « pass `offset` to
  continue from a given character position », « pass `pages` to read the
  following ones ».
- **N'annoncer que ce qui est vrai ici** : ni paramètre désactivé, ni
  format non lu.
- **Compter les tokens.** Le prompt part à chaque requête et chaque tour,
  que l'outil serve ou non : ~6 200 caractères de JSON (~1 400 tokens)
  pour les six outils du déploiement. N'ajoutez une phrase qu'après une
  erreur de modèle. Stable à `present` et réglages égaux, il reste dans le
  préfixe en cache du backend.
- **Le changer change un comportement.** Les tests de chaque outil tiennent
  les phrases clés (`"NO network" in …`) ; `GET /v1/tools` montre le
  résultat.

## Codes d'erreur

Liste **fermée** (`tools.ERRORS`). L'outil choisit le plus précis ; chaque
surface le traduit.

| Code | Sens | Rendu par |
|---|---|---|
| `invalid_input` | Arguments inutilisables : champ manquant, URL mal formée, JSON non objet, outil inconnu | outil, exécuteur |
| `not_allowed` | Cible refusée par une règle : liste de domaines, adresse privée | outil |
| `not_accessible` | Cible injoignable, introuvable ou en erreur : hôte inconnu, HTTP 4xx/5xx, trop de redirections | outil |
| `unsupported` | Contenu non rendu : type non textuel, PDF sans texte | outil |
| `too_many_requests` | La cible demande de ralentir (HTTP 429) | outil |
| `timeout` | Délai dépassé (`Tool.timeout`, `run_timeout`) | exécuteur |
| `limit` | Limite d'appels de la réponse (`[tools].max_calls`, `Tool.max_calls`, `max_uses` du client). Rien n'a été exécuté | exécuteur |
| `failed` | L'outil **a tourné** et rapporte un échec, décrit par le texte (`isError` MCP). Ni arguments ni outil en cause a priori : le modèle décide | outil |
| `unavailable` | Outil en panne ou non configuré : moteur injoignable, exception | outil, exécuteur |

Un programme sorti en erreur sous `code_execution` n'est **pas** un
`failed` : c'est un résultat (`error = None`), avec ses fichiers.

| Code | `/v1/messages` (`error_code`) | `/v1/tools` | Statistiques |
|---|---|---|---|
| *succès* | bloc de résultat | `is_error: false`, `error: null` | `ok` |
| `invalid_input` | `invalid_tool_input` | `is_error: true`, `error` = le code | `error` |
| `not_allowed` | `url_not_allowed` | idem | `error` |
| `not_accessible` | `url_not_accessible` | idem | `error` |
| `unsupported` | `unsupported_content_type` | idem | `error` |
| `too_many_requests` | `too_many_requests` | idem | `error` |
| `timeout` | `unavailable` | idem | `error` |
| `limit` | `max_uses_exceeded` | idem | `limit` |
| `failed` | `unavailable` (pas de code Anthropic commun ; aucun outil lié ne le rend) | idem | `error` |
| `unavailable` | `unavailable` | idem | `error` |

Sur `/v1/responses` et `/v1/chat/completions`, le client ne voit jamais
d'erreur : l'élément est `completed` (Responses) ou rien n'apparaît
(chat/completions), et le modèle s'adapte au texte. La table Anthropic est
`anthropic_api.ERROR_CODES`. Partout, le **modèle** lit `Error: …`, jamais
le code.

## Liaisons aux protocoles

Comment un protocole **nomme** l'outil et rend compte de l'appel. Des
données, posées sur l'outil :

```python
class WebSearch(Tool):
    name = "web_search"
    responses = Responses(
        kinds=("web_search", "web_search_preview", "web_search_2025_08_26"),
        item="web_search_call")
    anthropic = Anthropic(prefix="web_search", block="web_search_tool_result")
```

**`Responses(kinds, item)`**, sur `/v1/responses` :

- `kinds` : les types qui l'activent dans `tools` ; `item` : l'élément
  rendu, `in_progress` puis `completed` avec `action` = `summary(args,
  result)`. Événements `response.<item>.in_progress / searching /
  completed`. Le texte va dans la mémoire des résultats.
- Plusieurs outils peuvent partager type et élément (`web_fetch`, activé
  par `web_search`, rendu en `web_search_call`). Au rejeu, le `type` de
  leur `summary` les distingue.

**`Anthropic(prefix, block)`**, sur `/v1/messages` :

- `prefix` : celui du type de l'outil serveur, toutes versions datées
  (`web_search` pour `web_search_20250305`) ; `block` : le bloc de
  résultat.
- L'outil serveur déclaré devient la fonction. L'appel sort en
  `server_tool_use` puis `<block>` (ou `<block>_error`, code traduit),
  compté dans `usage.server_tool_use.<prefix>_requests`.
- Le **format** de chaque bloc appartient au protocole : `anthropic_api.py`
  sait écrire et relire `web_search_tool_result` et `web_fetch_tool_result`
  (`_CONTENT`, `_REPLAY`). Un outil lié à un bloc inconnu n'est pas
  présenté.

**Sans liaison**, un outil reste exécutable et listé par `/v1/tools`, et
présentable sur `/v1/chat/completions` (`[chat].hosted_tools`), déclaré
par `{"type": "<nom>"}` ou par `[chat].always`. L'appel y est caché ; ses
`sources` deviennent des `url_citation` si le modèle les écrit. Responses
et Anthropic l'ignorent : rien pour rendre compte de l'appel.

## Le cycle d'un appel

1. **Présentation.** La surface reconnaît les déclarations du client
   (`Hosted.for_responses`, `for_server`, `for_kind`), écarte un outil
   homonyme d'une fonction du client, et présente `spec(present)` de chaque
   outil retenu à la place de la déclaration (`present` = les outils
   retenus). Sur `/v1/chat/completions`, les outils de `[chat].always`
   s'ajoutent sans déclaration (`Hosted.by_name`, même règle d'homonymie),
   sauf pour un modèle de `[chat].always_except`. Cette liste est relue à
   chaque requête : un outil enregistré après le démarrage y entre dès
   qu'il existe.
2. **Exécution bornée.** L'appel n'est pas rendu au client. La boucle
   (`app.hosted_loop`) passe par `Hosted.run`, point unique, dans cet
   ordre :
   1. nom inconnu → `invalid_input` ;
   2. limite atteinte → `limit`, sans exécuter ;
   3. arguments non objet JSON → `invalid_input` ;
   4. `run(args, call)` sous `Tool.timeout` (sinon `run_timeout`), toute
      exception rattrapée ;
   5. coupe à `max_result_chars`.

   Limite **par réponse** : un compte commun (`[tools].max_calls`), un
   compte à part par outil à `Tool.max_calls`. Quatre appels insistants
   au-delà d'une limite closent la réponse (`app.HOSTED_EXTRA_CALLS`).
3. **Résultat.** La surface reçoit le `Result` (`resolve`) : élément
   `completed` (Responses), bloc de résultat ou d'erreur (Anthropic),
   annotations en fin de réponse (chat/completions).
4. **Mémoire et rejeu.** Le modèle reçoit `text` au tour suivant, et
   **à l'identique** à chaque rejeu, sinon le préfixe change et le cache du
   backend ne sert plus. Seul le texte est gardé : dans `tools.Memory`
   (Responses, dont le client renvoie l'élément sans résultat), dans la
   mémoire des échanges cachés (chat/completions), ou nulle part
   (Anthropic : le client renvoie le bloc, d'où le texte est relu ou refait
   par `render`).
5. **Statistiques.** Une ligne par exécution : outil, route, modèle, issue
   (`ok` / `error` / `limit`, d'après le **code**), durée, taille du texte.
   Jamais les arguments ni le résultat.

## `/v1/tools` : l'enveloppe

```text
GET  /v1/tools          → {"object": "list", "data": [{name, description, parameters}]}
POST /v1/tools/<nom>    → corps : les arguments, en objet JSON
```

La liste donne le [prompt en vigueur](#le-prompt-dun-outil) de chaque outil
actif, avec `present` = tous les outils actifs. Réponse d'un `POST` :

```json
{
  "name": "web_fetch",
  "result": "URL: https://example.org/notes\nTitle: Notes\n…",
  "is_error": false,
  "error": null,
  "sources": [{"url": "https://example.org/notes", "title": "https://example.org/notes", "date": "", "snippet": ""}],
  "meta": {"url": "https://example.org/notes", "title": "Notes", "content_type": "text/html", "total": 63, "range": [0, 30]},
  "files": []
}
```

- `name`, `result`, `is_error` sont stables : des extensions de clients les
  lisent.
- `error` : le [code](#codes-derreur), `null` = succès. `sources`, `meta` :
  ceux du `Result`.
- `files` : des liens `{"name", "media_type", "size", "url"}` ; vide sans
  `[files].public_url` (voir [Les fichiers produits](#les-fichiers-produits)).
- `200` dès que l'outil existe ; `404` `unknown_tool` sinon ; `400` si le
  corps n'est pas un objet.

## Écrire un outil

L'outil minimal, complet, joué par
`proxy/tests/test_tools.py::test_contrat_outil_minimal_sans_liaison`
(`proxy/tests/fakes.py`) :

```python
from llm_proxy import tools


class Echo(tools.Tool):
    """L'outil MINIMAL du contrat — celui de docs/outils.md, « Écrire un
    outil » : un nom, un prompt, le schéma de ses arguments, une
    exécution. Sans liaison de protocole."""
    name = "echo"

    def prompt(self, present):
        return "Return the given text, unchanged."

    def parameters(self, present):
        return {
            "type": "object",
            "properties": {"text": {"type": "string",
                                    "description": "The text to return."}},
            "required": ["text"]}

    async def run(self, args, call):
        text = args.get("text")
        if not isinstance(text, str) or not text:
            raise tools.ToolError("invalid_input", "`text` is required.")
        return tools.Result(text, meta={"chars": len(text)})


tools.register(Echo(), provider="exemple")
```

`echo` n'est pas dans `ToolName` : hors du dépôt, l'outil s'inscrit comme
**fourni** (`provider`). Un outil du dépôt est natif (voir plus bas).

Une fois enregistré, sans une ligne de plus ailleurs :

```text
GET  /v1/tools                      → le liste, avec son prompt et son schéma
POST /v1/tools/echo {"text": "é"}   → {"name": "echo", "result": "é", "is_error": false,
                                       "error": null, "sources": [], "meta": {"chars": 1},
                                       "files": []}
POST /v1/tools/echo {}              → … "result": "Error: `text` is required.",
                                       "is_error": true, "error": "invalid_input" …
POST /v1/chat/completions, "tools": [{"type": "echo"}]
                                    → présenté au modèle, exécuté par le proxy, appel caché
```

Pour un outil du dépôt :

1. Son nom dans `ToolName` (`contract.py`).
2. Un module dans `proxy/llm_proxy/tools/`. Il importe le contrat par
   `from .contract import …`, pas par le paquet (qui importe ses outils),
   pose `NAME = ToolName.MON_OUTIL` et `name = NAME`, lit `[tools.<nom>]`
   (`config.flag`, `config.text`…) et finit par `TOOL = MonOutil()`.
3. `register(mon_outil.TOOL)` dans `proxy/llm_proxy/tools/__init__.py`.
   L'ordre du registre est l'ordre de présentation.
4. Une table `[tools.<nom>]` commentée dans `data/config.example.toml`, et
   sa section dans `docs/outils-heberges.md` (ou `docs/configuration.md`).
5. Une liaison, si un protocole a un nom pour lui.

Un fournisseur (`mcp.py`) appelle `register(tool, provider="mcp")` par outil découvert, au
démarrage puis à chaque découverte. Le registre ne sait pas retirer : un
outil disparu reste enregistré, avec `enabled` faux.

Ce qu'il faut tenir :

- **Le prompt décide de l'usage.** Prompt vague, outil pas ou mal appelé :
  voir [L'écrire](#lécrire).
- **Tout ce que le modèle doit savoir est dans `text`.** `sources` et
  `meta` ne lui parviennent pas et ne sont pas gardés.
- **`args` vient du modèle** : types faux, champs en trop, URL hostiles.
  Validez ; rendez `invalid_input` avec une phrase qui dit quoi corriger.
- **Une cible choisie par le modèle passe par `net.download`**
  (`tools/net.py`, réglé par `[tools.net]`). À chaque redirection : listes
  de domaines (configuration et `call.settings`), adresses publiques
  seulement, connexion vers l'adresse vérifiée. L'outil donne **ses**
  bornes : `timeout` et `limit` (octets, ou fonction qui décide aux
  premiers octets). Il ne reçoit que des `ToolError` au texte déjà prêt
  pour le modèle (« Error: 10.0.0.1 is a private or local address, which
  this proxy does not read. », « Error: host not found: … », « Error: only
  http(s) URLs are read. »). Avant de lire un cache :
  `net.check(url, call.settings)`.
- **`run` est annulable** : rien de bloquant dans la boucle asyncio
  (`asyncio.to_thread` pour du CPU), nettoyage dans un `finally`.
- **Une erreur parle au modèle** : ce qui s'est passé, et s'il doit
  réessayer. « Do not retry the search now » évite huit appels identiques.

### Les trois outils web

| | `web_search` | `web_fetch` | `web_grep` |
|---|---|---|---|
| `summary` | `{"type": "search", "query"}` | `{"type": "open_page", "url"}` ; pour un morceau, `<url> [20000, 40000]` | `{"type": "find_in_page", "url", "pattern"}` |
| `sources` | une par résultat : titre, URL, date, extrait | une : l'URL écrite par le modèle (celle qu'il citera) | comme `web_fetch` |
| `meta` | — | `url` (lue, après redirections), `title`, `content_type`, `total` (caractères), `range` (`[début, fin]`, pour un morceau) | `url`, `title`, `content_type`, `total`, `matches` (lignes trouvées) |
| `settings` | `allowed_domains`, `blocked_domains` | idem, plus `max_chars` | `allowed_domains`, `blocked_domains` |
| Codes | `invalid_input` (pas de `query`), `unavailable` (le reste : SearXNG injoignable, non configuré, moteurs bloqués) | `invalid_input`, `not_allowed`, `not_accessible`, `too_many_requests`, `unsupported` | ceux de `web_fetch`, dont il reprend la lecture (`web_fetch.load`, `page_text`) ; `invalid_input` (motif absent ou invalide) ; `failed` (motif coupé à son délai : `regex`, `regex_timeout`) |

`web_grep` est lié comme `web_fetch` côté Responses, mais pas à l'API
Messages : Anthropic n'a pas d'outil serveur de recherche dans une page.

#### Les URL refusées (`[tools.net].blocked_urls`)

`blocked_domains` refuse des domaines. `blocked_urls` refuse des **URL**,
décrites par des expressions régulières sur l'URL entière (insensibles à
la casse), chacune avec un `message` facultatif rendu au modèle. Le
contrôle a lieu dans `net.check` : à chaque redirection, pour tout outil
qui télécharge (`web_fetch`, `web_grep`, `ocr`, `transcribe`),
avant toute requête. Une expression invalide est écartée et signalée au
journal.

L'exemple livré vise un cas réel : **Qwen 3.8 Flash Next** appelle de
mémoire un hôte de stockage d'Alibaba (`routify…file-proxy….aliyuncs.com/
proxy_temp_file/…`), en parallèle d'appels utiles, et reçoit toujours un
403. Son message (« URL morte, ne pas réessayer ») arrête ses réessais ;
d'après ceux qui l'ont mesuré, une consigne dans le prompt n'y fait rien
([qwen-block-proxy-artifacts](https://github.com/yope/qwen-block-proxy-artifacts)).

#### Piste non retenue : le rendu des pages en JavaScript

Certaines pages n'affichent leur contenu qu'après leur JavaScript
(applications d'une seule page), et `web_fetch` les lit vides. Un rendu par
un vrai navigateur a été essayé le 18/10/2026 : une option `render: true`
de `web_fetch` et `web_grep`, servie par un Chromium headless
([browserless](https://docs.browserless.io)) dans un conteneur à part. Il
marchait, mais il a été **retiré** :

- **Trop lourd pour le besoin** : une image de 2,5 Go, un service de plus,
  environ 400 Mo de RAM par rendu, pour des pages rencontrées rarement.
- **Un risque réseau mal maîtrisé** : un navigateur ne passe pas par
  `net.py`. Ses sous-requêtes peuvent viser le réseau local. On ne pouvait
  que l'atténuer (refus des IP privées et des noms locaux), sauf à faire
  sortir le navigateur par un proxy filtrant (Squid), ce qui ajoutait
  encore un conteneur.
- **Les grandes API ne le font pas non plus.** Le `web_fetch` d'Anthropic
  dit *ne pas gérer les sites rendus dynamiquement en JavaScript*, et
  renvoie vers un outil de navigateur à part. Celui d'OpenAI (`open_page`
  de `web_search`) ne parle que de contenu en direct ou en cache.

Le code est dans l'historique git (commits `8bed50a` et `2166f54`) si le
besoin revient. Pour une page qui en a vraiment besoin, l'utilisateur peut
en coller le texte ou une capture dans le chat.

### `ocr` : lire le texte d'une image ou d'un PDF scanné

`tools/ocr.py`, `[tools.ocr]`, désactivé par défaut. Le texte est lu par un
**modèle de vision** déjà relayé par un backend (`model =
"<backend>/<modèle>"`, backend à `images = true`). Ni Tesseract, ni route
OCR dédiée.

- **Arguments** : `url` (requis, http(s)) ; `pages` (PDF : `"3"`, `"1-3"`,
  `"2,5-7"` ; défaut, les `max_pages` premières). Ni langue ni consigne :
  le modèle de vision reconnaît l'écriture seul, et une consigne libre en
  ferait autre chose qu'une transcription, et un canal d'injection.
- **Lu** : PNG, JPEG, GIF, WebP, reconnus aux premiers octets. D'un PDF,
  les images *embarquées* (pypdf ; ni Pillow ni poppler), pas un rendu :
  une image par page, ou des bandes envoyées ensemble. JPEG tel quel ;
  gris ou RVB bruts réécrits en PNG. Ni redimensionnement ni redressement
  (`/Rotate` ignoré). **Pas lu** : CCITT, JBIG2, JPEG 2000, CMJN, palette,
  page sans image (son texte se lit par `web_fetch`).
- **Résultat** : du texte seulement.

  ```text
  URL: https://exemple.org/scan.pdf
  Content-Type: application/pdf
  Pages: 1-4 of 12 (pass pages="5-12" to continue)

  ---
  [Page 1]
  …
  ```

  `meta` : `url` (lue), `content_type`, `pages` (rendues), `total_pages`
  (PDF), `model`. `sources` : l'URL demandée. `summary` :
  `{"type": "ocr", "url"}`, avec les pages d'un PDF (`<url> [pages 5-8]`).
- **Erreurs** : `invalid_input` (URL ou `pages` illisibles, hors du
  document) ; `not_allowed` (adresse privée, domaine refusé) ;
  `not_accessible` (introuvable, HTTP ≥ 400) ; `too_many_requests` (429 de
  la cible, quota ou 429 du modèle de vision) ; `unsupported` (ni image ni
  PDF, trop gros, PDF chiffré ou abîmé, aucune image lisible) ; `timeout`
  (rien de lu à temps) ; `unavailable` (non configuré, modèle de vision
  injoignable ou en erreur).
- **Bornes** : voir [`[tools.ocr]`](configuration.md#toolsocr). Une page
  en échec ou en retard n'efface pas les autres : ce qui est lu est rendu,
  et `Pages:` dit quoi redemander. L'outil tient donc **son** budget
  (`timeout`), et `Tool.timeout` = `timeout` + 10 s : coupé par
  l'exécuteur, il ne rendrait rien.
- **Coût** : une requête chat/completions au modèle de vision par page,
  sous le limiteur du backend, avec sa ligne de statistiques « requête »
  (endpoint `/v1/tools/ocr`). Fichier et pages vont au cache web
  (`[tools].web_cache_ttl`).
- **Garde-fou** : `net.download`. Le texte lu est non fiable, comme une
  page web.
- **Présentation** : `POST /v1/tools/ocr` ; `{"type": "ocr"}` ou
  `[chat].always` sur chat/completions. Sans liaison. Présentés ensemble,
  `ocr` et `web_fetch` se renvoient l'un à l'autre par `prompt(present)`
  (« when web_fetch reports an image, or a PDF with no extractable text » ;
  « For an image, or a PDF with no extractable text (a scan), use ocr with
  the same URL »). Leurs textes d'erreur, non.

### `code_execution` : un outil à état, à fichiers, derrière un service

`tools/code_execution.py`, `[tools.code_execution]`, désactivé par défaut.
Le service `executor` (`services/executor/`, mis en route par
`docker-compose.override.yml`) fait tourner le programme du modèle dans un
bac à sable. Déploiement, bornes et fichiers d'entrée côté exécuteur :
[Exécution de code](execution-de-code.md).

| | `code_execution` |
|---|---|
| Arguments | `language` (annoncés : `python`, `bash`, `javascript`, `php`, plus `go` et `rust` si installables ; alias acceptés : `py`, `sh`, `shell`, `js`, `node`, `golang`, `rs`…) ; `code` (le programme entier) ; `files` (facultatif, des `{"url", "name"?}` ou URL nues, déposés avant le programme ; absent si `max_files = 0`) |
| `summary` | `{"type": "code_execution", "language"}` |
| `text` | L'issue (`Exit code: N` ou `Timed out…`), l'état du bac s'il est neuf, détruit ou d'un seul appel, les fichiers d'entrée déposés (nom, taille, URL) et refusés (URL, raison), les fichiers remis ou non (raison), puis la sortie, en dernier car la coupe l'emporte. Aucune URL du proxy : seulement celles données dans `files` |
| `files` | Les fichiers créés ou modifiés dans `/work` par CET appel, acceptés par le magasin. Type lu dans les octets pour une image |
| `meta` | `exit_code`, `timed_out`, `fresh`, `files` (chemins) ; `inputs` (noms déposés) si `files` |
| `call` lus | `client`, `session` (la clé du bac) ; `settings` (domaines) pour les fichiers d'entrée |
| `timeout` | `[tools.code_execution].timeout` + 120 s, plus `download_timeout` + 45 s |
| `max_calls` | compté à part |

**Codes** : `invalid_input` (langage, code, `files` mal formé ou trop
nombreux) ; `too_many_requests` (tous les bacs exécutent) ; `unsupported`
(lot refusé par l'exécuteur, HTTP 413) ; `unavailable` (non configuré,
exécuteur injoignable, en panne, réponse illisible). Si **aucun** fichier
d'entrée n'entre, rien n'est exécuté : code du refus s'il est unique
(`not_allowed`, `not_accessible`, `unsupported`, `invalid_input`),
`not_accessible` s'ils diffèrent.

**Un programme sorti en erreur ou tué par son délai est un SUCCÈS de
l'outil** : `error = None`, code de sortie dans le texte, fichiers rendus.
De même si une partie seulement des fichiers d'entrée manque.

Ce qu'il montre du contrat :

- **L'état vit ailleurs.** `call.client` et `call.session` nomment un bac
  chez l'exécuteur, qui l'expire seul. Sans session (`/v1/tools`) : un bac
  par appel.
- **Le texte nomme les fichiers, le proxy écrit les liens.** Un fichier que
  le magasin refuserait (`files.refusal`) est listé comme non remis.
- **Un service voisin est une adresse de configuration**, avec un jeton :
  hors garde-fou réseau, pannes détaillées au journal et non au modèle.
  Pas sondé au démarrage : éteint, l'outil rend `unavailable`.
- **Les langages compilés ont leur `language`.** L'exécuteur compile le
  source sous `/tmp` (ni source ni binaire parmi les fichiers produits) et
  l'exécute dans le délai. La description dit ce que l'absence de réseau
  interdit (ni module Go, ni crate), sinon le modèle l'essaie.
- **Les fichiers d'entrée sont téléchargés par le proxy**, jamais par le bac
  (sans réseau) : `net.download`, 4 de front, bornes propres (nombre,
  taille par fichier et totale, durée totale), refus au-delà, jamais de
  coupe. Ils partent avec le programme (`inputs`, base64) ; l'exécuteur
  rejuge noms et tailles. Leur nom est prévisible (le modèle écrit son
  programme dans le même appel), assaini par `files.safe_name` et
  dédoublonné ; un échec partiel n'arrête pas l'appel ; un fichier
  d'entrée ne repart que si le programme l'a modifié. Détail :
  [Fichiers d'entrée](execution-de-code.md#fichiers-dentrée-files).
- **Leur contenu vient du web** : ce que le programme en imprime peut
  porter une injection de prompt, comme une page de `web_fetch`.
- **Versions.** Sans `files`, la requête ne change pas. Un exécuteur plus
  ancien ignore `inputs` ; le proxy le voit à la réponse et le dit au
  modèle. Au-delà de 2 Mo de corps, il répond 400 : `unavailable`. Un
  proxy plus ancien ignore `inputs` et `rejected` de la réponse.

L'API de l'exécuteur : en tête de `services/executor/executor/server.py`.

### `transcribe` : un outil sans liaison, qui appelle un backend

`tools/transcribe.py`. Le modèle passe l'URL d'un audio, le proxy rend sa
transcription. Sur `/v1/chat/completions` (`{"type": "transcribe"}` ou
`[chat].always`) et `POST /v1/tools/transcribe` ; Responses et Messages
n'ont pas d'outil à lui lier.

| | `transcribe` |
|---|---|
| Arguments | `url` (requis), `language` (ISO 639-1, facultatif), `offset` |
| Texte | `URL: …`, puis `Language: …` et `Duration: m:ss` si connus, `Characters: …` pour un morceau, `---`, la transcription |
| `summary` | `{"type": "transcribe", "url"}`, plus la plage pour un morceau |
| `sources` | une : l'URL écrite par le modèle |
| `meta` | `url` (lue), `total` (caractères), `language` et `duration` (s) si connus, `range` pour un morceau |
| `settings` | `allowed_domains`, `blocked_domains`, `max_chars` |
| `timeout` | `download_timeout` + `timeout` de `[tools.transcribe]` (360 s par défaut), plus `convert_timeout` (60 s) si ffmpeg convertit |
| Codes | `invalid_input` (URL, langue) ; `not_allowed` ; `not_accessible` ; `too_many_requests` (cible ou modèle) ; `unsupported` (pas de l'audio, trop gros, format non lu et non converti, indécodable par ffmpeg, WAV converti trop long) ; `timeout` (dont conversion) ; `unavailable` (non configuré, backend éteint ou en erreur, ffmpeg qui ne se lance plus) |

Ce qu'il montre du contrat :

- **Un outil peut appeler un backend** sans le relais (`relaying.py`), par
  `backends.route_backend` puis le client HTTP du backend. Il refait alors
  la porte de quota (`settings.is_exempt`, limiteur) et la ligne de
  statistiques « requête » (`stats.record`), d'endpoint
  `/v1/tools/transcribe` : sa consommation se distingue de celle des
  clients.
- **Un délai à lui** (`Tool.timeout`, propriété lue dans sa configuration).
- **Reconnaître un contenu** aux premiers octets, puis au `Content-Type`,
  jamais à l'extension. La borne de `net.download` est une fonction : rien
  n'est lu d'un fichier annoncé trop gros, et la lecture s'arrête aux douze
  premiers octets d'un non-audio.
- **Garder le travail, pas l'entrée** : le texte en cache (URL et langue),
  jamais l'audio.
- **Ne promettre que ce qui sera lu.** `formats` liste ce que le modèle lit
  tel quel. Le reste est converti en WAV 16 kHz mono par ffmpeg (`convert`,
  actif par défaut ; l'image Docker l'embarque), sinon refusé
  (`unsupported`) avant envoi. La description cite les formats réellement
  lus.
- **Un décodeur lit des octets hostiles.** ffmpeg tourne en sous-processus
  sans shell ni entrée standard, arguments fixes, environnement réduit à
  `PATH`, dossier temporaire supprimé dans tous les cas. Format d'entrée
  **imposé** par les premiers octets (`-f`, donc aucune liste HLS ou
  concat suivie), protocole `file` seul (`-protocol_whitelist`), première
  piste son seule. Délai `convert_timeout`, sortie bornée par
  `convert_max_bytes` (`-fs` : au-delà, refusé, pas coupé), processus tué
  aussi à l'annulation. Format inconnu : jamais converti. ffmpeg n'est
  **pas isolé** : une faille de décodeur s'exécute avec les droits du
  proxy.

Un vrai backend (gufo, `qwen3-asr-1.7b`) ne rend langue (un **nom** :
`"english"`) et durée qu'en `verbose_json`, d'où ce `response_format` par
défaut ; il accepte `language` en code ISO. Tout non-WAV y donne un
**HTTP 500** au corps `invalid_request_error` (« input must be a RIFF
WAV ») : l'outil lit ce type quel que soit le statut, et rend
`unsupported` (le fichier est en cause), pas `unavailable`.

### `image_generation` : un fichier pour seul résultat, par un backend

`tools/image_generation.py`, `[tools.image_generation]`, désactivé par
défaut. Le modèle d'images d'un backend génère l'image
(`POST /v1/images/generations`), le proxy la remet. Sur
`/v1/chat/completions` (`{"type": "image_generation"}` ou `[chat].always`)
et `POST /v1/tools/image_generation`. Mise en route et bornes :
[Génération d'images](outils-heberges.md#génération-dimages).

Pas de liaison : l'élément Responses `image_generation_call` porte l'image
en base64, et cette surface ne rend pas de fichiers (voir
[Prévu, pas construit](#prévu-pas-construit)).

| | `image_generation` |
|---|---|
| Arguments | `prompt` (requis, 4 000 caractères au plus), `size` (une de `sizes` ; défaut `size`), `image_url` si `edits` |
| `text` | `Image generated: image-eecf50.png (image/png, 512x512, 515 kB). It is shown to the user with your answer; do not write a link or a markdown image yourself. You cannot see it: …` (`Image edited: …` pour une retouche). Ni base64 ni URL |
| `files` | Une image, `image-<condensé>.<ext>` (un nom distinct par image), type lu dans les octets |
| `meta` | `model`, `file`, `media_type`, `size` (dimensions réelles si lisibles, sinon demandées), `bytes` |
| `summary` | le défaut, `{"type": "image_generation"}` |
| `timeout` | `timeout` + 2 × `download_timeout` (360 s par défaut) |
| `max_calls` | 2, compté à part |
| Codes | `invalid_input` (prompt, taille hors liste, `image_url` sans `edits`, refus du backend en 400/413/415/422) ; `too_many_requests` (429, quota) ; `unsupported` (image trop grosse pour être remise ; image à retoucher invalide ou trop grosse) ; `not_allowed` / `not_accessible` (image à retoucher) ; `unavailable` (non configuré, pas de `[files].public_url`, backend éteint ou en erreur, pas d'image lisible, `url` du backend illisible) |

Ce qu'il montre du contrat :

- **Le résultat peut n'être qu'un fichier.** Le texte le nomme et dit ce
  que le modèle n'a pas à faire ; la surface écrit le lien.
- **Vérifier avant de dépenser** : `files.refusal(0)` avant la requête.
  Sans adresse publique, l'image ne serait remise à personne.
- **Une adresse rendue par un backend n'est pas une adresse de
  configuration.** Une `url` de son origine (ou relative) est lue avec la
  clé du backend, sans garde-fou. D'une autre origine, elle passe par
  `net.download` (adresses publiques seulement), et son refus n'est pas
  rendu tel quel : il nommerait une adresse du backend.
- **Ne rien demander qu'un backend puisse refuser** : ni `n`, ni
  `response_format`. Les deux formes de réponse sont lues.
- **Un plafond bas, dit dans la description** : le modèle sait qu'il a deux
  images par réponse.
- **La même fonction, un paramètre de plus.** La retouche (`edits`) est
  `image_url` : téléchargement gardé, borné aux premiers octets d'une
  image, puis `POST /v1/images/edits` en multipart. Annoncé seulement si
  actif.

Un vrai backend (gufo, `Qwen-Image-2.1-heretic`) rend `b64_json`, sans
`usage` : zéros dans la ligne de statistiques. La retouche n'y a pas été
essayée.

## Serveurs MCP

`tools/mcp.py` est un **client MCP**. Le proxy demande leurs outils
(`tools/list`) aux serveurs de `[tools.mcp.<serveur>]`. Chacun devient un
`Tool` sans liaison : présentable sur `/v1/chat/completions`, exécutable
par `POST /v1/tools/<nom>`. Responses et Anthropic ne les présentent pas :
leurs formes MCP (`{"type": "mcp", "server_url": …}`, `mcp_servers`)
portent une URL fournie par le client, exclue ici.

L'inverse existe : le proxy **serveur MCP** (`mcp_server.py`, `POST /mcp`,
`[mcp_server]`) offre ses outils actifs, ceux-ci compris, par `Tool.spec`
et `Hosted.run` (route `/mcp`, `call.client` = la clé). Un `Result` y
devient un contenu `text`, ses fichiers des `resource_link`, un échec
`isError`. Le client de `mcp.py` s'y connecte (`test_mcp_server.py`).
Détails : [Serveur MCP](outils-heberges.md#serveur-mcp--mcp).

**Permis** : HTTP seulement (« Streamable HTTP »), liste fermée de serveurs
en configuration, en-têtes statiques d'authentification (secrets par
`${VAR}`). Ni stdio, ni URL venue d'un client ou du modèle, ni OAuth.

**Nommage** : `<préfixe>_<outil>`, le préfixe étant le nom de la table (ou
`prefix`). Hors `[A-Za-z0-9_-]` → `_`. 64 caractères au plus, sinon coupé
et fini par un condensé. Deux `search` donnent `docs_search` et
`wiki_search`. Un nom déjà pris écarte le second venu (journal).

**Déclaration sur `/v1/chat/completions`** :

| `type` | Déclare |
|---|---|
| `mcp` | tous les outils MCP |
| `mcp:<serveur>` | ceux d'un serveur |
| `<serveur>_<outil>` | un seul |

`mcp` et `mcp:<serveur>` sont connus dès qu'un serveur est **configuré**
(`mcp.kinds()`) : déclarés avant toute découverte, ils valent un `400`
explicite, pas l'erreur d'un backend. `[chat].always` accepte
`<serveur>_<outil>` et `mcp:<serveur>`, pas `mcp` nu (`chat_api.offered`,
relu à chaque requête : un outil annoncé plus tard y entre).

**Reçu par le modèle** : la description (ou `title`), bornée à
`description_chars`, et l'`inputSchema` (sans `$schema`) s'il tient en
`schema_chars` ([réglages](configuration.md#toolsmcp-et-toolsmcpserveur)). Le proxy ne valide pas les arguments : le serveur le
fait, et son refus revient au modèle.

**Résultat.**

| Le serveur rend | `Result` |
| --- | --- |
| contenus `text` | `text`, joints par une ligne vide |
| `isError: true` | `text` = `Error: <texte de l'outil>`, `error` = `failed` |
| `image`, `audio`, ressource binaire | `[image content omitted: image/png, 300 bytes — this proxy returns text only]` ; `meta.omitted` liste les types |
| ressource textuelle | `[resource <uri>]` puis son texte |
| `resource_link` | `[resource link: nom — uri (type)] description` ; une `Source` si l'URI est http(s) |
| `structuredContent` | en JSON dans `text` s'il n'y a aucun texte ; toujours dans `meta.structured` |
| erreur JSON-RPC `-32602` | `invalid_input` |
| autre erreur JSON-RPC, HTTP 4xx/5xx, injoignable | `unavailable` |
| HTTP 429 | `too_many_requests` |
| délai `timeout` du serveur dépassé (à 5 s près, c'est aussi le `Tool.timeout` de ses outils) | `timeout` |
| saisie demandée (`input_required` : élicitation, sampling) | `unsupported` |

`meta` porte aussi `server` et `tool` (nom chez le serveur).

**Protocole.** L'ère du serveur est sondée une fois (`server/discover`) :

- 2026-07-28, sans état : version et capacités dans `_meta` ; en-têtes
  `MCP-Protocol-Version`, `Mcp-Method`, `Mcp-Name`, `Mcp-Param-…` ;
- 2025-03-26 à 2025-11-25 : `initialize`, `notifications/initialized`,
  `Mcp-Session-Id`, session rouverte une fois sur 404.

Réponses JSON ou SSE. Non pris en charge : transport HTTP+SSE
(2024-11-05), reprise de flux (`Last-Event-ID`), flux d'écoute (`GET`,
`subscriptions/listen`), élicitation, sampling, ressources, prompts.

**Découverte** : au démarrage (`mcp.start()` dans le `lifespan` de
`startup.py`, attendu `startup_wait` s au plus ; `mcp.stop()` à l'arrêt),
toutes les `refresh` s, et aussitôt quand un serveur signale un changement
dans un flux ou refuse un appel pour outil ou paramètres inconnus. Un
serveur éteint ne bloque pas le démarrage. Un outil retiré est désactivé ;
pendant une coupure, les outils connus restent présentés et rendent
`unavailable`.

**Sécurité** : un outil exposé l'est à tous les clients, avec le compte des
en-têtes configurés, sans confirmation. Le choix des serveurs et outils
(`tools`, `exclude`) est le seul garde-fou. Descriptions, schémas et
résultats sont du texte tiers dans le prompt.

**État** : `/healthz` → `tools.mcp`, par serveur : `name`, `url` (sans
requête), `enabled`, `up` (`null` = pas encore sondé), `error`, `era`,
`protocol`, `tools`. Jamais les en-têtes. Le tableau de bord en fait une
ligne du panneau « Outils ».

## Prévu, pas construit

Le contrat permet un outil à fichiers, à état, lent ou compté à part.
`code_execution` et `image_generation` s'en servent sur
`/v1/chat/completions` et `/v1/tools`. Pas construit :

- **Fichiers et session sur Responses et Anthropic.** `Result.files` n'y
  est rendu à personne, `Call.session` y vaut `""`. Aucun outil lié n'en
  produit. Resterait à définir ce que chaque protocole en fait (élément
  `code_interpreter_call` ou `image_generation_call`, bloc
  `code_execution_tool_result`), et de quoi dériver une conversation.
- **Un outil à `max_calls` sous un `max_uses` du client.** La limite du
  client abaisse la sienne comme la commune. Aucun outil lié à l'API
  Messages n'a de compte propre pour l'éprouver.
