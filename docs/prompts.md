# Prompts

Les **prompts** sont les textes que le modèle lit et que le proxy fournit :
descriptions des outils du proxy et du navigateur et de leurs paramètres,
consignes des familles d'outils du navigateur (ajoutées au prompt
système), textes que la page du chat ajoute aux messages (résumé d'une
conversation compactée, état du moment des outils, protocole de Gemini
Nano), consigne de fin de budget d'outils, instructions du serveur MCP,
consigne d'agrandissement d'une image.
Ils sont définis dans un catalogue et modifiables par l'admin depuis
*Configuration → Prompts*, sans reconstruire l'image.

Les messages d'erreur et les résultats des outils ne sont pas des prompts :
ils restent dans le code.

## Le catalogue

Les textes par défaut sont dans `proxy/llm_proxy/prompts/`, en anglais, un
fichier TOML par domaine :

| Fichier | Contenu |
|---|---|
| `tools.toml` | Outils du proxy (`web_fetch`, `ocr`, `code_execution`…), consigne de fin de budget d'outils, agrandissement d'image |
| `page.toml` | Outils du navigateur (`<outil>.description`, `<outil>.param.<chemin>`), consignes de leurs familles (`family.<clé>.guidance`) et état du moment d'une famille, joint au message de l'utilisateur (`family.<clé>.state…`) ; variables : les `settings` de l'outil, et pour l'état, ce que la page connaît (identifiants, noms, nombres) |
| `chat.toml` | Textes que la page du chat ajoute aux messages : résumé d'une conversation compactée (`compact.*`), état du moment (`messages.*`), protocole de Gemini Nano (`gemini_nano.*`) |
| `mcp.toml` | Instructions du serveur MCP exposé |

Une table par sujet, une clé par texte : `[web_fetch]` puis
`description = "…"` donnent la clé `web_fetch.description`. La description
d'un paramètre s'écrit `"param.<nom>"`.

### Variables

Un texte peut contenir des **variables** `{nom}`, remplies à chaque usage
par le proxy, ou par la page pour les textes qu'elle ajoute (`chat.toml`,
`family.<clé>.state…`) : une borne (`{max_pages}`), le nom d'un outil
(`{web_search}`), ou un fragment qui n'est présent que si un autre outil est
proposé au modèle en même temps (`{search_hint}`, vide sinon). `{{` et `}}`
écrivent une accolade.

Un texte modifié n'utilise que les variables de son texte par défaut. Les
variables permises sont affichées dans l'édition et rendues par
`GET /ui/prompts`.

## Modifier un prompt

*Configuration → Prompts* liste les textes par catégorie, avec une
recherche et un filtre sur les textes modifiés. Pour chacun : *Modifier*,
puis *Enregistrer* ; *Rétablir* rend le texte par défaut. Un texte
enregistré égal au défaut n'est pas gardé comme modification.

Par défaut, la page ne montre que les catégories qui règlent le
comportement du chat :

| Catégorie | Textes |
|---|---|
| Compactage d'une conversation | `compact.*` |
| État du moment | `messages.*`, `family.<clé>.state…` |
| Consignes des familles d'outils | `family.<clé>.guidance` |
| Gemini Nano | `gemini_nano.*` |
| Agrandir une image | `image_generation.upscale` |

*Tous les textes* ajoute les catégories *Outils du proxy*, *Outils du
navigateur* et *Serveur MCP* : les descriptions des outils, de leurs
paramètres et les instructions MCP. Le modèle s'en sert pour appeler les
outils ; une description modifiée peut dégrader ces appels sans erreur
visible. La recherche et *modifiés seulement* portent sur tous les textes,
quelle que soit la case.

- **Droits** : les lire, comme la configuration du proxy (un admin ; sans
  comptes OIDC, la page connectée) ; les modifier écrit `config.toml` : un
  **admin OIDC** seulement. Sans comptes OIDC, *Modifier* et *Rétablir*
  sont absents et les routes répondent 403 : un texte se modifie à la main
  dans [`[prompts]`](configuration.md#prompts).
- **Texte refusé** : vide, de plus de 20 000 caractères, avec une variable
  que son défaut n'a pas, ou avec `${` (un prompt ne lit pas
  l'environnement).
- **Portée** : tout le proxy, tous les comptes et toutes les surfaces.
- **Effet** : à la requête suivante ; pour les outils du navigateur et les
  textes de la page, au rechargement de la page du chat, qui les reçoit
  avec `/ui/session` (`chat.page`, et `chat.prompts` : les gabarits que la
  page remplit elle-même).

Les modifications sont gardées dans la table
[`[prompts]`](configuration.md#prompts) de `config.toml` : clé du
catalogue = texte. La page la met à jour
([Enregistrer depuis la page](configuration.md#enregistrer-depuis-la-page)) ;
modifiée à la main, elle s'applique au
[rechargement à chaud](configuration.md#rechargement-à-chaud).

Une modification devenue invalide après une mise à jour (clé retirée,
variable disparue du défaut) est ignorée et dite au journal.

Un ancien réglage `[tools.image_generation].upscale_prompt` n'est plus lu :
reporter son texte dans le prompt `image_generation.upscale`.

## Routes

| Route | Effet |
|---|---|
| `GET /ui/prompts` | `{max_chars, writable, categories: [{label, essential}], prompts: [{key, domain, category, essential, default, override, variables}]}` ; `writable` : la page peut-elle écrire (admin OIDC) ; `categories` : dans l'ordre d'affichage, `essential` pour celles montrées par défaut ; `override` vaut `null` sans modification |
| `PUT /ui/prompts/{clé}` | Corps `{"text": "…"}` ; rend l'entrée mise à jour. 404 `not_found` : clé inconnue ; 400 `invalid_prompt` : variable inconnue ou mal écrite, accolade seule, `${`, texte vide ou trop long ; 409 `conflict` : pas de `config.toml` |
| `DELETE /ui/prompts/{clé}` | Rend le texte par défaut ; 404 `not_found` : clé inconnue |

`GET` répond 403 à un compte qui n'est pas admin ; `PUT` et `DELETE`, à
tout autre qu'un admin OIDC (sans comptes OIDC : à tous).

## Extensions

Les outils d'une [extension](extensions.md) gardent leurs textes dans le
code de l'extension ; ils ne sont pas dans le catalogue.
