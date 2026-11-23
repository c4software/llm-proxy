# Sécurité

Une `api_key` configurée sur un backend rend ses droits accessibles à
tout client pouvant atteindre le proxy si celui-ci n'est pas protégé.
Certaines routes restent accessibles sans clé, même lorsque
l'authentification est active.

## Les trois parades

Elles se cumulent.

- **`proxy.api_keys`** : les clients doivent présenter une clé, sinon
  401. Voir [Clé du proxy](#clé-du-proxy).
- **Les [comptes OIDC](tableau-de-bord.md#comptes-oidc)** (`[oidc]`) :
  - la page ne s'ouvre que par le fournisseur d'identité ;
  - chacun gère ses clés (uuid4, seul le condensé est en base), et
    peut limiter chacune à certains outils hébergés (pas d'exécution de
    code ni de webhook pour une clé de CI, par exemple) ;
  - un admin accepte, bloque ou révoque les comptes ;
  - une clé volée n'ouvre pas la page.
- **Le réseau** : Nginx Proxy Manager, Tailscale, réseau Docker partagé.

## Clé du proxy

Le client transmet la clé selon la surface utilisée : `Authorization: Bearer <clé>`
sur la surface OpenAI ou `x-api-key` sur Anthropic.

Quand l'auth est active :

- ni le Bearer du client ni ses cookies ne sont relayés aux backends ;
- `x-api-key` n'est jamais relayé.

Le tableau de bord `/ui` exige la même clé :

- La page la demande dans un formulaire, jamais dans l'URL.
- Elle ouvre ensuite une session signée qui ne contient que le condensé de la clé.
- Le cookie est `HttpOnly`, `SameSite=Lax` et `Secure` en HTTPS. Le
  HTTPS est reconnu en direct ou derrière un reverse proxy qui envoie
  `X-Forwarded-Proto: https` (comme Nginx Proxy Manager).
- Le cookie est limité à `/ui`. Il ne dépense des tokens que via le
  chat, et seulement si `[ui] chat = true`.
- Les routes `/ui` qui écrivent ou dépensent exigent un corps JSON.

## Ce qui reste accessible sans clé

Même avec l'auth active :

- `GET /v1/files/<jeton>/<nom>` : le jeton du lien vaut droit d'accès.
- `POST /hooks/<jeton>` : un [webhook entrant](webhooks.md), dont le jeton
  vaut droit d'accès. Il répond au nom du compte qui l'a créé, tant que ce
  compte est actif ou que sa clé du TOML est encore là. Voir
  [Webhooks, Sécurité](webhooks.md#sécurité).
- `GET /healthz`, réduit à `{"status": "ok"}`. Le détail (backends et
  leurs URL, outils, caches) exige une clé, ou la session de la page par
  `GET /ui/health`.
- Routes nécessaires à l'accès à la page :
  - `/` (redirige vers `/ui`), `/ui` ;
  - ses fichiers : `/ui/static/*`, `/ui/sw.js` ;
  - `GET /ui/auth`, `GET /ui/auth/login` et le retour OIDC ;
  - `POST /ui/login`, `POST /ui/logout`.

## Dépôts

L'outil `repository` ([Dépôts](depots.md)) télécharge une adresse fournie
par un compte, sans filtrage d'adresse. Une forge du réseau interne est
donc accessible, et tout compte peut définir une source. N'activer
`[repos].enabled` que si les comptes sont de confiance. Le jeton d'une
source n'est transmis ni à la page, ni au modèle, ni au bac, et n'est pas
ajouté aux URL ni transmis lors d'une redirection vers un autre hôte.

Le proxy ne décode pas le contenu d'un dépôt : l'archive est décompressée
et lue dans un bac de l'exécuteur, sans réseau (voir
[Comment un dépôt est lu](depots.md#comment-un-dépôt-est-lu)). Seule la
forge `git` fait lire un pack par git dans le proxy, avec des limites et
sans exécuter le contenu du dépôt. Les durées et emplacements sont décrits
dans [Ce qui est gardé](depots.md#ce-qui-est-gardé-et-combien-de-temps).

## Backends des comptes

Avec `[user_backends].enabled` (actif par défaut), un compte déclare l'adresse d'un backend
([Backends des comptes](backends.md)), sans filtrage d'adresse : une
machine du réseau interne du proxy est donc accessible, y compris un
backend du TOML sans clé : un compte peut ainsi atteindre des modèles que
ses [modèles permis](tableau-de-bord.md#comptes-oidc) lui refusent, hors
partage équitable. Le créateur d'un backend partagé reçoit, sur son
serveur, les requêtes des autres comptes vers ses modèles. Les outils
hébergés (OCR, transcription, images) ne visent que les backends partagés
par un admin : un autre compte ne capte pas leur trafic en partageant un
backend sous le nom attendu. Le désactiver (`enabled = false`) si les
comptes ne sont pas de confiance.

La clé d'un backend et la valeur de ses en-têtes ne sont rendues ni à la
page ni aux autres comptes ; ceux-ci voient les noms des en-têtes d'un
backend partagé. Le proxy ne transmet jamais à un backend de compte
l'`Authorization` ni les cookies du client, même sans clé et proxy ouvert,
et n'y suit pas les redirections.

## Extensions

Une [extension](extensions.md) exécute du code Python dans le processus
du proxy et accède à ses données : configuration et secrets, jetons des
comptes et bases de `data/`. Son code `.js` s'exécute dans la page de
chaque utilisateur avec sa session. Il peut lire les données accessibles
à la page (conversations, clés affichées) et appeler ses routes. Seul un
administrateur peut déposer une extension dans un dossier monté en lecture
seule. En dehors d'un tel montage, le proxy refuse les dossiers et
fichiers qu'il peut écrire ou dont il est propriétaire, car il pourrait
en modifier les droits. Une extension n'est pas isolée. Pour un outil qui
n'a pas besoin du proxy, un [serveur MCP](outils.md#serveurs-mcp) séparé
est plus sûr.

Le réglage Configuration → Extensions est réservé aux administrateurs.
Sans comptes OIDC, il est accessible à toute personne qui ouvre la page,
comme la configuration. Il permet uniquement de sélectionner les fichiers
`[extensions.<nom>].enabled = false` désactive l'extension. La page ne peut
pas réactiver une extension ainsi désactivée.

`offered` (où un outil est offert, `api` ou `web`) détermine sa
**présentation**, pas son autorisation. La page et les webhooks ne
disposent pas de droits par clé : un outil offert au `web` est accessible
à tout compte utilisant la page et à tout webhook entrant. Sans comptes
OIDC, une clé du TOML donne aussi accès aux routes de la page
(`/ui/tools`).

L'extension d'exemple `pull_request` écrit sur une forge avec le jeton
d'un compte de service, y compris pour le texte reçu par un webhook : voir
[Sa sûreté](extensions.md#pull_request--proposer-une-modification).

## Webhooks sortants

Une URL Apprise de webhook sortant n'est pas soumise à un filtrage
d'adresse : un compte peut faire envoyer un message vers toute adresse
accessible au proxy, y compris sur le réseau interne. Seules les URL hors
Apprise sont soumises à `[tools.net]`. Voir
[Webhook sortant](webhooks.md#webhook-sortant).

## Secrets gardés en clair

`data/conversations.db` conserve en clair les jetons des webhooks
entrants, les URL des webhooks sortants et les jetons des sources de
dépôts. La page doit pouvoir réafficher ces URL et le proxy doit présenter
les jetons aux forges. Une copie de la base ou de la sauvegarde de `data/`
contient donc des URL et des jetons utilisables. Les données conservées en
mémoire par le proxy (dépôts, fichiers produits) peuvent finir dans le
swap de l'hôte : utilisez un swap chiffré ou désactivez-le.

## Le conteneur du proxy

Le service `albert-proxy` est exécuté avec des restrictions (voir
[Déploiement](deploiement.md#durcissement-du-conteneur)) : image en
lecture seule, écriture limitée à `./data` et à `/tmp` en mémoire,
capacités Linux supprimées, élévation de privilèges désactivée et
utilisateur non root. Une faille dans le décodage de données tierces
(pages, PDF, audio, packs git) ne peut donc pas modifier l'image ni
obtenir de privilèges supplémentaires.

## La page

- **En-têtes** de `/ui` : `X-Frame-Options: SAMEORIGIN` (pas de cadrage
  par un autre site), `X-Content-Type-Options: nosniff` et
  `Referrer-Policy: same-origin`. Il n'y a pas de
  `Content-Security-Policy` : Vue, en build global, compile ses gabarits
  à la volée. De plus, les cadres `srcdoc` (artifacts, bacs, VM Linux)
  héritent de la politique de la page. Une directive `script-src` stricte
  bloquerait leur code en ligne. Chaque cadre définit sa propre politique
  au début de son document.
- **Markdown** : tout le texte est échappé avant l'analyse. Le caractère
  NUL, jeton interne du rendu, est retiré à l'entrée et refusé dans une
  URL. Les images d'un autre site ne sont chargées qu'au clic
  ([chat](chat.md)).
- **Artifacts** : la politique de contenu est appliquée avant le contenu.
  La capture rendue par un artifact n'est conservée que si elle est en
  base64, au format JPEG ou PNG et dans les limites définies.

## Outils hébergés et exécution de code

- Avec les [outils hébergés](outils-heberges.md), le proxy envoie des
  requêtes vers des adresses choisies par le modèle, limitées aux
  adresses publiques. Le texte provenant du web entre aussi dans le
  contexte de l'agent. Lire
  [Ce qu'aucun garde-fou n'empêche](outils-heberges.md#ce-quaucun-garde-fou-nempêche)
  avant d'activer ces outils.
- Avec `code_execution`, du code écrit par un modèle s'exécute sur la
  machine. Lire
  [Ce que le bac à sable vaut](execution-de-code.md#ce-que-le-bac-à-sable-vaut).
