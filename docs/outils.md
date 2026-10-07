# Outils hébergés : le contrat

Un outil **hébergé** est un outil que le proxy exécute lui-même, à la
place du fournisseur que le client croit avoir en face — la recherche web
qu'OpenAI ferait pour `{"type": "web_search"}`, l'outil serveur
`web_search_20250305` qu'Anthropic exécuterait. Ce que cela change pour un
client, les garde-fous, la mise en route : voir
[Outils hébergés](../README.md#outils-hébergés) dans le README. Ce fichier
décrit l'autre côté : **ce qu'est un outil pour le code du proxy**, et
comment en écrire un.

Le dépôt en porte six, `web_search`, `web_fetch`, `ocr`, `transcribe`,
`code_execution` et `image_generation` (`proxy/llm_proxy/tools/`), plus
un **fournisseur**,
`mcp.py`, qui y ajoute les outils des [serveurs MCP](#serveurs-mcp) de la
configuration. Ce que le contrat porte et qu'aucune surface ne fait
encore : voir [Prévu, pas construit](#prévu-pas-construit).

Tout tient dans `proxy/llm_proxy/tools/contract.py`, réexporté par le paquet :
`from llm_proxy import tools` puis `tools.Tool`, `tools.Result`…

## En une page

    client ──déclare──▶ surface ──spec(present)──▶ modèle
                                                      │ appelle
    client ◀──summary / bloc── surface ◀──Result── Hosted.run ──▶ outil.run(args, call)
                                  │                    │
                                  └─ texte seul ──▶ mémoire      └─▶ une ligne de statistiques

- Un outil est un **objet** : un nom, son **prompt** — ce que le modèle
  lit pour savoir quand et comment l'appeler (`prompt`, `parameters`),
  que la classe de base assemble en la fonction présentée (`spec`) —,
  une exécution (`run`), de quoi dire l'appel au client (`summary`), et
  ses **liaisons** aux protocoles, en données.
- Il rend un **`Result`** : le texte que lit le modèle, un code d'erreur
  à côté, des sources, des faits pour l'affichage, des fichiers produits.
  C'est le seul format d'échange entre un outil et le reste du proxy.
- **Rien d'autre ne connaît l'outil.** Ni les surfaces, ni `/v1/tools`,
  ni les statistiques ne relisent son texte, ne testent son nom ou ne
  devinent ce qu'il sait faire.

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

| Membre | Pour qui | Règle |
|---|---|---|
| `text` | le modèle | **Seul ce texte est mémorisé et rejoué.** Tout ce que le modèle doit savoir y est écrit, en anglais. Borné par `[tools].max_result_chars` : le surplus est coupé et marqué `[truncated]` par l'exécuteur |
| `error` | le proxy et le client | Un code de la [liste fermée](#codes-derreur), `None` pour un succès. Le texte d'une erreur commence par `Error: ` et dit au modèle quoi faire ; le code voyage **à côté**, il ne se déduit jamais du texte (une page lue peut commencer par « Error: ») |
| `sources` | le client | `Source(url, title, date, snippet)` — seule `url` est obligatoire, `date` est `AAAA-MM-JJ` ou vide. Elles font les annotations `url_citation` d'un client chat/completions et les blocs `web_search_result` d'un client Anthropic. Une erreur n'en a pas |
| `meta` | le client | Un dictionnaire de valeurs JSON : l'URL réellement lue, un titre, une plage de caractères… Rien que le modèle doive lire, rien qui soit gardé. Les clés sont à l'outil |

| `files` | le client | `Artifact(name, media_type, data)` : un fichier **produit** par l'outil — un nom sans chemin, son type MIME, ses octets. Rendu en **lien** : voir ci-dessous |

Quand l'exécuteur coupe un texte trop long, `sources`, `meta` et `files`
ne sont pas touchés : ils disent ce que l'outil a trouvé ou produit.

**Les fichiers sont rendus en liens.** Le magasin `proxy/llm_proxy/files.py`
les garde quelques heures, en mémoire vive, et les sert par
`GET <public_url>/v1/files/<jeton>/<nom>` — hors clé du proxy (un
navigateur n'en envoie pas) : le jeton, imprévisible, vaut droit d'accès.
Seules les images matricielles, reconnues à leurs octets, sont servies en
ligne ; tout le reste part en téléchargement, sous un type inerte. Deux
surfaces les rendent : `/v1/chat/completions` ajoute les liens à la
**fin** de la réponse (`Stored.markdown`, en flux comme en JSON), et
l'enveloppe de `/v1/tools` les liste. Sans `[files].public_url`, aucun
lien ne peut être écrit : rien n'est gardé, et un outil le sait d'avance
par `files.refusal(taille)`. Les surfaces Responses et Anthropic ne les
rendent pas (aucun outil lié n'en produit). Aucune **mémoire** ne les
garde : `tools.Memory` et la mémoire des échanges cachés ne retiennent
que `text`, et le tour suivant ne rend que lui au modèle. Un outil qui
produit un fichier le **nomme donc dans son texte** — son nom, jamais
son URL : c'est le proxy qui écrit le lien, le modèle n'a pas de jeton à
recopier.

Un échec se construit par `tools.failure(code, message)` —
`Result("Error: <message>", code)` — ou, depuis `run`, en levant
`tools.ToolError(code, message)`, ce qui revient au même. `message` est
une phrase en anglais, ponctuation comprise, sans le préfixe.

## L'outil : `Tool`

Une classe de base légère ; `name`, `prompt` et `run` sont à écrire —
`parameters` aussi dès que l'outil prend des arguments —, le reste a un
défaut.

| Membre | Rôle |
|---|---|
| `name` | Le nom de la fonction présentée au modèle, et celui de `POST /v1/tools/<nom>`. Unique dans le registre |
| `enabled` | Actif ? `True` par défaut ; les outils du dépôt le lisent dans `[tools.<nom>].enabled` |
| `prompt(present) -> str` | Son **[prompt](#le-prompt-dun-outil)** : le texte que le modèle lit pour savoir quand et comment l'appeler — la `description` de la fonction. En anglais. `present` est l'ensemble des noms des outils hébergés présentés **avec lui** dans cette requête, le sien compris : un prompt ne renvoie qu'à ce que le modèle peut appeler (`web_search` ne dit « Use web_fetch… » que si `web_fetch` est là, et inversement). Sans défaut : un outil qui ne l'écrit pas lève `NotImplementedError` dès qu'on le présente |
| `parameters(present) -> dict` | Le schéma JSON de ses arguments. Ses `description` **font partie du prompt** : le modèle les lit, elles s'écrivent comme lui. Un schéma neuf à chaque appel, jamais un objet partagé. Défaut : aucun argument, `{"type": "object", "properties": {}}` |
| `spec(present) -> dict` | La fonction à la forme chat/completions, `{"type": "function", "function": {name, description, parameters}}`, **assemblée** par la classe de base de `name`, `prompt(present)` et `parameters(present)`. C'est la seule fabrique de la définition envoyée au modèle (les trois surfaces) et listée par `GET /v1/tools`. Un outil ne l'écrit pas |
| `async run(args, call) -> Result` | L'exécution. `args` : les arguments du modèle, un objet JSON déjà lu et **rien de validé**. `call` : voir plus bas. Rend un `Result` ou lève `ToolError` |
| `summary(args, result=None) -> dict` | Ce que le client affiche de l'appel : un `type`, et ce qui le distingue (`{"type": "search", "query": …}`). C'est l'`action` d'un élément Responses. `result` vaut `None` pour l'appel seul — un élément rejoué dont la mémoire a perdu le résultat. Défaut : `{"type": <name>}` |
| `timeout` | Délai (s) d'**une** exécution, propre à l'outil ; `None` (le défaut) = `[tools].run_timeout`. Il **remplace** le délai commun — plus long pour une transcription, plus court si l'outil le veut. L'exécuteur l'applique |
| `max_calls` | Appels exécutés au plus pour **une** réponse, propres à l'outil et **comptés à part** : ses appels ne pèsent pas sur le plafond commun, ni ceux des autres sur le sien. `None` (le défaut) = le plafond commun, `[tools].max_calls`, que ces outils-là se partagent |
| `family` | Un mot pour le refus par limite du plafond commun : `"web"` donne « the limit of 8 web tool calls », le défaut `""` « the limit of 8 tool calls ». Un outil à `max_calls` est nommé, lui : « the limit of 3 code_execution calls » |
| `responses`, `anthropic` | Les [liaisons aux protocoles](#liaisons-aux-protocoles), ou `None` |
| `kinds` | Les types qui déclarent l'outil dans `tools` d'une requête chat/completions : ceux de sa liaison Responses, ou son nom s'il n'en a pas. Calculé |
| `render(args, sources) -> str` | Le texte du modèle **refait** depuis des sources. À écrire seulement par un outil lié au bloc Anthropic `web_search_tool_result`, dont le client rejoue les sources et non le texte |

### Ce que `run` reçoit : `Call`

Ce qui ne vient **pas** du modèle — qui ne peut donc pas s'en affranchir.

| Champ | Contenu |
|---|---|
| `settings` | Ce que le **client** a réglé sur cet outil dans sa requête. Clés connues : `allowed_domains`, `blocked_domains` (listes de domaines d'un outil serveur Anthropic), `max_chars` (taille du contenu rendu, tirée de `max_content_tokens`). Un outil ignore celles qui ne le concernent pas |
| `endpoint` | La route par où l'appel arrive (`/v1/responses`, `/v1/tools`…) |
| `model` | Le modèle préfixé de la conversation ; vide pour l'appel direct |
| `client` | Le condensé de la clé du client (`tools.owner`), vide pour un proxy ouvert — jamais la clé |
| `session` | L'identifiant de la **conversation**, fourni par la surface : de quoi retrouver un état d'un appel au suivant (le bac de `code_execution`). Seule la surface `/v1/chat/completions` en fournit un — tiré à chaque requête, puis retrouvé avec l'échange caché que la mémoire reconnaît (`[chat].memory`) : il vaut tant que le client renvoie ses réponses inchangées. `""` sur `/v1/tools`, `/v1/responses` et `/v1/messages` : un outil à état doit marcher sans — un état par appel, rien de partagé. Il ne sort jamais du proxy |

### Ce que `run` peut faire, et ce qui lui arrive

- Rendre un `Result`, succès ou échec.
- Lever `ToolError(code, message)` : rendu en `failure(code, message)`.
- Lever autre chose : c'est une panne. Le modèle lit
  `Error: <nom> failed (<Exception>).`, le code est `unavailable`, la
  trace part dans le journal. Rien ne remonte au client.
- Dépasser son délai — `Tool.timeout`, ou `[tools].run_timeout` s'il n'en
  a pas : la coroutine est annulée, code `timeout`.
- Rendre un code hors liste, ou autre chose qu'un `Result` :
  `unavailable`, avec un avertissement dans le journal.

## Le prompt d'un outil

Un modèle ne connaît d'un outil que ce que le proxy lui en fait **lire**.
Le prompt est ce qu'il lit *avant* d'appeler : c'est lui qui décide si
l'outil est appelé, quand, et avec quels arguments. C'est un membre du
contrat, en deux parties que `spec` assemble :

| Membre | Devient | Contenu |
|---|---|---|
| `prompt(present)` | la `description` de la fonction | Ce que l'outil fait, quand l'appeler, ce qu'il rend, ses bornes |
| `parameters(present)` | les `parameters` de la fonction | Le schéma des arguments ; la `description` de chacun dit quoi y mettre |

Un outil n'écrit rien d'autre : ni le nom de la fonction ailleurs que
dans `name`, ni l'enveloppe `{"type": "function", …}`. Le prompt est
calculé **à chaque requête** : il peut dépendre de `present` et des
réglages de l'outil (`image_generation` n'annonce `image_url` que si
`edits` est actif, `code_execution` n'annonce `files` que si
`max_files` > 0, `transcribe` cite les formats réellement lus). Celui
d'un outil MCP est la description et le schéma **du serveur** : un texte
tiers, que le proxy borne et n'écrit pas.

**Le relire.** `GET /v1/tools` rend, pour chaque outil actif, `name`,
`description` et `parameters` : c'est le prompt **en vigueur**, sorti de
la même fabrique (`spec`) que ce qui part au modèle, avec les réglages
du déploiement. `present` y vaut *tous les outils actifs* : une phrase
de renvoi (« Use web_fetch… ») y figure, alors qu'un modèle à qui
`web_fetch` n'est pas présenté ne la lit pas.

### Tout ce que le modèle lit

Le prompt n'est pas le seul texte. La liste est complète — elle est
aussi en tête de `contract.py` — et tout y est en anglais :

| Texte | Écrit par | Où | Quand le modèle le lit |
|---|---|---|---|
| Le prompt (`description`) | l'outil | `Tool.prompt(present)` | À chaque requête où l'outil est présenté |
| Les descriptions de paramètres | l'outil | `Tool.parameters(present)` | Idem : elles font partie du prompt |
| Le texte d'un résultat | l'outil | `Result.text` | Après l'appel, et à chaque rejeu de la conversation |
| Le texte d'une erreur | l'outil | `failure(code, message)`, `ToolError(code, message)` : `Error: <message>` | Idem |
| Limite d'appels atteinte | l'exécuteur | `Hosted._refusal` (`tools/__init__.py`) : `Error: the limit of 8 web tool calls for one answer is reached. Answer now with what you already have.` | À la place de l'exécution |
| Délai dépassé | l'exécuteur | `Hosted._execute` : `Error: <nom> timed out after 60 s.` | À la place du résultat |
| Panne de l'outil | l'exécuteur | `Hosted._execute` : `Error: <nom> failed (<Exception>).` | Idem |
| Arguments illisibles, outil inconnu | l'exécuteur | `Hosted._execute` : `Error: the tool arguments are not a JSON object.` ; `Hosted.run` : `Error: unknown tool <nom>.` | Idem |
| Troncature | l'exécuteur | `Hosted._execute` : le texte coupé à `[tools].max_result_chars`, suivi de `[truncated]` | À la fin d'un résultat trop long |
| Résultat expiré de la mémoire | l'exécuteur | `EXPIRED` (`tools/__init__.py`) : `[result no longer available: the proxy was restarted or the entry expired; run the tool again if you still need it]` | Au rejeu d'un appel dont la mémoire a perdu le résultat |

Les textes de l'exécuteur sont écrits **au nom de l'outil**, pas par lui :
ils sont les mêmes pour tous, et un outil n'y met que son `name` et sa
`family`. Le nom de la fonction et les noms des paramètres sont lus
aussi ; ce sont des identifiants, pas de la prose — les choisir parlants.

### L'écrire

Ces règles sont tirées des prompts du dépôt, et de ce qu'on a vu des
modèles en faire.

- **En anglais**, comme tout ce que le modèle lit.
- **Dire ce que l'outil fait et quand l'appeler**, dès la première
  phrase : « Search the web. », « Read the text of an image […] or of a
  scanned PDF, given its URL ». Puis ce qu'il **rend** (« Returns a
  numbered list of results (title, date, URL, snippet) ») et ce qu'il
  **ne fait pas** (« The text has no timestamps and no speaker names »,
  « There is NO network »).
- **Ne renvoyer à un autre outil que s'il est dans `present`.** « Use
  web_fetch to read a result page » n'est écrit que si `web_fetch` est
  présenté dans la même requête ; sinon le modèle appelle une fonction
  qui n'existe pas. Même raison pour laquelle un **texte d'erreur** ne
  nomme pas d'autre outil : `run` ne sait pas ce qui est présenté, et le
  texte, mémorisé, sera relu à un tour où l'outil nommé peut ne plus
  l'être.
- **Dire ce qui est remis à l'utilisateur sans que le modèle ait à
  l'écrire.** « The image is delivered to the user automatically, with
  your answer […] you must not write a link or a markdown image for
  it » ; « never write a link or a path to a delivered file ». Sans
  cela le modèle invente un lien, ou recopie un chemin du bac.
- **Être impératif là où un modèle se trompe.** Une possibilité se lit
  comme une option. Vécu le 07/10/2026 : `code_execution` disait « list
  it in `files` » ; un modèle a quand même téléchargé le fichier depuis
  le bac (échec : pas de réseau), puis l'a lu par `web_fetch` pour le
  recopier dans son programme. Le prompt dit maintenant « you MUST list
  its URL in `files` » et « Never download it in the program, and never
  paste its content into the code ». Les capitales se gardent pour ces
  endroits-là : partout, elles ne disent plus rien.
- **Chiffrer les bornes et les délais**, depuis la configuration, pas en
  dur : « A program is killed after 30 s », « 2 at most in one answer »,
  « up to 25 MB », « A PDF is read 4 pages at a time », « Up to 8,
  20.0 MB each ». Un modèle qui les connaît ne les découvre pas par une
  erreur — qui coûte un des appels de la réponse.
- **Dire comment continuer** quand le résultat est borné : « pass
  `offset` to continue from a given character position », « pass `pages`
  to read the following ones ».
- **Ne rien annoncer qui ne soit vrai ici** : un paramètre désactivé
  n'est pas décrit, un format non lu n'est pas cité.
- **Compter les tokens.** Le prompt part à **chaque** requête où l'outil
  est présenté, à chaque tour de la boucle, que l'outil serve ou non.
  Les six outils du déploiement pèsent environ 6 200 caractères de JSON
  — à peu près 1 400 tokens par requête. Une phrase s'y ajoute quand un
  modèle s'est trompé sans elle, pas avant. (Le prompt ne change pas
  d'une requête à l'autre tant que `present` et les réglages sont les
  mêmes : il reste dans le préfixe que le cache d'un backend garde.)
- **Le changer, c'est changer un comportement** : les tests de chaque
  outil tiennent les phrases qui comptent (`"NO network" in …`), et
  `GET /v1/tools` montre le résultat sur un déploiement.

## Codes d'erreur

Liste **fermée** (`tools.ERRORS`). Un outil choisit le plus précis ;
chaque surface le traduit dans son vocabulaire.

| Code | Sens | Qui le rend |
|---|---|---|
| `invalid_input` | Arguments du modèle inutilisables : champ manquant, URL mal formée, JSON qui n'est pas un objet, outil inconnu | l'outil, l'exécuteur |
| `not_allowed` | Cible refusée par une règle : liste de domaines, adresse privée | l'outil |
| `not_accessible` | Cible injoignable, introuvable ou en erreur : hôte inconnu, HTTP 4xx/5xx, trop de redirections | l'outil |
| `unsupported` | Contenu que l'outil ne sait pas rendre : type non textuel, PDF sans texte | l'outil |
| `too_many_requests` | La cible demande de ralentir (HTTP 429) | l'outil |
| `timeout` | Délai de l'exécution dépassé (`Tool.timeout`, `run_timeout`) | l'exécuteur |
| `limit` | Limite d'appels de la réponse atteinte (`[tools].max_calls`, `Tool.max_calls`, `max_uses` du client) — rien n'a été exécuté | l'exécuteur |
| `failed` | L'outil **a tourné** et rapporte lui-même un échec ; le texte dit lequel : `isError` d'un outil MCP. (Un programme sorti en erreur sous `code_execution` n'en est **pas** un : c'est un résultat, `error = None`, avec ses fichiers.) Ni les arguments (`invalid_input`) ni l'outil (`unavailable`) ne sont en cause a priori — le modèle lit, et décide | l'outil |
| `unavailable` | L'outil lui-même est en panne ou n'est pas configuré : moteur de recherche injoignable, exception | l'outil, l'exécuteur |

Ce que chaque surface en fait :

| Code | `/v1/messages` (`error_code` du bloc) | `/v1/tools` | Statistiques (issue) | `/v1/responses`, `/v1/chat/completions` |
|---|---|---|---|---|
| *succès* | bloc de résultat | `is_error: false`, `error: null` | `ok` | élément `completed` / rien |
| `invalid_input` | `invalid_tool_input` | `is_error: true`, `error` = le code | `error` | le client ne voit pas d'erreur : l'élément est `completed`, le modèle lit le texte et s'adapte |
| `not_allowed` | `url_not_allowed` | idem | `error` | idem |
| `not_accessible` | `url_not_accessible` | idem | `error` | idem |
| `unsupported` | `unsupported_content_type` | idem | `error` | idem |
| `too_many_requests` | `too_many_requests` | idem | `error` | idem |
| `timeout` | `unavailable` | idem | `error` | idem |
| `limit` | `max_uses_exceeded` | idem | `limit` | idem |
| `failed` | `unavailable` (Anthropic n'a pas de code commun pour cela ; aucun outil lié ne le rend aujourd'hui) | idem | `error` | idem |
| `unavailable` | `unavailable` | idem | `error` | idem |

La table Anthropic est `anthropic_api.ERROR_CODES`. Dans tous les cas le
**modèle** lit le texte `Error: …`, jamais le code.

## Liaisons aux protocoles

Comment un protocole **nomme** l'outil et rend compte de son appel : des
données, posées sur l'outil.

```python
class WebSearch(Tool):
    name = "web_search"
    responses = Responses(
        kinds=("web_search", "web_search_preview", "web_search_2025_08_26"),
        item="web_search_call")
    anthropic = Anthropic(prefix="web_search", block="web_search_tool_result")
```

| Liaison | Champs | Effet |
|---|---|---|
| `Responses(kinds, item)` | `kinds` : les types d'outil qui l'activent dans `tools` ; `item` : l'élément rendu au client | Sur `/v1/responses`, l'appel devient un élément `<item>` — `in_progress`, puis `completed` avec `action` = `summary(args, result)` — et les événements `response.<item>.in_progress / searching / completed`. Son texte est rangé dans la mémoire des résultats. Plusieurs outils peuvent partager un type et un élément (`web_fetch` est activé par `web_search` et rendu en `web_search_call`) : c'est le `type` de leur `summary` qui les distingue au rejeu |
| `Anthropic(prefix, block)` | `prefix` : celui du type de l'outil serveur, toutes versions datées (`web_search` pour `web_search_20250305`) ; `block` : le bloc de résultat | Sur `/v1/messages`, l'outil serveur déclaré devient la fonction ; l'appel sort en `server_tool_use` puis `<block>` (ou `<block>_error` avec le code traduit), compté dans `usage.server_tool_use.<prefix>_requests`. Le **format** du contenu de chaque bloc appartient au protocole : `anthropic_api.py` sait écrire et relire `web_search_tool_result` et `web_fetch_tool_result` (`_CONTENT`, `_REPLAY`) ; un outil lié à un bloc qu'il ne connaît pas n'est pas présenté |

**Sans liaison**, un outil reste :

- exécutable par `POST /v1/tools/<nom>` et listé par `GET /v1/tools` ;
- présentable sur `/v1/chat/completions` (`[chat].hosted_tools`) : le
  client le déclare par son nom, `{"type": "<nom>"}`, dans `tools`.
  L'appel est caché du client, comme pour les autres ; ses `sources`
  deviennent des annotations `url_citation` si le modèle les écrit. Son
  nom dans `[chat].always` le fait présenter d'office, sans déclaration.

Les surfaces Responses et Anthropic, elles, l'ignorent : elles n'auraient
rien pour rendre compte de l'appel.

## Le cycle d'un appel

1. **Présentation.** La surface reconnaît ce que le client déclare
   (`Hosted.for_responses`, `for_server`, `for_kind`), écarte un outil
   dont une fonction du client porte déjà le nom, puis présente au modèle
   `spec(present)` de chaque outil retenu, à la place de la déclaration :
   son nom, son [prompt](#le-prompt-dun-outil) et le schéma de ses
   arguments, où `present` est l'ensemble des outils retenus.
   Sur `/v1/chat/completions`, les outils nommés par `[chat].always` sont
   présentés **sans déclaration**, à la suite de ceux du client
   (`Hosted.by_name`, même règle d'homonymie), sauf à un modèle de
   `[chat].always_except` ; la liste est relue à
   chaque requête contre les outils actifs, un outil enregistré après le
   démarrage y entre donc dès qu'il existe.
2. **Exécution bornée.** Quand le modèle appelle la fonction, l'appel
   n'est pas rendu au client : la boucle (`app.hosted_loop`) passe par
   `Hosted.run`, le point unique. Dans l'ordre : nom inconnu →
   `invalid_input` ; limite d'appels atteinte → `limit`, sans exécuter ;
   arguments qui ne sont pas un objet JSON → `invalid_input` ; puis
   `run(args, call)` sous le délai de l'outil (`Tool.timeout`, sinon
   `run_timeout`), toute exception rattrapée ; enfin le texte coupé à
   `max_result_chars`. La limite d'appels se compte **par réponse** : un
   compte commun (`[tools].max_calls`) pour tous les outils qui n'ont pas
   le leur, un compte à part pour chaque outil à `Tool.max_calls`. Un
   modèle qui insiste quatre fois au-delà d'une limite voit sa réponse
   close sans lui (`app.HOSTED_EXTRA_CALLS`).
3. **Résultat.** La surface reçoit le `Result` (`resolve`) et en fait ce
   que son protocole prévoit : un élément `completed` (Responses), un
   bloc de résultat ou d'erreur (Anthropic), des annotations à la fin de
   la réponse (chat/completions).
4. **Mémoire et rejeu.** Le modèle reçoit `text` au tour suivant, et le
   recevra **à l'identique** quand le client rejouera la conversation —
   sinon le préfixe change et le cache du backend ne sert plus. Seul le
   texte est gardé : dans `tools.Memory` (Responses, le client renvoie
   l'élément sans son résultat), dans la mémoire des échanges cachés
   (chat/completions), ou nulle part (Anthropic : le client renvoie le
   bloc, d'où le texte est relu — ou refait par `render`).
5. **Statistiques.** `Hosted.run` écrit une ligne par exécution : outil,
   route, modèle, issue (`ok` / `error` / `limit`, d'après le **code**),
   durée, taille du texte. Jamais les arguments ni le résultat.

## `/v1/tools` : l'enveloppe

    GET  /v1/tools          → {"object": "list", "data": [{name, description, parameters}]}
    POST /v1/tools/<nom>    → corps : les arguments, en objet JSON

La liste est le [prompt en vigueur](#le-prompt-dun-outil) de chaque outil
actif : `description` = `prompt(present)`, `parameters` =
`parameters(present)`, `present` = tous les outils actifs.

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

`name`, `result` et `is_error` sont stables : des extensions de clients
les lisent. `error` est le [code](#codes-derreur) (`null` pour un succès),
`sources` et `meta` ceux du `Result` ; `files`, ses fichiers produits, en
**liens** — `{"name", "media_type", "size", "url"}` chacun, liste vide
sans `[files].public_url` (voir [Le résultat](#le-résultat--result)).
Toujours `200` quand l'outil
existe ; `404` `unknown_tool` sinon, `400` si le corps n'est pas un objet.

## Écrire un outil

L'outil minimal, complet — celui que joue
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


tools.register(Echo())
```

Une fois enregistré, sans une ligne de plus ailleurs :

    GET  /v1/tools                      → le liste, avec son prompt et son schéma
    POST /v1/tools/echo {"text": "é"}   → {"name": "echo", "result": "é", "is_error": false,
                                           "error": null, "sources": [], "meta": {"chars": 1},
                                           "files": []}
    POST /v1/tools/echo {}              → … "result": "Error: `text` is required.",
                                           "is_error": true, "error": "invalid_input" …
    POST /v1/chat/completions, "tools": [{"type": "echo"}]
                                        → présenté au modèle, exécuté par le proxy, appel caché

Pour un outil du dépôt :

1. Un module dans `proxy/llm_proxy/tools/`, qui importe le contrat par
   `from .contract import …` (pas par le paquet : celui-ci importe ses
   outils), lit ses réglages dans `[tools.<nom>]` (`config.flag`,
   `config.text`…) et finit par `TOOL = MonOutil()`.
2. `register(mon_outil.TOOL)` dans `proxy/llm_proxy/tools/__init__.py` — l'ordre
   du registre est celui où les fonctions sont présentées.
3. Une table `[tools.<nom>]` commentée dans `data/config.example.toml`,
   et sa section dans le README.
4. Une liaison, si un protocole a un nom pour lui.

Un fournisseur qui apporte plusieurs outils (`mcp.py`) appelle
`register` une fois par outil découvert, au démarrage puis à chaque
découverte. Le registre ne sait pas retirer : un outil disparu reste
enregistré, et c'est son `enabled` qui le dit.

Ce qu'il faut tenir :

- **Le prompt décide de l'usage.** Un outil juste dont le prompt est
  vague n'est pas appelé, ou mal : voir
  [Le prompt d'un outil](#le-prompt-dun-outil), « L'écrire ».
- **Tout ce que le modèle doit savoir est dans `text`.** `sources` et
  `meta` ne lui parviennent pas et ne sont pas gardés.
- **`args` vient du modèle** : types faux, champs en trop, URL hostiles.
  Valider, et rendre `invalid_input` avec une phrase qui dit quoi
  corriger.
- **Une cible choisie par le modèle passe par `net.download`**
  (`tools/net.py`), le téléchargement gardé commun, réglé par
  `[tools.net]` : listes de domaines (celles de la configuration, plus
  celles de `call.settings`), adresses publiques seulement, connexion
  vers l'adresse vérifiée — à chaque saut de redirection. L'outil lui
  donne **ses** bornes (`timeout`, `limit` : un nombre d'octets ou une
  fonction qui décide aux premiers octets) et ne reçoit que des
  `ToolError`, dont le texte est déjà celui du modèle, en anglais
  (« Error: 10.0.0.1 is a private or local address, which this proxy
  does not read. », « Error: host not found: … », « Error: only http(s)
  URLs are read. »). Avant de lire un cache,
  `net.check(url, call.settings)`.
- **`run` est annulable** : pas de travail bloquant dans la boucle
  asyncio (`asyncio.to_thread` pour du CPU), et rien à nettoyer ailleurs
  que dans un `finally`.
- **Un texte d'erreur parle au modèle** : ce qui s'est passé, et s'il
  doit réessayer. « Do not retry the search now » évite huit appels
  identiques.

### Les deux outils web

| | `web_search` | `web_fetch` |
|---|---|---|
| `summary` | `{"type": "search", "query"}` | `{"type": "open_page", "url"}` — l'URL suivie de la plage lue, `<url> [20000, 40000]`, pour un morceau de page |
| `sources` | une par résultat : titre, URL, date, extrait | une : l'URL telle que le modèle l'a écrite (c'est elle qu'il citera) |
| `meta` | — | `url` (réellement lue, après redirections), `title`, `content_type`, `total` (caractères de la page), `range` (`[début, fin]`, seulement pour un morceau) |
| `settings` lus | `allowed_domains`, `blocked_domains` | `allowed_domains`, `blocked_domains`, `max_chars` |
| Codes rendus | `invalid_input` (pas de `query`), `unavailable` (tout le reste : SearXNG injoignable, non configuré, moteurs bloqués) | `invalid_input`, `not_allowed`, `not_accessible`, `too_many_requests`, `unsupported` |

### `ocr` : lire le texte d'une image ou d'un PDF scanné

`proxy/llm_proxy/tools/ocr.py`, `[tools.ocr]` (désactivé par défaut). Le texte
est lu par un **modèle de vision** qu'un backend du proxy relaie déjà
(`model = "<backend>/<modèle>"`, backend avec `images = true`) : pas de
Tesseract, pas de route OCR dédiée.

| Argument | | |
|---|---|---|
| `url` | chaîne, requis | L'URL http(s) de l'image ou du PDF |
| `pages` | chaîne | PDF seulement : `"3"`, `"1-3"`, `"2,5-7"`. Défaut : les premières (`max_pages`) |

Pas d'argument de langue ni de consigne : le modèle de vision reconnaît
l'écriture seul, et une consigne libre du modèle ferait de l'outil autre
chose qu'une transcription (et un canal d'injection vers le modèle de
vision).

**Ce qui est lu.** PNG, JPEG, GIF, WebP — reconnus à leurs premiers octets,
quel que soit le type annoncé. D'un PDF : les images *embarquées* dans ses
pages, pas un rendu (pypdf ne dessine pas une page ; ni Pillow ni poppler
ne sont requis). Un scan est une image par page — ou plusieurs bandes,
envoyées ensemble. JPEG repris tel quel ; pixels bruts gris ou RVB réécrits
en PNG. **Pas lus** : les scans codés en CCITT (fax), JBIG2 ou JPEG 2000,
les images CMJN ou à palette, une page sans image (son texte, s'il y en a,
se lit par `web_fetch`). Aucune image n'est redimensionnée ni redressée
(`/Rotate` ignoré).

**Résultat.** Du texte seulement :

    URL: https://exemple.org/scan.pdf
    Content-Type: application/pdf
    Pages: 1-4 of 12 (pass pages="5-12" to continue)

    ---
    [Page 1]
    …

`meta` : `url` (celle réellement lue), `content_type`, `pages` (numéros
rendus), `total_pages` (PDF), `model`. `sources` : l'URL demandée.
`summary` : `{"type": "ocr", "url"}`, l'URL suivie des pages lues d'un
PDF (`<url> [pages 5-8]`).

**Erreurs** (codes du contrat) : `invalid_input` (URL ou `pages`
illisibles, pages hors du document), `not_allowed` (adresse privée,
domaine refusé), `not_accessible` (introuvable, HTTP ≥ 400),
`too_many_requests` (429 de la cible, quota ou 429 du modèle de vision),
`unsupported` (ni image ni PDF, trop gros, PDF chiffré ou abîmé, aucune
image lisible), `timeout` (rien de lu dans le délai), `unavailable` (outil
non configuré, modèle de vision injoignable ou en erreur).

**Bornes.** Téléchargement `max_bytes` (20 Mo), image `max_image_bytes`
(5 Mo), `max_pages` par appel (4), `max_chars` rendus (20 000), `timeout`
(50 s). Une page en échec ou pas finie à temps n'efface pas les autres :
ce qui est lu est rendu, la ligne `Pages:` dit quoi redemander. C'est
pourquoi l'outil tient **son** budget et que l'exécuteur lui laisse
davantage (`Tool.timeout` = `timeout` + 10 s, à la place de
`[tools].run_timeout`) : coupé par l'exécuteur, il ne rendrait rien.

**Coût.** Une requête chat/completions au modèle de vision par page :
elle passe par le limiteur d'un backend à quotas, et laisse une ligne de
statistiques « requête » (endpoint `/v1/tools/ocr`) en plus de celle de
l'outil. Le fichier téléchargé et chaque page lue sont gardés dans le
cache web (`[tools].web_cache_ttl`) : relire ou demander la suite ne
retélécharge ni ne relit rien.

**Garde-fou.** Le commun, `[tools.net]`, par `net.download`. Le texte lu
dans une image est du contenu non fiable, comme une page web.

**Présentation.** `POST /v1/tools/ocr` ; sur `/v1/chat/completions`,
`{"type": "ocr"}` dans `tools` ou `[chat].always`. Pas de liaison
Responses ni Anthropic. Présenté avec `web_fetch`, chacun renvoie à
l'autre par `prompt(present)` : `ocr` dit « when web_fetch reports an
image, or a PDF with no extractable text », `web_fetch` dit « For an
image, or a PDF with no extractable text (a scan), use ocr with the same
URL ». Les textes d'**erreur** de `web_fetch`, eux, ne nomment pas `ocr` :
`run` ne sait pas ce qui est présenté, et un texte mémorisé citerait un
outil qui peut ne plus l'être au tour suivant.

### `code_execution` : un outil à état, à fichiers, derrière un service

`proxy/llm_proxy/tools/code_execution.py`, `[tools.code_execution]` (désactivé
par défaut) — le modèle écrit un programme, le service `executor` du
compose (`services/executor/`, mis en route par `docker-compose.override.yml`) le
fait tourner dans un bac à sable. L'exécuteur a été validé sur le
déploiement le 07/10/2026 (`python -m executor.validate`, 0 échec) ;
l'outil y a été appelé le même jour par le proxy et par un modèle, sur
deux tours d'une conversation : voir le README, « Exécution de code ».

| | `code_execution` |
|---|---|
| Arguments | `language` (`python`, `bash`, `javascript`, `c`, `cpp`, `go`, `rust` ; `sh`, `shell`, `js`, `node`, `c++`, `golang`, `rs`… acceptés), `code` (le programme entier), `files` (facultatif : des `{"url", "name"?}` — une URL nue est acceptée — à déposer dans le dossier de travail avant le programme ; absent de la fonction si `max_files = 0`) |
| `summary` | `{"type": "code_execution", "language"}` |
| `text` | L'issue (`Exit code: N`, ou `Timed out…`), l'état du bac s'il est neuf, détruit ou d'un seul appel, les fichiers d'entrée déposés (nom dans le bac, taille, URL d'origine) et ceux qui ne l'ont pas été (URL, raison), les fichiers remis et ceux qui ne le sont pas (avec la raison), puis la sortie — en dernier, c'est elle qu'une coupe emporte. Jamais d'URL du proxy : les seules sont celles que le modèle a données dans `files` |
| `files` | Les fichiers créés ou modifiés dans `/work` par CET appel, que le magasin accepte : nom sans chemin, type lu dans les octets pour une image |
| `meta` | `exit_code`, `timed_out`, `fresh`, `files` (les chemins) ; `inputs` (les noms déposés) si `files` était demandé |
| `call` lus | `client` et `session` : la clé du bac ; `settings` (`allowed_domains`, `blocked_domains`) pour le téléchargement des fichiers d'entrée |
| `timeout`, `max_calls` | `[tools.code_execution].timeout` + 120 s, plus `download_timeout` + 45 s (télécharger et déposer les fichiers d'entrée) ; `max_calls`, compté à part |
| Codes rendus | `invalid_input` (langage, code, `files` mal formé ou trop nombreux), `too_many_requests` (tous les bacs exécutent), `unavailable` (non configuré, exécuteur injoignable, en panne, réponse illisible). Quand **aucun** des fichiers d'entrée n'a pu entrer, rien n'est exécuté et le code est celui de leur refus s'il est unique — `not_allowed` (domaine, adresse privée), `not_accessible` (injoignable, HTTP ≥ 400, trop lent), `unsupported` (trop gros, vide), `invalid_input` (URL) —, `not_accessible` s'ils diffèrent ; `unsupported` aussi quand l'exécuteur refuse le lot (HTTP 413). **Un programme sorti en erreur, ou tué par son délai, est un SUCCÈS de l'outil** : `error = None`, le code de sortie dans le texte, les fichiers rendus — ni `failed`, ni `timeout`. De même un appel dont une PARTIE des fichiers d'entrée manque : le programme tourne, le texte dit lesquels |

Ce qu'il montre du contrat :

- **L'état vit ailleurs.** L'outil ne garde rien : `call.client` et
  `call.session` nomment un bac chez l'exécuteur, qui l'expire seul. Sans
  session (`/v1/tools`), un bac par appel.
- **Le texte nomme les fichiers, le proxy écrit les liens.** Le modèle ne
  recopie pas une URL à jeton ; la surface range `Result.files`
  (`proxy/llm_proxy/files.py`) et ajoute les liens à la réponse.
- **Ne rien annoncer qu'on ne peut tenir** : un fichier que le magasin
  refuserait (`files.refusal`) est listé comme non remis.
- **Un service voisin est une adresse de configuration**, avec un jeton :
  ni le garde-fou réseau, ni le détail de ses pannes pour le modèle (il
  va au journal). Le proxy ne le sonde pas au démarrage et n'en dépend
  pas : éteint, l'outil rend `unavailable`.
- **Les langages compilés ont leur valeur de `language`**, plutôt que de
  passer par `bash` : le modèle donne son source comme pour Python,
  l'exécuteur le compile sous `/tmp` (ni source ni binaire parmi les
  fichiers produits) et l'exécute dans le délai de l'appel. La
  description dit ce que l'absence de réseau interdit — ni module Go, ni
  crate — parce que le modèle, sinon, l'essaie.

- **Les fichiers d'entrée sont téléchargés par le proxy, pas par le
  bac** — qui n'a pas de réseau, et doit le rester. `files` passe par
  `net.download` comme toute cible choisie par le modèle (`[tools.net]`,
  listes du client, chaque redirection), 4 de front, sous des bornes
  propres à l'outil (nombre, taille par fichier, taille totale, durée
  totale) ; un fichier au-delà est **refusé, jamais coupé**. Puis ils
  partent à l'exécuteur dans la même requête que le programme (`inputs`,
  en base64), qui rejuge noms et tailles et les déplie dans `/work` par
  l'entrée standard de `podman exec`.
- **Le nom d'un fichier d'entrée doit être prévisible** : le modèle écrit
  son programme dans le MÊME appel. Dans l'ordre : le `name` qu'il donne ;
  le dernier élément du chemin de l'URL qu'il a écrite, s'il porte une
  extension ; le nom de `Content-Disposition` ; sinon ce dernier élément
  (ou `file`) avec l'extension du type annoncé. Toujours assaini
  (`files.safe_name`), sans point ni tiret en tête, et dédoublonné
  (`data-2.csv`). Le texte du résultat donne le nom retenu.
- **Un échec partiel n'arrête pas l'appel.** Les fichiers entrés restent
  dans le bac de la conversation : le modèle lit ce qui manque, corrige,
  et n'a pas à les redemander. Si **aucun** n'entre, le programme — écrit
  pour eux — n'est pas lancé : il ne ferait qu'échouer, en consommant un
  des appels de la réponse, et l'erreur de l'outil est plus claire qu'un
  `FileNotFoundError`.
- **Un fichier d'entrée n'est pas un fichier produit** : déposé avant le
  repère de la récolte, il ne repart pas chez l'utilisateur — sauf si le
  programme l'a modifié (un tableur complété en place est bien ce que
  l'utilisateur attend).
- **Le contenu d'un fichier d'entrée est un texte du web.** Il ne s'exécute
  pas (c'est le programme du modèle qui le lit, dans un bac qui isole déjà
  ce programme), mais ce que le programme en imprime revient au modèle :
  une injection de prompt peut s'y trouver, comme dans une page de
  `web_fetch`. Rien à isoler de plus ; à savoir.
- **Compatibilité proxy ↔ exécuteur.** Sans `files`, la requête est celle
  d'avant, au champ près. Un exécuteur **plus ancien** ignore `inputs` et
  lance le programme sans les fichiers : le proxy le voit à l'absence de
  `inputs` dans la réponse et l'écrit au modèle (« the sandbox service is
  too old to receive files ») — au-delà de 2 Mo de corps, cet exécuteur-là
  répond 400 et l'outil rend `unavailable`. Un proxy **plus ancien**
  n'envoie pas le champ et ignore `inputs` et `rejected` de la réponse.

L'API de l'exécuteur : en tête de `services/executor/executor/server.py`.

### `transcribe` : un outil sans liaison, qui appelle un backend

`proxy/llm_proxy/tools/transcribe.py` — le modèle passe l'URL d'un fichier
audio, le proxy rend sa transcription. Présenté sur
`/v1/chat/completions` (déclaré `{"type": "transcribe"}`, ou d'office par
`[chat].always`) et exécutable par `POST /v1/tools/transcribe` ; ni
l'API Responses ni l'API Messages n'ont d'outil à lui lier.

| | `transcribe` |
|---|---|
| Arguments | `url` (obligatoire), `language` (code ISO 639-1, facultatif), `offset` |
| Texte | `URL: …`, puis `Language: …` et `Duration: m:ss` si le backend les rend, `Characters: …` pour un morceau, `---`, la transcription |
| `summary` | `{"type": "transcribe", "url"}` — l'URL suivie de la plage lue pour un morceau |
| `sources` | une : l'URL telle que le modèle l'a écrite |
| `meta` | `url` (réellement lue), `total` (caractères), `language` et `duration` (secondes) s'ils sont connus, `range` pour un morceau |
| `settings` lus | `allowed_domains`, `blocked_domains`, `max_chars` |
| `timeout` | le sien : `download_timeout` + `timeout` de `[tools.transcribe]` (360 s par défaut), plus `convert_timeout` (60 s) quand ffmpeg convertit, à la place de `run_timeout` |
| Codes rendus | `invalid_input` (URL, langue), `not_allowed`, `not_accessible`, `too_many_requests` (la cible, ou le modèle de transcription), `unsupported` (pas de l'audio, trop gros, audio que le modèle ne lit pas et qui n'est pas converti, audio que ffmpeg ne décode pas, WAV converti trop long), `timeout` (dont une conversion trop longue), `unavailable` (non configuré, backend éteint ou en erreur, ffmpeg qui ne se lance plus) |

Ce qu'il montre du contrat :

- **Un outil peut appeler un backend du proxy** sans passer par `app.py` :
  `backends.route_backend` sur le modèle de sa configuration, puis le
  client HTTP du backend. Il refait alors lui-même ce que `app.py` fait
  autour d'un relais : la porte de quota (`settings.is_exempt`, le
  limiteur du backend) et la ligne de statistiques « requête »
  (`stats.record`) — en plus de celle de l'outil, écrite par
  `Hosted.run`. Son endpoint est la route du proxy d'où la requête naît,
  `/v1/tools/transcribe` : dans l'usage du modèle de transcription, ce
  que l'outil consomme se distingue de ce que les clients consomment.
- **Un délai à lui** (`Tool.timeout`, une propriété qui lit sa
  configuration) : une transcription ne tient pas dans le délai commun.
- **Reconnaître un contenu** : les premiers octets d'abord, le
  `Content-Type` ensuite, jamais l'extension de l'URL. La borne de
  lecture passée à `net.download` est une fonction : rien n'est lu d'un
  fichier annoncé trop gros, et la lecture s'arrête aux douze premiers
  octets de ce qui n'est pas de l'audio.
- **Garder le travail, pas l'entrée** : le texte est en cache (par URL et
  langue), l'audio ne l'est jamais.
- **Ne promettre que ce qui sera lu.** `[tools.transcribe].formats` dit
  ce que le modèle de transcription lit tel quel. Le reste est converti
  en WAV 16 kHz mono par ffmpeg (`convert`, actif par défaut) quand
  ffmpeg est là — l'image Docker l'embarque — et refusé (`unsupported`)
  avant l'envoi sinon. La description de l'outil cite ce qui sera
  réellement lu : tous les formats avec la conversion, ceux de `formats`
  sans elle.
- **Un décodeur lit des octets hostiles.** ffmpeg reçoit un fichier venu
  du web : sous-processus sans shell ni entrée standard, arguments fixes
  (rien n'y vient de l'URL ni du fichier), environnement réduit à `PATH`,
  dossier temporaire à lui supprimé dans tous les cas ; format d'entrée
  **imposé** d'après les premiers octets (`-f` : pas de sondage, donc
  aucune des listes de fichiers — HLS, concat — qu'ffmpeg sait suivre) ;
  seul protocole `file` (`-protocol_whitelist`) ; première piste son
  seulement ; délai (`convert_timeout`, processus tué) et sortie bornée
  (`convert_max_bytes`, par `-fs` : au-delà le fichier est refusé, pas
  coupé) ; tué aussi si l'appel est annulé. Un audio de format inconnu
  n'est jamais converti. Ce que cela ne fait pas : isoler ffmpeg — une
  faille de décodeur s'exécute avec les droits du proxy.

Ce qu'un vrai backend rend (gufo, `qwen3-asr-1.7b`, relevé le
07/10/2026) : `response_format=json` → `{"text"}` seul ;
`verbose_json` → en plus `"language": "english"` (un **nom**, pas un
code) et `"duration": 11` — d'où le `verbose_json` demandé par défaut
(`response_format`). Le champ `language` en code ISO (`en`) est accepté.
Tout ce qui n'est pas du WAV vaut un **HTTP 500** dont le corps dit
`invalid_request_error` (« input must be a RIFF WAV ») : l'outil lit ce
type dans le corps, quel que soit le statut, et rend `unsupported` (le
fichier est en cause) plutôt qu'`unavailable` (le backend le serait).

### `image_generation` : un fichier pour seul résultat, par un backend

`proxy/llm_proxy/tools/image_generation.py`, `[tools.image_generation]`
(désactivé par défaut) — le modèle décrit une image, le modèle d'images
d'un backend la génère (`POST /v1/images/generations`), le proxy la
remet au client. Présenté sur `/v1/chat/completions` (déclaré
`{"type": "image_generation"}`, ou d'office par `[chat].always`) et
exécutable par `POST /v1/tools/image_generation`. Pas de liaison : l'API
Responses a bien un outil de ce nom, mais son élément
`image_generation_call` porte l'image en base64, et cette surface ne
rend pas de fichiers (voir [Prévu, pas construit](#prévu-pas-construit)).

| | `image_generation` |
|---|---|
| Arguments | `prompt` (obligatoire, 4 000 caractères au plus), `size` (une de `sizes` ; défaut `size`), et `image_url` seulement si `edits` |
| `text` | Une phrase : `Image generated: image-eecf50.png (image/png, 512x512, 515 kB). It is shown to the user with your answer; do not write a link or a markdown image yourself. You cannot see it: …` — `Image edited: …` pour une retouche. Jamais de base64, jamais d'URL |
| `files` | L'image, une seule : `image-<condensé>.<ext>` (un nom par image, que le modèle distingue dans une conversation), type lu dans les octets |
| `meta` | `model`, `file`, `media_type`, `size` (les dimensions réelles si elles se lisent, sinon la taille demandée), `bytes` |
| `summary` | le défaut, `{"type": "image_generation"}` |
| `timeout`, `max_calls` | `timeout` + 2 × `download_timeout` de sa table (360 s par défaut) ; `max_calls` (2), compté à part |
| Codes rendus | `invalid_input` (prompt, taille hors liste, `image_url` sans `edits`, requête refusée par le backend en 400/413/415/422), `too_many_requests` (429 ou quota du modèle d'images), `unsupported` (image trop grosse pour être remise ; image à retoucher qui n'en est pas une, ou trop grosse), `not_allowed` / `not_accessible` (l'image à retoucher), `unavailable` (non configuré, pas de `[files].public_url`, backend éteint ou en erreur, réponse sans image lisible, `url` du backend illisible) |

Ce qu'il montre du contrat :

- **Le résultat peut n'être qu'un fichier.** Le texte ne fait que le
  nommer et dire au modèle ce qu'il n'a pas à faire : la surface écrit
  le lien. C'est ce qui manquait à l'outil du même nom retiré le
  05/10/2026, qui rendait un base64 à un seul protocole.
- **Vérifier avant de dépenser.** `files.refusal(0)` est demandé avant
  la requête : sans adresse publique, l'image ne serait remise à
  personne, et la générer coûterait quand même.
- **Une adresse rendue par un backend n'est pas une adresse de
  configuration.** Le backend peut rendre l'image par `url`. De son
  origine (ou relative), elle est lue par le client du backend, avec sa
  clé, sans garde-fou : c'est l'adresse qu'on joint déjà. De toute autre
  origine, elle passe par `net.download` comme une cible du modèle —
  adresses publiques seulement — et son refus n'est pas rendu au modèle
  tel quel : le texte du garde-fou nommerait une adresse du backend.
- **Ne rien demander qu'un backend puisse refuser sans raison.** Ni
  `n`, ni `response_format` : les deux formes de réponse sont lues.
- **Un compte d'appels bas, dit dans la description** : le modèle sait
  qu'il a deux images par réponse, avant d'en demander une troisième.
- **La même fonction, un paramètre de plus.** La retouche (`edits`) est
  `image_url` sur le même outil : un téléchargement gardé, borné aux
  premiers octets d'une image, puis `POST /v1/images/edits` en
  multipart. Le paramètre n'est annoncé que si elle est active.

Ce qu'un vrai backend rend (gufo, `Qwen-Image-2.1-heretic`, relevé le
07/10/2026) : `{"created", "data": [{"b64_json"}]}` pour
`{model, prompt, size}`, un PNG aux dimensions demandées, sans `usage`
— d'où des zéros dans la ligne de statistiques de la requête. La
retouche n'y a pas été jouée.

## Serveurs MCP

`proxy/llm_proxy/tools/mcp.py` est un **client MCP** : le proxy se connecte aux
serveurs listés dans `[tools.mcp.<serveur>]`, leur demande leurs outils
(`tools/list`), et chacun devient un outil hébergé — un `Tool` du contrat
comme les autres, sans liaison de protocole : présentable sur
`/v1/chat/completions`, exécutable par `POST /v1/tools/<nom>`. Les
surfaces Responses et Anthropic ne les présentent pas : leurs formes MCP
(`{"type": "mcp", "server_url": …}`, `mcp_servers`) portent une URL de
serveur fournie par le client, ce qui est exclu ici.

**Ce qui est permis.** HTTP seulement (transport « Streamable HTTP ») ;
une liste fermée de serveurs, dans la configuration ; des en-têtes
statiques pour l'authentification (secrets par `${VAR}`). Pas de stdio,
pas d'URL fournie par un client ou par le modèle, pas d'OAuth.

**Nommage.** La fonction présentée au modèle s'appelle
`<préfixe>_<outil>` — le préfixe est le nom de la table (ou `prefix`).
Caractères hors `[A-Za-z0-9_-]` remplacés par `_`, 64 caractères au plus
(au-delà : coupé, fini par un condensé). Deux serveurs qui ont chacun un
`search` donnent `docs_search` et `wiki_search`. Un nom déjà pris écarte
l'outil arrivé en second (journal).

**Déclaration sur `/v1/chat/completions`.** `{"type": "mcp"}` déclare tous
les outils MCP, `{"type": "mcp:<serveur>"}` ceux d'un serveur,
`{"type": "<serveur>_<outil>"}` un seul. `mcp` et `mcp:<serveur>` sont
connus du proxy dès qu'un serveur est **configuré** (`mcp.kinds()`),
avant toute découverte : déclarés alors qu'aucun outil n'est encore là,
ils valent un `400` qui le dit, pas l'erreur d'un backend. `[chat].always`
présente d'office un outil MCP par son nom, `<serveur>_<outil>`, ou tous
ceux d'un serveur par `mcp:<serveur>` (`chat_api.offered` : les outils
dont c'est un `kinds`, relus à chaque requête — un outil annoncé plus
tard y entre). `mcp` nu n'y est pas admis.

**Ce que le modèle reçoit.** La description du serveur (ou son `title`),
bornée à `description_chars` ; son `inputSchema` tel quel (sans
`$schema`), s'il tient en `schema_chars`. Le proxy ne valide pas les
arguments : le serveur le fait, et son refus revient au modèle.

**Résultat.**

| Ce que le serveur rend | `Result` |
| --- | --- |
| contenus `text` | `text`, joints par une ligne vide |
| `isError: true` | `text` = `Error: <texte de l'outil>`, `error` = `failed` : l'outil a tourné et dit avoir échoué |
| `image`, `audio`, ressource binaire | une ligne `[image content omitted: image/png, 300 bytes — this proxy returns text only]` ; `meta.omitted` liste les types |
| ressource textuelle | `[resource <uri>]` puis son texte |
| `resource_link` | une ligne `[resource link: nom — uri (type)] description` ; une `Source` si l'URI est http(s) |
| `structuredContent` | en JSON dans `text` s'il n'y a aucun texte ; toujours dans `meta.structured` |
| erreur JSON-RPC `-32602` | `invalid_input` |
| autre erreur JSON-RPC, HTTP 4xx/5xx, serveur injoignable | `unavailable` |
| HTTP 429 | `too_many_requests` |
| délai dépassé (`timeout` du serveur ; c'est aussi, à 5 s près, le `Tool.timeout` de ses outils, à la place de `run_timeout`) | `timeout` |
| l'outil demande une saisie (`input_required` : élicitation, sampling) | `unsupported` |

`meta` porte aussi `server` et `tool` (le nom chez le serveur).

**Protocole.** Les deux ères de MCP sont parlées, celle du serveur étant
sondée une fois (`server/discover`) : la révision 2026-07-28, sans état
(version et capacités dans `_meta` de chaque requête, en-têtes
`MCP-Protocol-Version`, `Mcp-Method`, `Mcp-Name`, `Mcp-Param-…`), et les
révisions 2025-03-26 à 2025-11-25 (`initialize`,
`notifications/initialized`, `Mcp-Session-Id`, session rouverte une fois
sur 404). Réponses en JSON ou en flux SSE. Non pris en charge : l'ancien
transport HTTP+SSE (2024-11-05), la reprise d'un flux coupé
(`Last-Event-ID`), les flux d'écoute (`GET`, `subscriptions/listen`),
l'élicitation et le sampling, les ressources et les prompts.

**Découverte.** Au démarrage de l'application (`mcp.start()` dans le
`lifespan` d'`app.py`, attendu `startup_wait` secondes au plus ;
`mcp.stop()` à l'arrêt), puis toutes les `refresh` secondes, et sans attendre
quand le serveur signale un changement dans un flux de réponse ou refuse
un appel pour outil ou paramètres inconnus. Un serveur éteint ne bloque
pas le démarrage. Un outil que le serveur retire est désactivé ; pendant
une coupure, les outils déjà connus restent présentés (leur appel rend
`unavailable`).

**Sécurité.** Un outil exposé l'est à tous les clients du proxy, avec le
compte des en-têtes configurés, et personne ne confirme un appel : le
choix des serveurs et de leurs outils (`tools`, `exclude`) est le seul
garde-fou. Descriptions, schémas et résultats sont du texte tiers qui
entre dans le prompt du modèle.

**État.** `/healthz` → `tools.mcp` : par serveur de la configuration,
`name`, `url` (sans sa requête), `enabled`, `up` (`null` = pas encore
sondé), `error`, `era`, `protocol`, `tools` — jamais les en-têtes. Le
tableau de bord en fait une ligne du panneau « Outils ».

## Prévu, pas construit

Le contrat porte de quoi écrire un outil à fichiers, à état, lent ou
compté à part ; `code_execution` et `image_generation` s'en servent sur
`/v1/chat/completions` et `/v1/tools`. Ce qui n'est **pas** construit
derrière :

- **Fichiers et session sur les surfaces Responses et Anthropic.**
  `Result.files` n'y est rendu à personne et `Call.session` y vaut `""` :
  aucun outil lié à ces protocoles n'en produit, et il resterait à dire
  ce que chacun en fait (un élément `code_interpreter_call` ou
  `image_generation_call`, un bloc `code_execution_tool_result`) et de
  quoi dériver une conversation.
- **Un outil à `max_calls` sous un `max_uses` du client** : la limite du
  client abaisse la sienne comme elle abaisse la commune ; aucun outil
  lié à l'API Messages n'a encore de compte propre pour l'éprouver.
