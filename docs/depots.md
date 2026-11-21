# Dépôts

L'outil hébergé `repository` lit un projet d'une forge (GitLab, GitHub,
Gitea, Forgejo, ou tout dépôt git en HTTPS) : son arborescence, un fichier,
une recherche. **Rien du projet n'est exécuté**, et son contenu est lu
**dans un bac de l'exécuteur de code**, pas dans le proxy.

Cas typique : un [webhook entrant](webhooks.md) reçoit une alerte de
sécurité sur une image. Le modèle cherche dans le projet concerné où la
dépendance est épinglée (`requirements.txt`, `pyproject.toml`…), et répond
avec le correctif exact.

## Activer

```toml
[repos]
enabled = true

[tools.code_execution]      # l'exécuteur, où les dépôts sont lus
url = "http://executor:8080"
token = "${EXECUTOR_TOKEN}"
# enabled peut rester à false : l'outil code_execution n'est pas offert,
# l'exécuteur sert seulement aux dépôts.
```

Il faut le service `executor` de la pile (voir
[Exécution de code](execution-de-code.md) : le bloc 1 de
`docker-compose.override.example.yml`, et `EXECUTOR_TOKEN` dans `.env`).
Sans exécuteur configuré, l'outil n'est pas offert.

L'outil apparaît alors dans le menu Outils du chat (« Dépôts »), et dans
les outils qu'un webhook entrant peut cocher. Chaque compte déclare ses
sources dans *Configuration → Dépôts*.

## Comment un dépôt est lu

```text
modèle ──repository(command, project…)──▶ proxy
                                            │ 1. la source du compte (jeton)
                                            │ 2. l'archive : téléchargée, ou
                                            │    prise dans le cache (compressée,
                                            │    jamais ouverte par le proxy)
                                            ▼
                          exécuteur : le bac « repository » du compte
                          (sans réseau, sans jeton)
                            3. décompresse l'archive dans /work/.repos/<id>
                            4. tree / read / grep, par repository_bac.py
                                            │
modèle ◀────────────── le texte ────────────┘
```

Le travail est partagé pour que **le contenu d'un dépôt ne soit jamais
décodé dans le proxy** :

- **Le proxy** choisit la source, télécharge l'archive avec son jeton et la
  garde compressée en cache. Il ne l'ouvre pas.
- **Un bac de l'exécuteur**, un par compte (la session `repository`),
  reçoit l'archive comme un fichier d'entrée. Il la décompresse et répond à
  la commande. C'est le même conteneur `executor` que `code_execution` : sans
  réseau, sous ses bornes (mémoire, processus, délai, taille de `/work`).
  L'outil `code_execution` lui-même peut rester inactif.
- **Le jeton** ne quitte pas le proxy : ni le bac ni le modèle ne le voient.

