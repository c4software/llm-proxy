# Webhooks

Chaque compte peut définir ses propres webhooks, dans *Configuration →
Webhooks* de `/ui`. Il y a deux sortes de webhooks :

- un webhook **entrant** est une adresse secrète. Un service tiers
  (supervision, CI, formulaire, cron, Slack) y poste un message au format
  d'un webhook entrant de Slack. Le message entre dans une conversation du
  chat de la page, et le modèle y répond au nom du compte ;
- un webhook **sortant** est une URL compatible Slack. Le proxy y poste la
  réponse à un webhook entrant. Le modèle peut aussi y poster, par l'outil
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
| Prompt système | Facultatif. Il remplace celui de la conversation pour les réponses du webhook. Il est aussi posé sur la conversation que le webhook crée |
| Outils hébergés | Les outils présentés au modèle (recherche web, code, [dépôts](depots.md), mémoire, `webhook`…). Aucun coché : ceux de la conversation. Les outils de la page (navigateur, appareil) n'existent pas ici, faute de navigateur |
| Conversation | **Une nouvelle à chaque message** ; **toujours la même** : une conversation existante choisie dans la liste, ou une conversation dédiée créée au premier message ; ou **aucune, passe plat** : rien n'est gardé, le modèle ne reçoit que le message (et le prompt système du webhook) et sa réponse va seulement au sortant, qui est alors obligatoire |
| Réponse envoyée à | Un webhook sortant du compte, ou aucun (la réponse reste alors dans la conversation) |

L'adresse, `https://<proxy>/hooks/wh-…`, s'affiche à la création, puis à
tout moment par le bouton **Adresse** du webhook, avec Copier et un exemple
`curl`. **Régénérer** donne une nouvelle adresse et ferme l'ancienne. Un
webhook créé en v2026.11.21 ou v2026.11.21.1, dont le proxy ne gardait
qu'un condensé, doit être régénéré une fois pour que son adresse se
réaffiche. L'adresse vaut droit
d'écrire dans l'historique du compte et de dépenser ses tokens : il faut la
garder comme une clé.

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
| `thread_ts` | Repris dans la réponse envoyée au webhook sortant : elle part dans le même fil Slack |
| `response_url` | Commande slash : la réponse y est aussi postée (`response_type: in_channel`). En `https`, vers une adresse publique seulement, même avec `[webhooks].allow_private` |

### La réponse

Comme Slack, l'appel reçoit `200 ok` tout de suite. Le modèle répond
ensuite, en tâche de fond :

1. le message entre dans la conversation, marqué comme reçu par le
   webhook, avec une réponse **en cours** ;
