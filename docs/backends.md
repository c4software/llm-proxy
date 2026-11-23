# Backends des comptes

Chaque compte peut déclarer ses propres backends, à côté de ceux de
`[backends.<nom>]`. La partie *Configuration → Backends* de `/ui` apparaît
sauf `[user_backends].enabled = false` (voir [`[user_backends]`](configuration.md#user_backends)).

## Déclarer un backend

Un backend de compte a ces champs :

| Champ | Rôle |
|---|---|
| Nom | Le préfixe de ses modèles : `perso` sert `perso/<modèle>`. 1 à 40 caractères parmi `a-z`, `0-9`, `.`, `_`, `-`, une lettre ou un chiffre en tête. Un nom de `[backends.<nom>]` est refusé |
| Protocole | `openai` (`/v1/chat/completions`) ou `anthropic` (`/v1/messages`, voir [Backend Anthropic](configuration.md#backend-anthropic)) |
| Adresse | La base du backend, sans `/v1`, sans identifiants |
| Clé | Envoyée au backend en `Authorization: Bearer` (`x-api-key` en `anthropic`). Facultative. Jamais rendue à la page |
| En-têtes | Comme [`headers`](configuration.md#backendsnom) : un par ligne, `Nom: valeur`. Secrets comme la clé : la page n'en montre que les noms. `Nom:` sans valeur garde l'ancienne ; si l'adresse change d'origine, il faut la ressaisir (sinon `400`). Une ligne retirée retire l'en-tête. Les autres comptes voient les noms d'un backend partagé, jamais les valeurs. Noms et valeurs : voir [`headers`](configuration.md#backendsnom) |
| Images | Comme [`images`](configuration.md#backendsnom) d'une table `[backends.<nom>]` : *oui*, *non*, ou *selon le protocole* (oui en `anthropic`, non en `openai`) |
| Tokenisation | Comme [`tokenize_path`](configuration.md#backendsnom) : un chemin sous l'adresse (`/tokenize` pour llama.cpp) qui rend `count_tokens` exact. Vide : estimation |
| tool_choice | Comme [`force_tool_choice`](configuration.md#backendsnom) : `auto`, `required` ou `none`, injecté quand une requête a des outils sans `tool_choice`. Albert : `auto`. Vide : aucune injection |
| Quotas | Comme [`quotas`](configuration.md#backendsnom) : le limiteur, avec ses règles *Limites*, les limites du compte quand le backend les publie (Albert) ou les annonce dans les en-têtes de ses réponses ([Mistral, OpenAI, Groq, Anthropic](configuration.md#limites-annoncées-par-les-en-têtes)) et les réglages de [`[quotas]`](configuration.md#quotas). Posé par un admin seulement, sur ses backends ou, depuis *Backends de tous les comptes*, sur ceux d'un autre compte ; un compte garde ou ôte celui de son backend |
| Limites | Comme [`limits`](configuration.md#backendsnom), avec *Quotas* : une règle par ligne, des motifs de noms puis des plafonds, `claude-*, qwen-big rpm=10 tpd=5000000`. Posées par un admin, comme les quotas ; un compte garde ou ôte celles de son backend |

Ses modèles s'emploient comme les autres : dans le chat de la page, et
avec toutes les clés du compte sur `/v1/*`. Ils apparaissent dans
`/v1/models` et `/ui/models`. Un nom de `[backends.<nom>]` ajouté plus tard
l'emporte sur celui d'un compte.

À la modification, une clé laissée vide reste, sauf si l'adresse change
d'origine (schéma, hôte ou port) : il faut alors la ressaisir.

*Désactiver* retire un backend du routage sans le supprimer : ses modèles
ne sont plus servis ni listés, et un backend partagé n'apparaît plus aux
autres comptes (l'admin le voit, avec son état). Il garde sa clé et son nom, qu'aucun autre backend partagé ne peut
prendre. *Activer* le remet tel quel.

## Pour qui

| Choix | Effet |
|---|---|
| *moi seul* | Seul le compte qui l'a déclaré le voit et s'en sert |
| *tout le monde (avec ma clé)* | Tous les comptes le voient et s'en servent, avec la clé de son créateur. Ils voient son nom, son protocole, son adresse et son créateur, jamais la clé. Leurs requêtes vers ces modèles partent vers ce serveur |

Deux backends partagés ne portent pas le même nom. Un backend *moi seul*
peut reprendre le nom d'un backend partagé : il le masque pour son compte.

Un compte bloqué perd ses backends, partagés compris ; il les retrouve
quand il est rétabli. L'admin voit les backends de tous les comptes dans
la même partie, peut les modifier (en-têtes, limites, quotas, état, et
leur partage : un backend qu'il partage sert alors tous les comptes avec la
clé de son créateur) ou les supprimer, sans jamais voir leur clé ni la
valeur de leurs en-têtes.

Un backend partagé se comporte comme un backend de `[backends.<nom>]` :

- ses requêtes comptent dans le
  [partage équitable](tableau-de-bord.md#partage-équitable), celles de son
  créateur comprises ;
- partagé par un admin, un outil hébergé peut le viser dans son réglage
  `model` (`[tools.ocr].model = "commun/…"`, de même pour `transcribe` et
  `image_generation`). Partagé par un autre compte, non : le nom d'un
  backend partagé revient au premier compte qui le prend ;
- il figure dans `/ui/health`.

Un backend *moi seul* ne sert que son compte : ni partage équitable, ni
outil hébergé.

Les [modèles permis d'un compte](tableau-de-bord.md#comptes-oidc),
posés par l'admin, ne s'appliquent pas à ses propres backends ; ils
s'appliquent aux backends partagés par les autres. Les risques sont
décrits dans [Sécurité](securite.md#backends-des-comptes).

## API

Par la session de la page, en JSON :

| Route | Effet |
|---|---|
| `GET /ui/backends` | `{backends, shared, reserved}` : les siens, ceux que d'autres partagent et qui sont actifs (avec `by`, leur créateur), les noms de `[backends.<nom>]`. Admin, `?all=1` : `all`, ceux de tous les comptes |
| `POST /ui/backends` | Crée `{name, api, url, api_key?, headers?, shared, images?, tokenize_path?, tool_choice?, quotas?, limits?, enabled?}` (`enabled` vrai par défaut ; `quotas` : admin, sinon `403`) |
| `PUT /ui/backends/<id>` | Modifie (même corps ; `enabled` absent : état inchangé ; `headers` absent : inchangés ; présent, il remplace la liste, une valeur vide gardant l'ancienne). Admin, `?all=1` : celui de n'importe quel compte (même règle de clé). Un compte ordinaire garde les quotas posés par l'admin tant que l'adresse ne change pas d'origine |
| `DELETE /ui/backends/<id>` | Supprime → `{deleted}`. Admin, `?all=1` : celui de n'importe quel compte |

Un compte ne voit ni ne modifie le backend d'un autre (`404`) ; sa
suppression rend `{"deleted": false}`. Sans comptes OIDC, toute session de
la page a les droits d'admin ici, comme pour *Extensions* et *Proxy*.
