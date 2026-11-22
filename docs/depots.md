# Dépôts

L'outil hébergé `repository` lit un projet d'une forge (GitLab, GitHub,
Gitea, Forgejo, ou tout dépôt git en HTTPS) : son arborescence, un fichier,
une recherche. **Rien du projet n'est exécuté**, et son contenu est lu
**dans un bac de l'exécuteur de code**, pas dans le proxy.

Cas typique : un [webhook entrant](webhooks.md) reçoit une alerte de
sécurité sur une image. Le modèle cherche dans le projet concerné où la
dépendance est épinglée (`requirements.txt`, `pyproject.toml`…), et répond
avec le correctif exact. Avec l'outil `pull_request`, il peut aussi le
proposer en pull request : voir
[Proposer une modification](#proposer-une-modification-pull_request).

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

Le bac garde les deux dépôts les plus récents, pour tenir dans `/work`,
tant qu'il vit (voir
[Ce qui est gardé](#ce-qui-est-gardé-et-combien-de-temps)). Un bac neuf
n'a rien : le proxy y redépose l'archive de son cache, ou la retélécharge
si elle n'y est plus.

Le dépôt est décompressé dans un dossier caché, `/work/.repos/` :
l'exécuteur ne renvoie donc pas ses fichiers comme « fichiers produits ».
Seuls les fichiers réguliers sont écrits, sous le dossier du dépôt : les
liens, les chemins absolus et les `..` de l'archive sont ignorés.

## Les sources

Une source, c'est une forge, son adresse, un jeton et, au choix, les
projets permis.

- **Par compte** : chacun ajoute les siennes dans *Configuration → Dépôts*,
  avec ses propres jetons. Le jeton n'est jamais réaffiché : à la
  modification, un champ vide le garde. Il est redemandé si l'adresse
  (hôte, port) ou la forge change, pour ne pas partir, sans être ressaisi,
  vers un autre hôte.
- **Écriture** : une source du compte peut cocher *Écriture*, pour l'outil
  [`pull_request`](#proposer-une-modification-pull_request), et lister des
  *Chemins interdits*. Jamais une source partagée ni la forge `git`.
- **Partagées** : l'admin en déclare pour tous dans
  `[repos.sources.<nom>]`, typiquement avec un jeton d'organisation en
  `${VAR}`. Un compte qui crée une source du même nom (sans tenir compte de
  la casse) la remplace pour lui.

```toml
[repos.sources.entreprise]
forge = "gitlab"
url = "https://gitlab.exemple.fr"
token = "${GITLAB_TOKEN}"
projects = ["equipe/*", "outils/*"]
```

| Forge | Récupération | Jeton |
|---|---|---|
| `gitlab` | `GET <url>/api/v4/projects/<projet>/repository/archive.tar.gz` | `read_repository` (et `read_api`) |
| `github` | `GET api.github.com/repos/<projet>/tarball[/<ref>]`, ou `<url>/api/v3` pour un GitHub Enterprise | jeton fin, *Contents: read* |
| `gitea` | `GET <url>/api/v1/repos/<projet>/archive/<ref>.tar.gz` ; sans ref, la branche par défaut lue avant (Forgejo aussi) | lecture du dépôt |
| `git` | `git fetch --depth 1` de `<url>/<projet>.git`, puis `git archive` (une branche, un tag ou un commit) | en `Authorization: Basic <username>:<jeton>` ; `username` : `git` par défaut, `oauth2` pour GitLab, `x-access-token` pour GitHub |

`projects` borne ce que le modèle peut demander : des motifs à jokers,
sans casse (`equipe/*`). Vide, tous les projets que le jeton voit sont
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
bornée à une seconde par fichier (un fichier où elle déborde est dit
« trop lent » et sauté) et à 10 secondes en tout. Un fichier de plus de
2 Mo est listé, mais ni lu ni cherché ; un fichier binaire n'est pas lu.

Deux commandes simultanées sur le même projet attendent le même
téléchargement. Une source modifiée (adresse, forge, jeton) ne sert pas
l'ancien contenu.

Une archive n'a pas d'historique, et le `git fetch` n'en garde qu'un
commit. L'outil ne fait que lire : ni exécution du projet, ni push.
Proposer une modification est le rôle de
[`pull_request`](#proposer-une-modification-pull_request).

## Ce qui est gardé, et combien de temps

Le code d'un dépôt n'est gardé que le temps d'être lu :
`[repos].cache_ttl` (30 minutes par défaut) après sa dernière lecture, la
même durée dans le proxy et dans le bac. L'effacement est actif : il
n'attend pas l'appel suivant.

| Où | Quoi | Effacé |
|---|---|---|
| RAM du proxy (cache) | L'archive compressée | `cache_ttl` après sa dernière lecture (la purge passe chaque minute) ; plus tôt si d'autres archives la poussent dehors (`cache_bytes`, 32 au plus) ; au redémarrage du proxy |
| `/tmp` du proxy (forge `git`) | Le pack git | À la fin du téléchargement (dossier temporaire ; `/tmp` est en mémoire) |
| Bac de l'exécuteur | L'archive reçue | Juste après sa décompression |
| Bac de l'exécuteur | Le projet décompressé | Avec le bac, détruit après `cache_ttl` sans commande (le proxy demande cette inactivité à l'exécuteur, voir [Exécution de code](execution-de-code.md)) ; au redémarrage de l'exécuteur ; et, au-delà de deux dépôts, le plus ancien. L'inactivité compte pour tout le bac : un dépôt y reste tant que l'autre est lu |
| Partout | Tout ce que le compte a téléchargé | Tout de suite : à la modification ou à la suppression d'une source, et par le bouton **Oublier les dépôts téléchargés** (*Configuration → Dépôts*). Une commande déjà en route n'y remet rien. Si l'exécuteur ne répond pas, la page le dit : son bac expirera seul |

**Incognito et passe plat** : rien n'est gardé. Chaque commande dépose
l'archive dans un bac d'un seul appel, détruit après lui, et le cache ne la
garde pas. Le passe plat des [webhooks](webhooks.md) tourne en incognito.

**Ce qui suit la conversation**, et non le dépôt : les extraits que le
modèle a lus (résultats de `read` et `grep`) sont gardés avec les autres
échanges d'outils pour les tours suivants (`[chat].memory`, jusqu'à
`[tools].cache_ttl`, 24 h, en mémoire ; rien en incognito). Ce que le
modèle recopie dans sa réponse va dans l'historique du chat (effacé avec
la conversation, présent dans la sauvegarde de `data/`) et vers un webhook
sortant (un message posté sur Slack ou Mattermost ne se rappelle pas).

**Limite** : tout ce qui précède vit en mémoire (RAM, `tmpfs`), jamais sur
le disque du proxy ni de l'exécuteur. Ne plus y faire référence n'est pas
l'écraser, et le **swap** de l'hôte peut en garder une trace : sur la
machine qui héberge la pile, un swap chiffré, ou pas de swap.

## Proposer une modification (`pull_request`)

L'outil hébergé `pull_request` ouvre une pull request (une merge request
sur GitLab) **en brouillon** : une branche neuve, un commit, la PR. Rien
n'est fusionné : un humain relit. Avec un webhook entrant qui reçoit les
événements de la forge ([Corps d'une forge](webhooks.md#corps-dune-forge)),
une issue ouverte arrive au modèle, qui lit le projet avec `repository` et
propose le correctif.

```toml
[repos]
enabled = true
pull_requests = true
```

Puis, dans *Configuration → Dépôts*, cocher **Écriture** sur une source du
compte, avec un jeton qui peut écrire. L'outil apparaît dans le menu Outils
du chat (« Pull requests ») et dans les outils d'un webhook entrant. Son
prompt demande au modèle de ne l'appeler que si l'utilisateur, ou le
message reçu, demande une modification.

| Forge | Appels | Brouillon | Jeton |
|---|---|---|---|
| `gitlab` | fichiers existants (`HEAD …/repository/files/<chemin>`), `POST …/repository/commits`, `POST …/merge_requests` | titre préfixé `Draft: ` | `api`, rôle Developer au moins |
| `github` | fichiers existants (`GET …/contents/<chemin>`), ref et commit de la base, `POST …/git/trees`, `…/git/commits`, `…/git/refs`, `POST …/pulls` | `draft: true` ; si l'offre ne le permet pas, une PR ordinaire au titre préfixé `[Draft] ` | jeton fin, *Contents* et *Pull requests* en écriture |
| `gitea` | fichiers existants (`GET …/contents/<chemin>`), `POST …/contents`, `POST …/pulls` ; Gitea 1.20 ou plus, Forgejo aussi | titre préfixé `WIP: ` | `write:repository` |

Les écritures passent par l'API de la forge, sans redirection suivie : le
jeton ne va qu'à l'adresse de la source. Seul un fichier ordinaire se
remplace (ni lien, ni sous-module), et seul un fichier présent sur la base
se supprime. Sur GitHub, un fichier remplacé est écrit sans bit
exécutable. Si la PR échoue après la création de la branche (un jeton
sans ce droit, par exemple), la branche est supprimée.

`pull_request(project, title, files, description?, base?, source?)` :

- `files` : des fichiers entiers, `{path, content}`, ou `{path, delete:
  true}`. Le prompt demande au modèle de relire le fichier d'abord ;
- `base` : la branche de départ et de destination. Absente, la branche par
  défaut ;
- `source` : facultatif quand une seule source permet l'écriture.

La branche s'appelle `[repos].pr_branch_prefix` (`llm-proxy/` par défaut),
suivi du titre réduit et d'un condensé du projet, de la base et des
fichiers. La même modification redemandée (même titre, mêmes fichiers),
par exemple après un webhook rejoué, retombe sur la même branche : déjà là,
elle est refusée. La description finit par une
mention ajoutée par le proxy. L'issue de l'appel est notée sur la source
(*Dernier usage*).

| Borne | Défaut |
|---|---|
| `[repos].pr_max_files` | 20 fichiers par PR |
| `[repos].pr_max_bytes` | 1 Mo de contenus par PR |
| `[repos].pr_per_hour` | 10 PR par compte et par heure glissante |
| Par réponse | une PR |
| Titre, description | 200 et 20 000 caractères |

**Chemins refusés**, avant tout appel à la forge : ceux qui sortent du
projet (`..`, `.git`), les points d'entrée usuels de la CI
(`.gitlab-ci.yml`, `.gitlab-ci/*`, `.gitlab/ci/*`, `.github/workflows/*`,
`.github/actions/*`, `.gitea/workflows/*`, `.forgejo/workflows/*`,
`.woodpecker*`, `.drone.yml`, `.circleci/*`, `Jenkinsfile`, `.travis.yml`,
`azure-pipelines.yml`, `bitbucket-pipelines.yml`, `.buildkite/*`,
`.tekton/*`), ceux de `[repos].pr_blocked_paths` et les *Chemins
interdits* de la source. Les motifs ignorent la casse ; `*` couvre aussi
`/`. Un projet GitLab dont la CI vit ailleurs (*CI/CD configuration file*)
doit ajouter ce chemin à ses *Chemins interdits*.

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
  archive piégée ou une expression catastrophique ne coûte que du temps
  de bac.
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
- **Écrire** : `pull_request` ne fait qu'une proposition. La branche est
  neuve et la PR en brouillon. Rien n'est écrit sur une branche existante,
  et rien n'est fusionné. Le modèle peut y être poussé par une injection
  de prompt (un message reçu, un fichier lu). La relecture protège ce qui
  est fusionné, pas ce qui est publié : une branche et une PR sont
  visibles de qui voit le projet dès leur création.
- **Un événement de forge** (une issue d'un dépôt public, écrite par
  n'importe qui) : pendant sa réponse, `repository` et `pull_request`
  n'ouvrent que le projet de l'événement, sur sa forge
  ([Corps d'une forge](webhooks.md#corps-dune-forge)). Un message
  ordinaire reçu par un webhook n'a pas cette borne : ne cocher
  `pull_request` que sur un webhook dont les messages viennent de sources
  de confiance.
- **La CI** : une branche poussée déclenche la CI, avec les secrets du
  projet. Les points d'entrée usuels sont refusés, mais pas les fichiers
  qu'ils incluent ou appellent (`include: local`, un script lancé par la
  CI). Côté forge, ne pas donner de secrets aux pipelines des branches de
  `[repos].pr_branch_prefix` : variables protégées de GitLab,
  environnements de GitHub.
- Les jetons sont gardés en clair dans `data/conversations.db` : voir
  [Secrets gardés en clair](securite.md#secrets-gardés-en-clair).
- Le conteneur du proxy tourne en lecture seule, sans capacité Linux (voir
  [Déploiement](deploiement.md#durcissement-du-conteneur)).