2. le proxy envoie au modèle **le seul message reçu**, précédé du prompt
   système (celui du webhook, sinon celui de la conversation) : jamais les
   tours précédents, même dans une conversation « toujours la même ».
   Chaque message est une demande à part ; la conversation sert à les
   ranger et à les relire (si l'on y écrit ensuite depuis la page, la page
   reprend, elle, tout l'historique). La requête passe par
   `/ui/chat/completions` au nom du compte qui a créé le webhook : ses
   statistiques, sa part du partage équitable ;
3. la réponse prend la place de celle en cours, puis part vers le webhook
   sortant choisi, convertie en mrkdwn. Une erreur (backend hors ligne…)
   part aussi, préfixée « Erreur : ».

En **passe plat**, rien n'est rangé : le modèle reçoit le seul message (et
le prompt système du webhook), et sa réponse va au sortant. Le tour est
incognito : ni échange d'outils gardé, ni dépôt gardé
([Dépôts](depots.md#ce-qui-est-gardé-et-combien-de-temps)). Deux effets
de l'incognito : l'outil `memory` n'y est pas offert, même coché, et un
fichier produit par un outil (une image, un graphique) n'est lisible que
15 minutes (`[files].incognito_ttl`), y compris par son lien envoyé au
sortant. Le sortant d'un passe plat ne peut pas être supprimé (409) tant
que le passe plat s'en sert.

Deux messages d'un webhook « toujours la même conversation » sont traités
l'un après l'autre (rangés dans l'ordre) ; le second ne reçoit pas le
premier. Si le tour
échoue en route (historique plein, panne), l'erreur part quand même au
sortant et au `response_url`, et la réponse en cours devient cette erreur :
rien ne reste « en cours ». Une conversation supprimée dans la page pendant
le tour n'est pas recréée pour la seule réponse, et une question retirée
(message modifié) ne reçoit pas de réponse ajoutée à la fin. Supprimée
entre deux messages, la conversation d'un webhook « toujours la même » est
remplacée par une nouvelle, dédiée.

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

Un nom, une URL compatible Slack et, au choix, un salon : Slack (`https://hooks.slack.com/services/…`),
Mattermost, Rocket.Chat, Discord (URL du webhook suivie de `/slack`)… Le
proxy y poste :

```json
{"text": "<message en mrkdwn>", "channel": "#alertes"}
```

Le **salon** est facultatif. Défini, il part dans `channel` : `#alertes`,
`@quelqu'un`, ou le nom que le service attend (`town-square` pour
Mattermost). Vide, le message va dans le salon de l'URL. Mattermost,
Rocket.Chat et les anciens webhooks Slack le suivent. Un webhook d'app
Slack l'ignore et poste toujours dans le salon choisi à sa création :
pour un autre salon, il faut une autre URL, donc un autre sortant.

Le Markdown du modèle est converti : `**gras**` devient `*gras*`,
`[texte](url)` devient `<url|texte>`, un titre devient une ligne en gras, une
puce devient `•`. Le code passe tel quel. Un texte de plus de 39 000
caractères est coupé.

La page ne relit jamais l'URL, qui vaut droit d'écrire : elle n'en montre
que l'hôte et la fin. **Essayer** envoie un message d'essai.

L'envoi suit les règles réseau des outils : les refus de `[tools.net]`
(`blocked_urls`, `blocked_domains`), les adresses publiques seulement. Une
redirection n'est pas suivie : c'est un échec, à corriger dans l'URL
(`https`, slash final). Pour un Mattermost du réseau interne, il faut
`[webhooks].allow_private = true`. Un sortant ne peut pas viser un webhook
entrant d'un proxy (`/hooks/wh-…`) : chaque réponse deviendrait un
nouveau message.

### L'outil `webhook`

Quand les webhooks sont actifs, le chat propose l'outil **Webhooks** (menu
Outils). Il ne voit que les sortants **exposés à l'outil** (case du
formulaire, décochée par défaut) : un sortant décoché ne sert qu'aux réponses
des webhooks entrants. Le modèle l'appelle avec un texte et, si le compte en a plusieurs,
le nom d'un sortant : « poste le résumé sur Équipe ». Son prompt lui
interdit de l'appeler de lui-même. Il ne voit jamais les URL : seulement
les noms des sortants exposés, rendus sur erreur (un nom inconnu, ou
plusieurs sortants sans nom donné).

Comme tout outil hébergé, il est aussi offert à un client de l'API qui le
déclare (`{"type": "webhook"}` sur `/v1/chat/completions`), avec les
sortants du compte de sa clé.

## Sécurité

- Le jeton d'un entrant est la seule protection de `/hooks/<jeton>` : il
  ne doit circuler que vers le service qui l'utilise. Il est gardé en clair
  dans `data/conversations.db` (le bouton Adresse le réaffiche), comme les
  URL des sortants : une copie de la base, ou de sa sauvegarde, donne des
  adresses utilisables.
- Les réponses sont faites au nom du créateur. Le proxy vérifie, à chaque
  message, que son compte est actif (comptes OIDC) ou que sa clé est
  encore dans `proxy.api_keys`. Un webhook créé quand le proxy était ouvert
  cesse de répondre si le proxy devient fermé.
- Un message reçu est un texte venu de l'extérieur, que le modèle lit
  comme celui de l'utilisateur. Avec des outils actifs (`webhook`, code,
  mémoire), il peut leur faire faire ce que l'utilisateur pourrait
  demander. Il vaut mieux ne cocher que les outils utiles au webhook.
- Avec un `response_url`, l'appelant lit la réponse. Le modèle ne recevant
  que le message et le prompt système, l'appelant n'atteint pas
  l'historique de la conversation ; il atteint le prompt système et ce que
  les outils cochés lisent (un dépôt, le web, la mémoire) : le jeton vaut
  l'accès à ce que le webhook peut lire.
