# Prompts

Les **prompts** sont les textes que le modèle lit et que le proxy fournit :
descriptions des outils du proxy et du navigateur et de leurs paramètres,
consignes des familles d'outils du navigateur (ajoutées au prompt
système), consigne de fin de budget d'outils, instructions du serveur MCP,
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
| `page.toml` | Outils du navigateur (`<outil>.description`, `<outil>.param.<chemin>`) et consignes de leurs familles (`family.<clé>.guidance`) ; variables : les `settings` de l'outil |
| `mcp.toml` | Instructions du serveur MCP exposé |

Une table par sujet, une clé par texte : `[web_fetch]` puis
`description = "…"` donnent la clé `web_fetch.description`. La description
d'un paramètre s'écrit `"param.<nom>"`.

### Variables

Un texte peut contenir des **variables** `{nom}`, remplies par le proxy à
chaque usage : une borne (`{max_pages}`), le nom d'un outil
(`{web_search}`), ou un fragment qui n'est présent que si un autre outil est
proposé au modèle en même temps (`{search_hint}`, vide sinon). `{{` et `}}`
écrivent une accolade.

Un texte modifié n'utilise que les variables de son texte par défaut. Les
variables permises sont affichées dans l'édition et rendues par
`GET /ui/prompts`.

## Modifier un prompt

*Configuration → Prompts* liste les textes par domaine, avec une recherche
et un filtre sur les textes modifiés. Pour chacun : *Modifier*, puis
*Enregistrer* ; *Rétablir* rend le texte par défaut. Un texte enregistré
égal au défaut n'est pas gardé comme modification.

- **Droits** : un admin ; sans comptes OIDC, la page connectée, comme la
  configuration du proxy.
- **Portée** : tout le proxy, tous les comptes et toutes les surfaces.
- **Effet** : à la requête suivante ; pour les outils du navigateur, au
  rechargement de la page du chat, qui reçoit leurs textes avec
  `/ui/session`.
- **Borne** : 20 000 caractères par texte.

Les modifications sont gardées dans `data/prompts.json`, à côté de
`config.toml` (écriture atomique), et incluses dans la
[sauvegarde](deploiement.md#sauvegarde-et-restauration). Le fichier est
relu quand il change : une restauration s'applique sans redémarrage.

- Un fichier illisible est dit au journal : les textes par défaut
  s'appliquent, et la page refuse d'écrire (409) tant qu'il n'est pas
  corrigé ou retiré.
- Une modification devenue invalide après une mise à jour (clé retirée,
  variable disparue du défaut) est ignorée et dite au journal.

Un ancien réglage `[tools.image_generation].upscale_prompt` n'est plus lu :
reporter son texte dans le prompt `image_generation.upscale`.

## Routes

| Route | Effet |
|---|---|
| `GET /ui/prompts` | `{max_chars, prompts: [{key, domain, default, override, variables}]}` ; `override` vaut `null` sans modification |
| `PUT /ui/prompts/{clé}` | Corps `{"text": "…"}` ; rend l'entrée mise à jour. 404 `not_found` : clé inconnue ; 400 `invalid_prompt` : variable inconnue ou mal écrite, accolade seule, texte vide ou trop long ; 409 `conflict` : `prompts.json` illisible |
| `DELETE /ui/prompts/{clé}` | Rend le texte par défaut ; mêmes erreurs 404 et 409 |

Ces routes répondent 403 à un compte qui n'est pas admin.

## Extensions

Les outils d'une [extension](extensions.md) gardent leurs textes dans le
code de l'extension ; ils ne sont pas dans le catalogue.