Le bac garde ses dépôts (les deux plus récents, pour tenir dans `/work`)
tant qu'il vit, c'est-à-dire 30 minutes après sa dernière commande (voir
[Ce qui est gardé](#ce-qui-est-gardé-et-combien-de-temps)). Un bac neuf
n'a rien : le proxy y redépose l'archive de son cache, ou la retélécharge
si elle n'y est plus.

Le dépôt est décompressé dans un dossier caché, `/work/.repos/`.
L'exécuteur ne renvoie donc pas ses fichiers comme « fichiers produits ».
Seuls les fichiers réguliers sont écrits, chacun sous le dossier du dépôt :
les liens, les chemins absolus et les `..` de l'archive sont ignorés.

## Les sources

Une source, c'est une forge, son adresse, un jeton et, au choix, les
projets permis.

- **Par compte** : chacun ajoute les siennes dans *Configuration → Dépôts*,
  avec ses propres jetons. Le jeton n'est jamais réaffiché : à la
  modification, un champ vide le garde. Il est redemandé si l'adresse
  (hôte, port) ou la forge change, pour ne pas partir, sans être ressaisi,
  vers un autre hôte.
- **Partagées** : l'admin en déclare pour tous dans
  `[repos.sources.<nom>]`, typiquement avec un jeton d'organisation en
  `${VAR}`. Un compte qui crée une source du même nom (sans tenir compte de
  la casse) la remplace pour lui.

```toml
[repos.sources.pmw]
forge = "gitlab"
url = "https://gitlab.pmw.lan"
token = "${GITLAB_TOKEN}"
projects = ["playmoweb/*", "kid-ai/*"]
```

| Forge | Récupération | Jeton |
|---|---|---|
| `gitlab` | `GET <url>/api/v4/projects/<projet>/repository/archive.tar.gz` | `read_repository` (et `read_api`) |
| `github` | `GET api.github.com/repos/<projet>/tarball[/<ref>]`, ou `<url>/api/v3` pour un GitHub Enterprise | jeton fin, *Contents: read* |
| `gitea` | `GET <url>/api/v1/repos/<projet>/archive/<ref>.tar.gz` ; sans ref, la branche par défaut lue avant (Forgejo aussi) | lecture du dépôt |
| `git` | `git fetch --depth 1` de `<url>/<projet>.git`, puis `git archive` (une branche, un tag ou un commit) | en `Authorization: Basic <username>:<jeton>` ; `username` : `git` par défaut, `oauth2` pour GitLab, `x-access-token` pour GitHub |

`projects` borne ce que le modèle peut demander : des motifs à jokers,
sans casse (`playmoweb/*`). Vide, tous les projets que le jeton voit sont
permis. Une adresse ne peut pas contenir d'identifiants
(`https://user:jeton@…`) : le jeton a son propre champ.

## L'outil

`repository(command, project, source?, ref?, …)` :

| Commande | Paramètres | Rend |
|---|---|---|
| `tree` | `path` (un dossier, facultatif), `depth` (2 par défaut) | Les fichiers et leurs tailles ; au-delà de la profondeur, les dossiers et leur nombre de fichiers (300 lignes au plus) |
| `read` | `path` (le fichier), `offset`, `limit` (400 lignes par défaut) | Le fichier, lignes numérotées (15 000 caractères par appel, une ligne de plus de 2 000 caractères coupée), et où reprendre |
| `grep` | `pattern` (une expression régulière Python, sans casse), `path` (un dossier), `include` (un motif de noms : `*.py`, `requirements*.txt`) | `chemin:ligne: texte`, chaque ligne une fois, coupée autour de la correspondance ; 60 résultats au plus |

- `project` : le chemin sur la forge, `groupe/sous-groupe/projet` ou
  `propriétaire/dépôt` ;
- `source` : le nom de la source. Facultatif s'il n'y en a qu'une. Un nom
  inconnu rend la liste des sources disponibles ;
- `ref` : une branche, un tag ou un commit. Absent, la branche par défaut.

Les numéros de ligne de `grep` sont ceux de `read`. Une recherche est
bornée à une seconde par fichier : un fichier où elle déborde est dit
« trop lent » et sauté. La recherche entière est bornée à 10 secondes. Une
expression piégée ne coûte donc que du temps de bac, jamais du proxy. Un
fichier de plus de 2 Mo est listé, mais ni lu ni cherché ; un fichier
binaire n'est pas lu.

Deux commandes simultanées sur le même projet attendent le même
téléchargement. Une source modifiée (adresse, forge, jeton) ne sert pas
l'ancien contenu.

Une archive n'a pas d'historique, et le `git fetch` n'en garde qu'un
commit. L'outil ne fait que lire : ni exécution du projet, ni push.

## Ce qui est gardé, et combien de temps

Le code d'un dépôt n'est gardé que le temps d'être lu : **30 minutes après
sa dernière lecture** (`[repos].cache_ttl`), la même durée dans le proxy et
dans le bac, avec une suppression active (pas seulement au prochain appel).

| Où | Quoi | Effacé |
|---|---|---|
| RAM du proxy (cache) | L'archive compressée | 30 min après sa dernière lecture ; la purge passe chaque minute. Aussi : poussée dehors par d'autres archives (`cache_bytes`, 32 au plus), redémarrage du proxy |
| `/tmp` du proxy (forge `git`) | Le pack git | À la fin du téléchargement (dossier temporaire ; `/tmp` est en mémoire) |
| Bac de l'exécuteur | L'archive reçue | Juste après sa décompression |
| Bac de l'exécuteur | Le projet décompressé | Le bac est détruit 30 min après sa dernière commande (le proxy demande cette inactivité à l'exécuteur, `idle` : il faut l'exécuteur de la même version ; un plus ancien l'ignore, et le bac vit alors `SANDBOX_IDLE`, 3 h) ; au redémarrage de l'exécuteur ; et au-delà de deux dépôts, le plus ancien. Le bac compte l'inactivité de tous ses dépôts : un dépôt peut y rester tant que l'autre est lu |
| Partout | Tout ce que le compte a téléchargé | Tout de suite : à la modification ou à la suppression d'une source, et par le bouton **Oublier les dépôts téléchargés** (*Configuration → Dépôts*). Une commande déjà en route n'y remet rien. Si l'exécuteur ne répond pas, la page le dit : son bac expirera seul |

**Incognito et passe plat** : rien n'est gardé. Chaque commande dépose
l'archive dans un bac d'un seul appel, détruit après lui, et le cache ne la
garde pas. Le passe plat des [webhooks](webhooks.md) tourne en incognito :
pas non plus d'échange d'outils gardé.

**Ce qui suit la conversation**, et non le dépôt : les extraits que le
modèle a lus (résultats de `read` et `grep`) sont gardés avec les autres
échanges d'outils pour les tours suivants (`[chat].memory`, jusqu'à
`[tools].cache_ttl`, 24 h, en mémoire ; rien en incognito ni en passe
plat). Ce que le modèle recopie dans sa réponse va dans l'historique du
chat (effacé avec la conversation) et vers un webhook sortant (Slack,
Mattermost : il ne se rappelle pas). La sauvegarde de `data/` contient
l'historique.

