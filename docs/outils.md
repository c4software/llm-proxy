# Outils hébergés : le contrat

Un outil **hébergé** est un outil que le proxy exécute lui-même, à la
place du fournisseur que le client croit avoir en face — la recherche web
qu'OpenAI ferait pour `{"type": "web_search"}`, l'outil serveur
`web_search_20250305` qu'Anthropic exécuterait. Ce que cela change pour un
client, les garde-fous, la mise en route : voir
[Outils hébergés](../README.md#outils-hébergés) dans le README. Ce fichier
décrit l'autre côté : **ce qu'est un outil pour le code du proxy**, et
comment en écrire un.

Le dépôt en porte quatre, `web_search`, `web_fetch`, `ocr` et
`transcribe` (`llm_proxy/tools/`), plus un **fournisseur**, `mcp.py`, qui
y ajoute les outils des [serveurs MCP](#serveurs-mcp) de la
configuration. Le contrat est écrit aussi pour ce qui suivra — une
exécution de code — sans le construire : voir
[Prévu, pas construit](#prévu-pas-construit).

Tout tient dans `llm_proxy/tools/contract.py`, réexporté par le paquet :
`from llm_proxy import tools` puis `tools.Tool`, `tools.Result`…

## En une page

    client ──déclare──▶ surface ──spec(present)──▶ modèle
                                                      │ appelle
    client ◀──summary / bloc── surface ◀──Result── Hosted.run ──▶ outil.run(args, call)
                                  │                    │
                                  └─ texte seul ──▶ mémoire      └─▶ une ligne de statistiques

- Un outil est un **objet** : un nom, une fonction à présenter au modèle
  (`spec`), une exécution (`run`), de quoi dire l'appel au client
  (`summary`), et ses **liaisons** aux protocoles, en données.
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

| `files` | personne encore | `Artifact(name, media_type, data)` : un fichier **produit** par l'outil — un nom sans chemin, son type MIME, ses octets. Voir ci-dessous |

Quand l'exécuteur coupe un texte trop long, `sources`, `meta` et `files`
ne sont pas touchés : ils disent ce que l'outil a trouvé ou produit.

**Les fichiers ne vont nulle part pour l'instant.** Le type existe pour
que l'exécution de code s'écrive contre lui ; rien ne le branche encore.
L'exécuteur les laisse passer tels quels, puis ils sont **ignorés** :
l'enveloppe de `/v1/tools` ne les porte pas, aucune surface ne les rend
au client, aucune mémoire ne les garde — `tools.Memory` et la mémoire des
échanges cachés ne retiennent que `text`, et le tour suivant ne rend que
lui au modèle. Un outil qui produit un fichier le **nomme donc dans son
texte** : c'est tout ce que le modèle en saura. Où vit un fichier,
combien de temps, et ce que chaque protocole en rend : c'est le chantier
de l'exécution de code, pas ce contrat.

Un échec se construit par `tools.failure(code, message)` —
`Result("Error: <message>", code)` — ou, depuis `run`, en levant
`tools.ToolError(code, message)`, ce qui revient au même. `message` est
une phrase en anglais, ponctuation comprise, sans le préfixe.

## L'outil : `Tool`

Une classe de base légère ; `name`, `spec` et `run` sont à écrire, le
reste a un défaut.

| Membre | Rôle |
|---|---|
| `name` | Le nom de la fonction présentée au modèle, et celui de `POST /v1/tools/<nom>`. Unique dans le registre |
| `enabled` | Actif ? `True` par défaut ; les outils du dépôt le lisent dans `[tools.<nom>].enabled` |
| `spec(present) -> dict` | La fonction à la forme chat/completions : `{"type": "function", "function": {name, description, parameters}}`. `present` est l'ensemble des noms des outils hébergés présentés **avec lui** dans cette requête, le sien compris : une description ne renvoie qu'à ce que le modèle peut appeler (`web_search` ne dit « Use web_fetch… » que si `web_fetch` est là, et inversement) |
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
| `session` | L'identifiant de la **conversation**, fourni par la surface : de quoi retrouver un état d'un appel au suivant (un conteneur d'exécution). **Vide partout pour l'instant** — aucune des trois API n'identifie une conversation, et aucune surface n'en fabrique encore un. Un outil à état doit donc marcher avec `""` : un état par appel, rien de partagé |

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
| `failed` | L'outil **a tourné** et rapporte lui-même un échec ; le texte dit lequel : `isError` d'un outil MCP, un programme sorti en erreur. Ni les arguments (`invalid_input`) ni l'outil (`unavailable`) ne sont en cause a priori — le modèle lit, et décide | l'outil |
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
   `spec(present)` de chaque outil retenu, à la place de la déclaration.
   Sur `/v1/chat/completions`, les outils nommés par `[chat].always` sont
   présentés **sans déclaration**, à la suite de ceux du client
   (`Hosted.by_name`, même règle d'homonymie) ; la liste est relue à
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

```json
{
  "name": "web_fetch",
  "result": "URL: https://example.org/notes\nTitle: Notes\n…",
  "is_error": false,
  "error": null,
  "sources": [{"url": "https://example.org/notes", "title": "https://example.org/notes", "date": "", "snippet": ""}],
  "meta": {"url": "https://example.org/notes", "title": "Notes", "content_type": "text/html", "total": 63, "range": [0, 30]}
}
```

`name`, `result` et `is_error` sont stables : des extensions de clients
les lisent. `error` est le [code](#codes-derreur) (`null` pour un succès),
`sources` et `meta` ceux du `Result` (ses `files` n'y sont pas : voir
[Le résultat](#le-résultat--result)). Toujours `200` quand l'outil
existe ; `404` `unknown_tool` sinon, `400` si le corps n'est pas un objet.

## Écrire un outil

L'outil minimal, complet — celui que joue
`tests/test_tools.py::test_contrat_outil_minimal_sans_liaison`
(`tests/fakes.py`) :

```python
from llm_proxy import tools


class Echo(tools.Tool):
    """L'outil MINIMAL du contrat — celui de docs/outils.md, « Écrire un
    outil » : un nom, une fonction présentée au modèle, une exécution.
    Sans liaison de protocole."""
    name = "echo"

    def spec(self, present):
        return {"type": "function", "function": {
            "name": self.name,
            "description": "Return the given text, unchanged.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string",
                                        "description": "The text to return."}},
                "required": ["text"]}}}

    async def run(self, args, call):
        text = args.get("text")
        if not isinstance(text, str) or not text:
            raise tools.ToolError("invalid_input", "`text` is required.")
        return tools.Result(text, meta={"chars": len(text)})


tools.register(Echo())
```

Une fois enregistré, sans une ligne de plus ailleurs :

    GET  /v1/tools                      → le liste, avec sa description et son schéma
    POST /v1/tools/echo {"text": "é"}   → {"name": "echo", "result": "é", "is_error": false,
                                           "error": null, "sources": [], "meta": {"chars": 1}}
    POST /v1/tools/echo {}              → … "result": "Error: `text` is required.",
                                           "is_error": true, "error": "invalid_input" …
    POST /v1/chat/completions, "tools": [{"type": "echo"}]
                                        → présenté au modèle, exécuté par le proxy, appel caché

Pour un outil du dépôt :

1. Un module dans `llm_proxy/tools/`, qui importe le contrat par
   `from .contract import …` (pas par le paquet : celui-ci importe ses
   outils), lit ses réglages dans `[tools.<nom>]` (`config.flag`,
   `config.text`…) et finit par `TOOL = MonOutil()`.
2. `register(mon_outil.TOOL)` dans `llm_proxy/tools/__init__.py` — l'ordre
   du registre est celui où les fonctions sont présentées.
3. Une table `[tools.<nom>]` commentée dans `data/config.example.toml`,
   et sa section dans le README.
4. Une liaison, si un protocole a un nom pour lui.

Un fournisseur qui apporte plusieurs outils (`mcp.py`) appelle
`register` une fois par outil découvert, au démarrage puis à chaque
découverte. Le registre ne sait pas retirer : un outil disparu reste
enregistré, et c'est son `enabled` qui le dit.

Ce qu'il faut tenir :

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
  `ToolError`. Avant de lire un cache, `net.check(url, call.settings)`.
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

`llm_proxy/tools/ocr.py`, `[tools.ocr]` (désactivé par défaut). Le texte
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
l'autre par `spec(present)` : `ocr` dit « when web_fetch reports an
image, or a PDF with no extractable text », `web_fetch` dit « For an
image, or a PDF with no extractable text (a scan), use ocr with the same
URL ». Les textes d'**erreur** de `web_fetch`, eux, ne nomment pas `ocr` :
`run` ne sait pas ce qui est présenté, et un texte mémorisé citerait un
outil qui peut ne plus l'être au tour suivant.

### `transcribe` : un outil sans liaison, qui appelle un backend

`llm_proxy/tools/transcribe.py` — le modèle passe l'URL d'un fichier
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
| `timeout` | le sien : `download_timeout` + `timeout` de `[tools.transcribe]` (360 s par défaut), à la place de `run_timeout` |
| Codes rendus | `invalid_input` (URL, langue), `not_allowed`, `not_accessible`, `too_many_requests` (la cible, ou le modèle de transcription), `unsupported` (pas de l'audio, trop gros, audio que le modèle ne lit pas), `timeout`, `unavailable` (non configuré, backend éteint ou en erreur) |

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
- **Ne promettre que ce que le backend sait faire.** Le proxy ne
  convertit pas l'audio (aucun décodeur ici). `[tools.transcribe].formats`
  dit ce que le modèle de transcription lit : la description de l'outil
  ne cite que ces formats, et un autre est refusé (`unsupported`) avant
  d'être envoyé.

Ce qu'un vrai backend rend (gufo, `qwen3-asr-1.7b`, relevé le
07/10/2026) : `response_format=json` → `{"text"}` seul ;
`verbose_json` → en plus `"language": "english"` (un **nom**, pas un
code) et `"duration": 11` — d'où le `verbose_json` demandé par défaut
(`response_format`). Le champ `language` en code ISO (`en`) est accepté.
Tout ce qui n'est pas du WAV vaut un **HTTP 500** dont le corps dit
`invalid_request_error` (« input must be a RIFF WAV ») : l'outil lit ce
type dans le corps, quel que soit le statut, et rend `unsupported` (le
fichier est en cause) plutôt qu'`unavailable` (le backend le serait).

## Serveurs MCP

`llm_proxy/tools/mcp.py` est un **client MCP** : le proxy se connecte aux
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
présente un outil MCP d'office par son nom, `<serveur>_<outil>` — il n'y a
pas de forme pour « tous ceux d'un serveur » dans cette liste.

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
compté à part. Ce qui n'est **pas** construit derrière :

- **Les fichiers produits ne sont rendus à personne.** `Result.files`
  existe ; aucune surface ne le rend, aucune mémoire ne le garde (voir
  [Le résultat](#le-résultat--result)). Reste à dire où vit un fichier,
  combien de temps, et ce que chaque protocole en fait.
- **Aucune surface ne fournit de `session`.** `Call.session` vaut `""`
  partout : aucune des trois API n'identifie une conversation, et il
  reste à décider de quoi le proxy en dérive un identifiant stable.
- **Un outil à `max_calls` sous un `max_uses` du client** : la limite du
  client abaisse la sienne comme elle abaisse la commune ; aucun outil
  lié à l'API Messages n'a encore de compte propre pour l'éprouver.
