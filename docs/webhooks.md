# Webhooks

Chaque compte peut définir ses propres webhooks, dans *Configuration →
Webhooks* de `/ui`. Il y a deux sortes de webhooks :

- un webhook **entrant** est une adresse secrète. Un service tiers
  (supervision, CI, formulaire, cron, Slack) y poste un message au format
  d'un webhook entrant de Slack. Le message entre dans une conversation du
  chat de la page, et le modèle y répond au nom du compte ;
- un webhook **sortant** est une URL [Apprise](https://github.com/caronc/apprise/wiki)
  (Slack, Mattermost, Discord, Teams, Telegram, ntfy, e-mail…). Le proxy y
  envoie la réponse à un webhook entrant. Le modèle peut aussi y poster, par l'outil
  hébergé `webhook`, quand l'utilisateur le lui demande.

## Activer

```toml
[ui]
chat = true          # un message reçu va dans le chat de la page

[webhooks]
enabled = true
```

Sans ces deux réglages, `/hooks/*` répond `404`. L'onglet Webhooks et
l'outil `webhook` n'apparaissent pas non plus. Les autres réglages
(bornes, délai, adresses privées) sont décrits dans
[Configuration](configuration.md#webhooks).

Les définitions sont gardées dans `data/conversations.db`, à côté de
l'historique, et sauvegardées avec lui.

## Webhook entrant

### Réglages

| Réglage | Rôle |
|---|---|
| Nom | Affiché dans la liste et sur les messages reçus. Il préfixe aussi le titre d'une conversation créée par le webhook |
| Modèle | Le modèle qui répond. Vide : celui de la conversation, sinon `[ui].chat_model` |
| Le message peut en demander un autre | Le champ `model` du corps est alors accepté. Sinon, un corps qui le donne reçoit `403 action_prohibited` |
| Prompt système | Facultatif. Il remplace celui de la conversation pour les réponses du webhook, et ne sert qu'à elles (voir sous le tableau) |
| Outils hébergés | Les outils présentés au modèle (recherche web, code, [dépôts](depots.md), mémoire, `webhook`…). Aucun coché : ceux d'une conversation existante choisie, sinon aucun. Les outils de la page (navigateur, appareil) n'existent pas ici, faute de navigateur |
| Conversation | **Une nouvelle à chaque message** ; **toujours la même** : une conversation existante choisie dans la liste, ou une conversation dédiée créée au premier message ; ou **aucune, passe plat** (voir [La réponse](#la-réponse)) |
| Réponse envoyée à | Un webhook sortant du compte, ou aucun (la réponse reste alors dans la conversation). Obligatoire en passe plat |

Le prompt système du webhook ne devient pas celui de la conversation : une
conversation créée par le webhook n'en hérite pas, et une nouvelle
conversation ouverte depuis elle dans la page part des réglages par
défaut. Les conversations qui l'avaient hérité en sont nettoyées au
démarrage du proxy.

L'adresse, `https://<proxy>/hooks/wh-…`, s'affiche à la création, puis à
tout moment par le bouton **Adresse** du webhook, avec Copier et un exemple
`curl`. **Régénérer** donne une nouvelle adresse et ferme l'ancienne. Si
le bouton Adresse répond que le jeton n'est pas gardé (webhook créé avant
v2026.11.21.2), il faut régénérer le webhook une fois. L'adresse vaut
droit d'écrire dans l'historique du compte et de dépenser ses tokens : il
faut la garder comme une clé.

### Le message

`POST /hooks/<jeton>`, sans clé du proxy. Le corps peut prendre les formes
d'un webhook Slack :

```bash
curl -X POST -H 'Content-Type: application/json' \
  -d '{"text": "Disque plein sur *srv1* : <https://grafana.example/d/1|tableau>"}' \
  https://proxy.example/hooks/wh-…
```

- JSON `{"text": …}`. Sans `text`, le texte de ses `blocks` (Block Kit :
  `header`, `section` et ses `fields`, `context`, `rich_text`) ; les
  `attachments` s'y ajoutent (`pretext`, `title`, `text`, `fields`, sinon
  `fallback`) ;
- formulaire `payload=<JSON>`, comme les anciennes intégrations Slack ;
- formulaire d'une **commande slash** (`text`, `user_name`,
  `response_url`) ;
- `text/plain` : le corps est le texte.

Le mrkdwn de Slack est lu : `<url|texte>` devient un lien Markdown,
`<@U…|nom>` devient `@nom`, et `&amp;`, `&lt;`, `&gt;` sont décodés.

Le proxy accepte aussi quatre champs facultatifs :

| Champ | Rôle |
|---|---|
| `model` | Un autre modèle, si le webhook le permet |
| `username` (ou `user_name`) | Qui écrit, affiché sur le message (« reçu par le webhook « Alertes » · zabbix ») |
| `thread_ts` | Repris dans la réponse envoyée à un sortant JSON (hors Apprise) : elle part dans le même fil Slack |
| `response_url` | Commande slash : la réponse y est aussi postée (`response_type: in_channel`). En `https`, vers une adresse publique seulement, même avec `[webhooks].allow_private` |

### La réponse

Comme Slack, l'appel reçoit `200 ok` tout de suite. Le modèle répond
ensuite, en tâche de fond :

1. le message entre dans la conversation, marqué comme reçu par le
   webhook, avec une réponse **en cours** ;
2. le proxy envoie au modèle **le seul message reçu**, précédé du prompt
   système (celui du webhook, sinon celui de la conversation) : jamais les
   tours précédents, même dans une conversation « toujours la même ».
   La conversation sert à ranger et relire les messages ; si l'on y écrit
   ensuite depuis la page, la page envoie, elle, tout l'historique. La
   requête passe par `/ui/chat/completions` au nom du compte qui a créé le
   webhook : ses statistiques, sa part du partage équitable ;
3. la réponse prend la place de celle en cours, puis part vers le webhook
   sortant choisi. Une erreur (backend hors ligne,
   historique plein…) prend aussi la place de la réponse en cours et part
   au sortant et au `response_url`, préfixée « Erreur : ».

Les messages d'un webhook « toujours la même conversation » sont traités
l'un après l'autre, dans l'ordre. Supprimée entre deux messages, sa
conversation est remplacée par une nouvelle, dédiée. Supprimée pendant le
tour, elle n'est pas recréée pour la seule réponse ; une question retirée
(message modifié) ne reçoit pas de réponse.

En **passe plat**, rien n'est rangé : le modèle reçoit le seul message (et
le prompt système du webhook), et sa réponse va seulement au sortant (et
au `response_url`). Le tour est incognito : ni échange d'outils ni dépôt
gardé ([Dépôts](depots.md#ce-qui-est-gardé-et-combien-de-temps)), l'outil
`memory` n'est pas offert même coché, et un fichier produit par un outil
(une image, un graphique) n'est lisible que 15 minutes
(`[files].incognito_ttl`), y compris par son lien envoyé au sortant. Le
sortant d'un passe plat ne peut pas être supprimé (`409`) tant que le
passe plat s'en sert.

Pendant la réponse en cours, la page affiche l'indicateur de saisie
(« réponse en cours, menée par le proxy ») et relit la conversation toutes
les 3 secondes jusqu'à la réponse. Elle n'y écrit pas pendant ce temps
(envoyer, modifier, régénérer) : elle écrirait sur une version dépassée.
Une réponse restée en cours plus de 30 minutes (proxy redémarré pendant le
tour) s'affiche en erreur, au chargement comme pendant l'attente.

Une page ouverte sur le chat relit la liste des conversations toutes les
20 secondes et au retour sur l'onglet. Une conversation nouvelle y entre,
et la conversation affichée est relue si le proxy en a une version plus
récente, à condition que rien ne soit en cours d'écriture dans la page.

La liste des webhooks montre, pour chacun, la date du dernier message, le
nombre de messages et la dernière erreur.

### Erreurs

En texte, comme Slack :

| Statut | Corps | Cause |
|---|---|---|
| 400 | `invalid_payload` | Corps illisible |
| 400 | `no_text` | Aucun texte trouvé |
| 400 | `msg_too_long` | Corps de plus de 1 Mo, ou texte au-delà de `[webhooks].max_text_chars` |
| 400 | `model_not_found` | `model` sans backend connu |
| 403 | `action_prohibited` | `model` refusé par le webhook, compte bloqué ou en attente, clé du TOML retirée, ou message posté par un proxy (un sortant pointé sur un entrant, qui bouclerait) |
| 404 | `no_service` | Jeton inconnu, ou webhooks inactifs |
| 429 | `rate_limited` | Au-delà de `[webhooks].calls_per_minute` pour ce webhook, ou de `[webhooks].max_running` réponses en cours pour le compte |

## Webhook sortant

Un nom et une URL [Apprise](https://github.com/caronc/apprise/wiki) : le
service, ses identifiants et, quand le service le permet, le salon.

| Service | URL |
|---|---|
| Slack | `slack://T…/B…/X…/#alertes`, ou l'URL du webhook `https://hooks.slack.com/services/…` |
| Mattermost | `mmost://chat.exemple.fr/<clé>` |
| Discord | `discord://<id>/<jeton>`, ou `https://discord.com/api/webhooks/…` |
| Teams, Telegram, ntfy, Matrix, e-mail… | voir le [wiki d'Apprise](https://github.com/caronc/apprise/wiki) |

Le message part en Markdown ; chaque service le rend à sa façon. Un texte
trop long pour le service part en plusieurs messages (sauf `overflow=`
dans l'URL) ; au-delà de 39 000 caractères, il est coupé. La réponse à un
message reçu est un nouveau message, pas une réponse dans le fil Slack.
Un envoi dure au plus `[webhooks].timeout`.

Une URL `http(s)://` qu'Apprise ne reconnaît pas reçoit
`{"text": "<message en mrkdwn>"}`, comme un webhook entrant de Slack. La
page la marque **à ressaisir** : l'envoi suit alors les règles réseau des
outils (`[tools.net]`, adresses publiques seulement sauf
`[webhooks].allow_private`), et une redirection n'est pas suivie. Un tel
sortant garde son salon, envoyé en `channel`, tant que l'URL n'est pas
ressaisie.

Toute URL qu'Apprise sait servir est acceptée, sans règle d'adresse :
`json://` ou `mmost://` vers une machine du réseau interne, `mailto://`…
Un compte peut donc faire envoyer un message vers toute adresse que le
proxy joint. Sont refusés : une URL qui vise un webhook entrant d'un proxy
(`/hooks/wh-…`, sous tout schéma : chaque réponse deviendrait un nouveau
message), un en-tête `+User-Agent=`, et un paramètre qui désigne un
fichier local (`keyfile=/…`).

L'URL vaut droit d'écrire. Le tableau n'en montre que le schéma et l'hôte ;
**Copier** et **Modifier** donnent l'URL entière, au seul compte qui l'a
créée.
**Essayer** envoie un message d'essai ; l'erreur d'Apprise s'affiche sur
le sortant.

### L'outil `webhook`

Quand les webhooks sont actifs, le chat propose l'outil **Webhooks** (menu
Outils). Il ne voit que les sortants **exposés à l'outil** (case du
formulaire, décochée par défaut) : un sortant décoché ne sert qu'aux
réponses des webhooks entrants. Le modèle l'appelle avec un texte et,
s'il y a plusieurs sortants exposés, le nom de l'un d'eux : « poste le
résumé sur Équipe ». Son prompt lui interdit de l'appeler de lui-même. Il
ne voit jamais les URL, seulement les noms des sortants exposés, rendus
sur erreur (nom inconnu, ou plusieurs sortants sans nom donné).

Comme tout outil hébergé, il est aussi offert à un client de l'API qui le
déclare (`{"type": "webhook"}` sur `/v1/chat/completions`), avec les
sortants du compte de sa clé.

## Sécurité

- Le jeton d'un entrant est la seule protection de `/hooks/<jeton>` : il
  ne doit circuler que vers le service qui l'utilise. Il est gardé en clair
  dans `data/conversations.db` (le bouton Adresse le réaffiche), comme les
  URL des sortants : voir
  [Secrets gardés en clair](securite.md#secrets-gardés-en-clair).
- Les réponses sont faites au nom du créateur. Le proxy vérifie, à chaque
  message, que son compte est actif (comptes OIDC) ou que sa clé est
  encore dans `proxy.api_keys`. Un webhook créé quand le proxy était ouvert
  cesse de répondre si le proxy devient fermé.
- Un message reçu est un texte venu de l'extérieur, que le modèle lit
  comme celui de l'utilisateur. Avec des outils actifs (`webhook`, code,
  mémoire), il peut leur faire faire ce que l'utilisateur pourrait
  demander. Il vaut mieux ne cocher que les outils utiles au webhook.
- Avec un `response_url`, l'appelant lit la réponse. Il n'atteint pas
  l'historique de la conversation, que le modèle ne reçoit pas, mais il
  atteint le prompt système et ce que lisent les outils cochés (un dépôt,
  le web, la mémoire) : le jeton vaut l'accès à ce que le webhook peut
  lire.
