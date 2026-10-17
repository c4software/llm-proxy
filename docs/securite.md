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
- `GET /healthz`, réduit à `{"status": "ok"}`. Le détail (backends et
  leurs URL, outils, caches) exige une clé, ou la session de la page par
  `GET /ui/health`.
- Ce qu'il faut pour entrer dans la page :
  - `/` (redirige vers `/ui`), `/ui` ;
  - ses fichiers : `/ui/static/*`, `/ui/sw.js` ;
  - `GET /ui/auth`, `GET /ui/auth/login` et le retour OIDC ;
  - `POST /ui/login`, `POST /ui/logout`.

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
