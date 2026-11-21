# Sécurité

Cette page dit comment protéger le proxy et ce qui reste accessible sans
clé. Point de départ : avec une `api_key` sur un backend, **le proxy
devient une clé Albert ouverte pour quiconque peut l'atteindre**.

## Les trois parades

Elles se cumulent.

- **`proxy.api_keys`** : les clients doivent présenter une clé, sinon
  401. Voir [Clé du proxy](#clé-du-proxy).
- **Les [comptes OIDC](tableau-de-bord.md#comptes-oidc)** (`[oidc]`) :
  - la page ne s'ouvre que par le fournisseur d'identité ;
  - chacun gère ses clés (uuid4, seul le condensé est en base) ;
  - un admin accepte, bloque ou révoque les comptes ;
  - une clé volée n'ouvre pas la page.
- **Le réseau** : Nginx Proxy Manager, Tailscale, réseau Docker partagé.

## Clé du proxy

Le client envoie la clé à la OpenAI (`Authorization: Bearer <clé>`) ou à
l'Anthropic (`x-api-key`).

Quand l'auth est active :

- ni le Bearer du client ni ses cookies ne sont relayés aux backends ;
- `x-api-key` n'est jamais relayé.

Le tableau de bord `/ui` exige la même clé :

- La page la demande dans un formulaire, jamais dans l'URL.
- Elle ouvre ensuite une session signée qui ne porte que le condensé de la clé.
- Le cookie est `HttpOnly`, `SameSite=Lax`, et `Secure` en HTTPS. Le
  HTTPS est reconnu en direct ou derrière un reverse proxy qui envoie
  `X-Forwarded-Proto: https` (comme Nginx Proxy Manager).
- Ce cookie ne vaut que sous `/ui`. Il ne dépense des tokens que par le
  chat, et seulement si `[ui] chat = true`.
- Les routes `/ui` qui écrivent ou dépensent exigent un corps JSON.

## Ce qui reste accessible sans clé

Même avec l'auth active :

- `GET /v1/files/<jeton>/<nom>` : le jeton du lien vaut droit d'accès.
- `POST /hooks/<jeton>` : un [webhook entrant](webhooks.md), dont le jeton
  vaut droit d'accès. Il répond au nom du compte qui l'a créé, tant que ce
  compte est actif ou que sa clé du TOML est encore là. Le jeton est gardé
  en clair dans `data/conversations.db`, comme les URL des webhooks
  sortants et les jetons des [sources de dépôts](depots.md) : la sauvegarde
  de `data/` les contient.
- `GET /healthz`, réduit à `{"status": "ok"}`. Le détail (backends et
  leurs URL, outils, caches) exige une clé, ou la session de la page par
  `GET /ui/health`.
- Ce qu'il faut pour entrer dans la page :
  - `/` (redirige vers `/ui`), `/ui` ;
  - ses fichiers : `/ui/static/*`, `/ui/sw.js` ;
  - `GET /ui/auth`, `GET /ui/auth/login` et le retour OIDC ;
  - `POST /ui/login`, `POST /ui/logout`.

## Dépôts

L'outil `repository` ([Dépôts](depots.md)) fait télécharger au proxy
une adresse fournie par un compte (sa source), sans règle d'adresse : une
forge du réseau interne est joignable, et tout compte peut définir une
source. À n'activer (`[repos].enabled`) que là où les comptes sont de
confiance. Le jeton d'une source n'est rendu ni à la page, ni au modèle,
ni au bac, ni dans une URL, et ne suit pas une redirection vers un autre
hôte.

Le contenu d'un dépôt n'est pas décodé dans le proxy : l'archive est
décompressée et lue dans un bac de l'exécuteur, sans réseau (voir
[Comment un dépôt est lu](depots.md#comment-un-dépôt-est-lu)). Seule la
forge `git` fait lire un pack par git dans le proxy, borné et sans rien
exécuter du dépôt.

## Le conteneur du proxy

Le service `albert-proxy` tourne durci (voir
[Déploiement](deploiement.md#durcissement-du-conteneur)) : image en
lecture seule, seuls `./data` et un `/tmp` en mémoire s'écrivent, aucune
capacité Linux, pas d'élévation de privilèges, utilisateur non root. Ce
que le proxy décode d'un tiers (pages, PDF, audio, packs git) a ainsi le
moins de prise possible.

## La page

- **En-têtes** de `/ui` : `X-Frame-Options: SAMEORIGIN` (pas de cadrage
  par un autre site), `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: same-origin`. **Pas de `Content-Security-Policy`** :
  Vue, en build global, compile ses gabarits à la volée, et surtout les
  cadres `srcdoc` (artifacts, bacs, VM Linux) héritent de la politique de
  la page — une `script-src` stricte casserait leur code, en ligne par
  construction. Chacun pose la sienne en tête de son document.
- **Markdown** : tout le texte est échappé avant l'analyse ; le caractère
  NUL, jeton interne du rendu, est retiré à l'entrée et refusé dans une
  URL. Les images d'un autre site ne se chargent qu'au clic
  ([chat](chat.md)).
- **Artifacts** : la politique de contenu passe avant le contenu, quel
  qu'il soit ; la capture rendue par un artifact n'est gardée que si
  c'est une image JPEG ou PNG en base64, bornée.

## Outils hébergés et exécution de code

- Avec les [outils hébergés](outils-heberges.md), le proxy **émet des
  requêtes vers des adresses choisies par le modèle**, limitées aux
  adresses publiques. Il fait aussi entrer du texte du web dans le
  contexte d'un agent. Lisez
  [Ce qu'aucun garde-fou n'empêche](outils-heberges.md#ce-quaucun-garde-fou-nempêche)
  avant de les activer.
- Avec `code_execution`, du code écrit par un modèle tourne sur la
  machine. Lisez
  [Ce que le bac à sable vaut](execution-de-code.md#ce-que-le-bac-à-sable-vaut).
