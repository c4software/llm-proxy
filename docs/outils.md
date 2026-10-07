# Outils hébergés : le contrat

Un outil **hébergé** est un outil que le proxy exécute lui-même, à la
place du fournisseur que le client croit avoir en face — la recherche web
qu'OpenAI ferait pour `{"type": "web_search"}`, l'outil serveur
`web_search_20250305` qu'Anthropic exécuterait. Ce que cela change pour un
client, les garde-fous, la mise en route : voir
[Outils hébergés](../README.md#outils-hébergés) dans le README. Ce fichier
décrit l'autre côté : **ce qu'est un outil pour le code du proxy**, et
comment en écrire un.

Le dépôt en porte deux, `web_search` et `web_fetch`
(`llm_proxy/tools/`). Le contrat est écrit pour ceux qui suivront — une
lecture d'image par un modèle de vision, une transcription, les outils
d'un serveur MCP découverts au démarrage, une exécution de code — sans
les construire : voir [Prévu, pas construit](#prévu-pas-construit).

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
  à côté, des sources, des faits pour l'affichage. C'est le seul format
  d'échange entre un outil et le reste du proxy.
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
```

| Membre | Pour qui | Règle |
|---|---|---|
| `text` | le modèle | **Seul ce texte est mémorisé et rejoué.** Tout ce que le modèle doit savoir y est écrit, en anglais. Borné par `[tools].max_result_chars` : le surplus est coupé et marqué `[truncated]` par l'exécuteur |
| `error` | le proxy et le client | Un code de la [liste fermée](#codes-derreur), `None` pour un succès. Le texte d'une erreur commence par `Error: ` et dit au modèle quoi faire ; le code voyage **à côté**, il ne se déduit jamais du texte (une page lue peut commencer par « Error: ») |
| `sources` | le client | `Source(url, title, date, snippet)` — seule `url` est obligatoire, `date` est `AAAA-MM-JJ` ou vide. Elles font les annotations `url_citation` d'un client chat/completions et les blocs `web_search_result` d'un client Anthropic. Une erreur n'en a pas |
| `meta` | le client | Un dictionnaire de valeurs JSON : l'URL réellement lue, un titre, une plage de caractères… Rien que le modèle doive lire, rien qui soit gardé. Les clés sont à l'outil |

Quand l'exécuteur coupe un texte trop long, `sources` et `meta` ne sont
pas touchés : ils disent ce que l'outil a trouvé.

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

### Ce que `run` peut faire, et ce qui lui arrive

- Rendre un `Result`, succès ou échec.
- Lever `ToolError(code, message)` : rendu en `failure(code, message)`.
- Lever autre chose : c'est une panne. Le modèle lit
  `Error: <nom> failed (<Exception>).`, le code est `unavailable`, la
  trace part dans le journal. Rien ne remonte au client.
- Dépasser `[tools].run_timeout` : la coroutine est annulée, code
  `timeout`.
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
| `timeout` | Délai de l'exécution dépassé (`run_timeout`) | l'exécuteur |
| `limit` | Limite d'appels de la réponse atteinte (`max_calls`, `max_uses` du client) — rien n'a été exécuté | l'exécuteur |
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
   `run(args, call)` sous `run_timeout`, toute exception rattrapée ; enfin
   le texte coupé à `max_result_chars`.
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
`sources` et `meta` ceux du `Result`. Toujours `200` quand l'outil
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

Un fournisseur qui apporte plusieurs outils (un serveur MCP) appelle
`register` une fois par outil découvert, au démarrage.

Ce qu'il faut tenir :

- **Tout ce que le modèle doit savoir est dans `text`.** `sources` et
  `meta` ne lui parviennent pas et ne sont pas gardés.
- **`args` vient du modèle** : types faux, champs en trop, URL hostiles.
  Valider, et rendre `invalid_input` avec une phrase qui dit quoi
  corriger.
- **Une cible choisie par le modèle passe par `net.public_target`**
  (`tools/net.py`) : adresses publiques seulement, connexion vers
  l'adresse vérifiée, à chaque redirection.
- **`run` est annulable** : pas de travail bloquant dans la boucle
  asyncio (`asyncio.to_thread` pour du CPU), et rien à nettoyer ailleurs
  que dans un `finally`.
- **Un texte d'erreur parle au modèle** : ce qui s'est passé, et s'il
  doit réessayer. « Do not retry the search now » évite huit appels
  identiques.

### Les deux outils du dépôt

| | `web_search` | `web_fetch` |
|---|---|---|
| `summary` | `{"type": "search", "query"}` | `{"type": "open_page", "url"}` — l'URL suivie de la plage lue, `<url> [20000, 40000]`, pour un morceau de page |
| `sources` | une par résultat : titre, URL, date, extrait | une : l'URL telle que le modèle l'a écrite (c'est elle qu'il citera) |
| `meta` | — | `url` (réellement lue, après redirections), `title`, `content_type`, `total` (caractères de la page), `range` (`[début, fin]`, seulement pour un morceau) |
| `settings` lus | `allowed_domains`, `blocked_domains` | `allowed_domains`, `blocked_domains`, `max_chars` |
| Codes rendus | `invalid_input` (pas de `query`), `unavailable` (tout le reste : SearXNG injoignable, non configuré, moteurs bloqués) | `invalid_input`, `not_allowed`, `not_accessible`, `too_many_requests`, `unsupported` |

## Prévu, pas construit

Le contrat s'arrête au texte. Trois extensions sont attendues, et rien
de ce qui précède ne les construit :

- **Résultats non textuels, fichiers.** Une image produite, un fichier
  sorti d'une exécution de code. Aujourd'hui `Result` n'a que `text` ;
  il faudra dire où vit le fichier, combien de temps, et ce que chaque
  protocole en rend. `meta` n'est pas fait pour cela.
- **État de session entre appels.** Un conteneur d'exécution qui survit
  d'un appel au suivant, une session MCP. `Call.client` identifie le
  client, pas la conversation — qu'aucune des trois API n'identifie.
- **Délai et quota propres à un outil.** Les bornes sont celles de
  `[tools]`, communes : `run_timeout`, `max_calls`. Un outil peut déjà
  refuser de lui-même par `ToolError("limit", …)` (compté `limit`), mais
  rien ne lui donne un délai plus long ni un compteur à lui.
