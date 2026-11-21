# Dépôts

L'outil hébergé `repository` lit un projet d'une forge (GitLab, GitHub,
Gitea, Forgejo, ou tout dépôt git en HTTPS) : son arborescence, un fichier,
une recherche. **Rien n'est exécuté** : le proxy télécharge le projet et
répond aux commandes du modèle.

Cas typique : un [webhook entrant](webhooks.md) reçoit une alerte de
sécurité sur une image. Le modèle cherche dans le projet concerné où la
dépendance est épinglée (`requirements.txt`, `pyproject.toml`…), et répond
avec le correctif exact.

## Activer

```toml
[repos]
enabled = true
```

L'outil apparaît dans le menu Outils du chat (« Dépôts »), et dans les
outils qu'un webhook entrant peut cocher.

## Les sources

Une source, c'est une forge, son adresse, un jeton et, au choix, les
projets permis.

- **Par compte** : chacun ajoute les siennes dans *Configuration → Dépôts*,
  avec ses propres jetons. Le jeton n'est jamais réaffiché : à la
  modification, un champ vide le garde.
- **Partagées** : l'admin en déclare pour tous dans
  `[repos.sources.<nom>]`, typiquement avec un jeton d'organisation en
  `${VAR}`. Un compte qui crée une source du même nom la remplace pour lui.

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
permis.

## L'outil

`repository(command, project, source?, ref?, …)` :

| Commande | Paramètres | Rend |
|---|---|---|
| `tree` | `path` (un dossier, facultatif), `depth` (2 par défaut) | Les fichiers et leurs tailles ; au-delà de la profondeur, les dossiers et leur nombre de fichiers |
| `read` | `path` (le fichier), `offset`, `limit` (400 lignes par défaut) | Le fichier, lignes numérotées, et où reprendre |
| `grep` | `pattern` (une expression régulière Python), `path` (un dossier), `include` (un motif de noms : `*.py`, `requirements*.txt`) | `chemin:ligne: texte`, 200 résultats au plus |

- `project` : le chemin sur la forge, `groupe/sous-groupe/projet` ou
  `propriétaire/dépôt` ;
- `source` : le nom de la source. Facultatif s'il n'y en a qu'une. Un nom
  inconnu rend la liste des sources disponibles ;
- `ref` : une branche, un tag ou un commit. Absent, la branche par défaut.

Le premier appel télécharge le projet. Il est ensuite gardé en mémoire
`[repos].cache_ttl` secondes (30 minutes par défaut), par compte, source,
projet et référence : les commandes suivantes sont immédiates, et deux
commandes simultanées attendent le même téléchargement. Une source
modifiée (adresse, forge, jeton) ne sert pas l'ancien contenu. Une
conversation incognito ne garde rien : chaque commande retélécharge. Seuls les
fichiers réguliers sont gardés ; un fichier de plus de 2 Mo est listé, mais
ni lu ni cherché. Un fichier binaire n'est pas lu.

`grep` prend une expression régulière (sans casse) et rend chaque ligne
trouvée une fois, coupée autour de la correspondance, avec le même numéro
que `read`. Chaque recherche dans un fichier est bornée par
`[tools.web_grep].regex_timeout` (un fichier où elle déborde est dit « trop
lent » et sauté), la recherche entière par 10 secondes : une expression
trop coûteuse ne bloque pas le proxy.

Une archive n'a pas d'historique, et le `git fetch` n'en garde qu'un
commit, non lu. L'outil ne fait que lire : ni exécution, ni push.

## Tailles

- `[repos].max_bytes` (50 Mo) : l'archive téléchargée, compressée ;
- `[repos].max_unpacked_bytes` (200 Mo) : le projet décompressé ;
- `[repos].cache_bytes` (400 Mo) : tous les projets gardés, dans la RAM du
  proxy. Les moins récemment lus sortent d'abord.

## Sécurité

- Le jeton ne quitte pas le proxy : le modèle ne le voit jamais, ni la
  page. L'adresse d'une source ne peut pas contenir d'identifiants.
- Le jeton ne suit une redirection que vers l'origine de la source (même
  schéma, hôte et port). Pour la forge `git`, les redirections sont
  refusées. `git` tourne avec un environnement minimal (sans les
  variables de proxy HTTP) ; il ne fait pas de checkout, et le pack qu'il
  écrit est borné à `[repos].max_bytes` (`prlimit`).
- Une source qui change d'adresse ou de forge doit redonner son jeton :
  il ne part pas, sans être ressaisi, vers un autre hôte.
- Les forges passent par le téléchargement commun du proxy : adresse
  résolue puis épinglée, corps borné, listes de refus de `[tools.net]`
  (`blocked_urls`, `blocked_domains`) appliquées.
- **Aucune règle d'adresse** : une source peut viser une forge du réseau
  interne, et tout compte peut en créer une. Le proxy peut donc servir à
  joindre des machines internes en HTTP(S) GET, avec ce que le compte met
  dans l'adresse. C'est un choix de déploiement : ne pas activer `[repos]`
  là où les comptes ne sont pas de confiance.
- Le modèle ne choisit qu'une source (par son nom), un projet et une
  référence, vérifiés : un chemin de projet, des motifs `projects`, une
  référence sans `..` ni `-` en tête.
- Les jetons sont gardés en clair dans `data/conversations.db`, comme les
  URL des webhooks sortants : la sauvegarde de `data/` les contient.