**Limite** : tout ce qui précède vit en mémoire (RAM, `tmpfs`), jamais sur
le disque du proxy ni de l'exécuteur. Ne plus y faire référence n'est pas
l'écraser, et le **swap** de l'hôte peut en garder une trace : sur la
machine qui héberge la pile, un swap chiffré, ou pas de swap.

## Tailles

Les bornes du proxy et celles de l'exécuteur s'enchaînent ; il faut les
monter ensemble pour de gros dépôts :

| Borne | Défaut | Porte sur |
|---|---|---|
| `[repos].max_bytes` | 20 Mo | l'archive téléchargée, compressée |
| `SANDBOX_MAX_INPUT_BYTES` (exécuteur) | 20 Mo | la même archive, à son entrée dans le bac |
| `[repos].max_unpacked_bytes` | 100 Mo | le projet décompressé ; 100 000 entrées au plus |
| `SANDBOX_WORK_SIZE` (exécuteur) | 256 Mo | `/work` du bac, où vivent les deux dépôts gardés |
| `[repos].cache_bytes` | 200 Mo | toutes les archives gardées, dans la RAM du proxy |

## Sécurité

- **Le contenu est lu hors du proxy.** Décompression, lecture et
  recherche se font dans le bac, sans réseau et sous ses bornes. Une
  archive piégée ou une expression catastrophique n'atteint pas le proxy.
- **Seule exception : la forge `git`.** `git fetch` et `git archive`
  tournent dans le proxy, et git y lit le pack reçu. git tourne avec un
  environnement minimal (sans configuration de l'hôte, sans variables de
  proxy HTTP), sous `prlimit` (le pack est borné à `max_bytes`, toujours
  un pack : `fetch.unpackLimit 1`), sans checkout. Les hooks sont ceux du
  dépôt vide créé par le proxy, de simples `.sample` inactifs, et aucun
  filtre n'est configuré : git n'exécute rien du dépôt. Pour tout lire
  hors du proxy, préférer une forge à API (`gitlab`, `github`, `gitea`).
- **Le jeton** ne quitte pas le proxy, et ne suit une redirection que vers
  l'origine de la source (même schéma, hôte et port). Pour la forge `git`,
  les redirections sont refusées. Une erreur authentifiée ne montre pas la
  partie après `?` d'une URL, où GitHub met un jeton temporaire.
- **Les forges** passent par le téléchargement commun du proxy : adresse
  résolue puis épinglée, corps borné, listes de refus de `[tools.net]`
  (`blocked_urls`, `blocked_domains`) appliquées, aussi au fetch `git`.
- **Aucune règle d'adresse** : une source peut viser une forge du réseau
  interne, et tout compte peut en créer une. Le proxy peut donc servir à
  joindre des machines internes en HTTP(S) GET, avec ce que le compte met
  dans l'adresse. C'est un choix de déploiement : ne pas activer `[repos]`
  là où les comptes ne sont pas de confiance.
- **Le modèle** ne choisit qu'une source (par son nom), un projet et une
  référence, vérifiés : un chemin de projet (ni `.` ni `..`), des motifs
  `projects`, une référence sans `..` ni `-` en tête.
- **Ce que le dépôt dit au modèle** : un fichier peut contenir des
  instructions (injection de prompt), que le modèle peut suivre avec les
  autres outils cochés (poster sur un webhook sortant exposé, écrire dans
  sa mémoire, aller sur le web). Ne cocher, sur un webhook, que les outils
  utiles.
- Les jetons sont gardés en clair dans `data/conversations.db`, comme les
  URL des webhooks sortants : la sauvegarde de `data/` les contient.
- Le conteneur du proxy tourne en lecture seule, sans capacité Linux (voir
  [Déploiement](deploiement.md#durcissement-du-conteneur)).
