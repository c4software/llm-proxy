# Backends des comptes

Chaque compte peut déclarer ses propres backends, à côté de ceux de
`[backends.<nom>]`. La partie *Configuration → Backends* de `/ui` apparaît
sauf `[user_backends].enabled = false` (voir [`[user_backends]`](configuration.md#user_backends)).

## Déclarer un backend

Un backend de compte a quatre champs :

| Champ | Rôle |
|---|---|
| Nom | Le préfixe de ses modèles : `perso` sert `perso/<modèle>`. 1 à 40 caractères parmi `a-z`, `0-9`, `.`, `_`, `-`, une lettre ou un chiffre en tête. Un nom de `[backends.<nom>]` est refusé |
| Protocole | `openai` (`/v1/chat/completions`) ou `anthropic` (`/v1/messages`, voir [Backend Anthropic](configuration.md#backend-anthropic)) |
| Adresse | La base du backend, sans `/v1`, sans identifiants |
| Clé | Envoyée au backend en `Authorization: Bearer` (`x-api-key` en `anthropic`). Facultative. Jamais rendue à la page |

Ses modèles s'emploient comme les autres : dans le chat de la page, et
avec toutes les clés du compte sur `/v1/*`. Ils apparaissent dans
`/v1/models` et `/ui/models`. Un nom de `[backends.<nom>]` ajouté plus tard
l'emporte sur celui d'un compte.

À la modification, une clé laissée vide reste, sauf si l'adresse change
d'origine (schéma, hôte ou port) : il faut alors la ressaisir.

## Pour qui

| Choix | Effet |
|---|---|
| *moi seul* | Seul le compte qui l'a déclaré le voit et s'en sert |
| *tout le monde (avec ma clé)* | Tous les comptes le voient et s'en servent, avec la clé de son créateur. Ils voient son nom, son protocole, son adresse et son créateur, jamais la clé. Leurs requêtes vers ces modèles partent vers ce serveur |

Deux backends partagés ne portent pas le même nom. Un backend *moi seul*
peut reprendre le nom d'un backend partagé : il le masque pour son compte.

Un compte bloqué perd ses backends, partagés compris ; il les retrouve
quand il est rétabli. L'admin voit les backends de tous les comptes dans
la même partie et peut en supprimer.

Les requêtes vers un backend de compte ne comptent pas dans le
[partage équitable](tableau-de-bord.md#partage-équitable). Un backend
partagé n'a pas d'autre borne que celles de son serveur.

Les [modèles permis d'un compte](tableau-de-bord.md#comptes-oidc),
posés par l'admin, ne s'appliquent pas à ses propres backends ; ils
s'appliquent aux backends partagés par les autres. Les risques sont
décrits dans [Sécurité](securite.md#backends-des-comptes).

## API

Par la session de la page, en JSON :

| Route | Effet |
|---|---|
| `GET /ui/backends` | `{backends, shared, reserved}` : les siens, ceux que d'autres partagent (avec `by`, leur créateur), les noms de `[backends.<nom>]`. Admin, `?all=1` : `all`, ceux de tous les comptes |
| `POST /ui/backends` | Crée `{name, api, url, api_key?, shared}` |
| `PUT /ui/backends/<id>` | Modifie (même corps) |
| `DELETE /ui/backends/<id>` | Supprime → `{deleted}`. Admin, `?all=1` : celui de n'importe quel compte |

Un compte ne voit ni ne modifie le backend d'un autre (`404`) ; sa
suppression rend `{"deleted": false}`. Sans comptes OIDC, toute session de
la page a les droits d'admin ici, comme pour *Extensions* et *Proxy*.
