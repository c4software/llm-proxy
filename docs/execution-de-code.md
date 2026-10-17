# Exécution de code

L'outil hébergé `code_execution` fait tourner un programme écrit par le
modèle. Il lui rend le code de sortie, la sortie et les fichiers produits.
Le proxy n'exécute rien lui-même : un service à part, `executor`
(`services/executor/`), non exposé, est le seul à avoir un moteur de
conteneurs. L'outil est **désactivé par défaut**, et le proxy sert sans
lui.

```text
client ─▶ proxy ──HTTP + jeton──▶ executor ──podman exec──▶ bac (sans réseau)
            │   réseau interne        │
            └─ liens /v1/files ◀──────┘ code de sortie, sortie, fichiers
```

## Mise en route

1. Lire [Ce que le bac à sable vaut](#ce-que-le-bac-à-sable-vaut).
2. Ajouter le service `executor` par l'override
   ([Le compose de base et son override](#le-compose-de-base-et-son-override)) :
   il n'est pas dans le `docker-compose.yml` du dépôt.
3. Suivre la [procédure de validation](#valider-lexécuteur-sur-le-déploiement),
   qui finit par poser `enabled = true` et `[files] public_url`.

Déclarer l'outil :

- sur `/v1/chat/completions` : `{"type": "code_execution"}` dans `tools`,
  ou `always = ["code_execution"]` ;
- en appel direct : `POST /v1/tools/code_execution` (un bac par appel, les
  fichiers en liens dans `files`) ;
- pas sur `/v1/responses` ni `/v1/messages`.

### Réglages

Dans `data/config.toml`, table `[tools.code_execution]` sauf mention :

| Réglage | Défaut | Rôle |
|---|---|---|
| `enabled` | `false` | Active l'outil |
| `timeout` | 30 s | Délai par programme, **compilation comprise** |
| `packages` | `false` | Dit au modèle qu'il peut installer des paquets (voir [Installer des paquets](#installer-des-paquets)) |
| `max_files` | 8 | Fichiers d'entrée par appel ; `0` retire le paramètre `files` |
| `max_file_bytes`, `max_files_bytes` | 20 Mo, 40 Mo | Taille d'un fichier d'entrée, du total |
| `download_timeout` | 60 s | Délai pour télécharger tous les fichiers d'entrée |
| `[files].public_url` | — | Adresse sous laquelle le **navigateur** joint le proxy, sans `/` final. Sans elle, aucun fichier n'est remis, et le modèle le sait |

Côté exécuteur, dans `docker-compose.override.yml` :

| Variable | Défaut | Rôle |
|---|---|---|
| `SANDBOX_IDLE` | 3 h | Expiration d'un bac sans appel |
| `SANDBOX_MAX_SESSIONS`, `…_PER_CLIENT` | 8, 4 | Bacs en tout, par clé |
| `SANDBOX_MAX_INPUTS`, `SANDBOX_MAX_INPUT_BYTES`, `SANDBOX_MAX_INPUT_TOTAL_BYTES` | comme le proxy | Bornes des fichiers d'entrée, rejugées par l'exécuteur : les monter ensemble |
| `SANDBOX_MEMORY`, `SANDBOX_TMP_SIZE` | 512 Mo, 128 Mo | Mémoire d'un bac, tmpfs `/tmp` (compté dedans) |
| `SANDBOX_TIMEOUT` | — | Délai côté exécuteur, à monter avec `timeout` |
| `SANDBOX_EGRESS`, `SANDBOX_EGRESS_HOSTS`, `SANDBOX_EGRESS_MAX_BYTES`, `SANDBOX_DEVBOX_SIZE` | voir [Installer des paquets](#installer-des-paquets) | |

## Fonctionnement

### Un bac par conversation

- Les fichiers de `/work` et `/tmp` restent d'un appel au suivant. Les
  variables non : chaque appel est un processus neuf.
- La conversation est reconnue par la
  [mémoire des échanges cachés](outils-heberges.md#client-chatcompletions--déclarer-loutil)
  (`[chat].memory`). Elle garde son bac tant que le client renvoie ses
  réponses inchangées. Sinon, ou par `/v1/tools`, le bac est neuf.
- Un bac expire après 3 h sans appel, 4 h au plus, ou quand il faut de la
  place (8 bacs, 4 par clé ; le moins récemment utilisé part). Le modèle
  est prévenu que le suivant est vide.

### Le système d'un bac

La base de [dev-box](https://github.com/c4software/dev-box) : Arch Linux,
sans ce qui ne sert pas à un bac (ni Tailscale, ni sshd, ni podman, ni
éditeurs). Rien n'est figé : `archlinux:latest` et les scripts de dev-box
sont tirés à chaque construction.

Préinstallé :

- le shell : bash, jq, sqlite3, zip, gawk… ;
- Python (celui d'Arch) et ses bibliothèques de données : numpy, pandas,
  scipy, sympy, scikit-learn, networkx, matplotlib, seaborn, pillow,
  openpyxl, python-docx, pypdf, reportlab
  (`services/executor/sandbox/requirements.txt`) ;
- Node.js et npm, PHP et composer, mise.

Le reste s'installe à la demande si la
[sortie vers les dépôts](#installer-des-paquets) est ouverte ; sinon, le
bac n'a que sa base, et le modèle le sait. Pour ajouter un outil pour tous :
`services/executor/Dockerfile`, puis reconstruire l'image.

L'image est construite par la CI (`.github/workflows/executor.yml`) à chaque
changement de `services/executor/`, le 1er du mois ou à la demande, et
publiée en `ghcr.io/c4software/llm-proxy-executor` (`latest`, date,
commit). Mettre à jour, puis rejouer la validation :

```bash
docker compose pull executor && docker compose up -d executor
# ou construire sur place :
docker compose build --pull executor
```

### Langages

| `language` | Remarque |
|---|---|
| `python`, `bash`, `javascript` (Node.js) | Toujours offerts |
| `php` | Le programme commence par `<?php` |
| `go`, `rust` | Seulement avec la sortie, une fois installés par `devbox dev-env` |

C et C++ ne sont pas offerts (mise n'installe pas gcc). Pour Go et Rust, le
modèle donne **un fichier source** : l'exécuteur le compile sous `/tmp` et
l'exécute, dans le délai de l'appel. Ni le source ni le binaire ne sont des
fichiers produits. Pour un projet à plusieurs fichiers (`go build`, cargo),
passer par `bash`.

Une première compilation Go dans un bac prend environ 8,6 s (cache de la
bibliothèque standard), 0,6 s ensuite.

### Fichiers produits

Les fichiers écrits dans `/work` sont remis au client par un lien que le
proxy ajoute à la fin de la réponse. Une image s'affiche, le reste se
télécharge. Le modèle n'écrit pas ces liens.

| Borne | Valeur |
|---|---|
| Côté exécuteur | 8 fichiers, 5 Mo chacun, 10 Mo par appel |
| Côté proxy | 10 Mo par fichier, 128 Mo en tout, 4 h |

Le magasin du proxy est en mémoire vive : un redémarrage du proxy casse les
liens.

### Fichiers d'entrée (`files`)

Le bac n'a pas de réseau. C'est donc le **proxy** qui télécharge ce que le
modèle veut traiter (CSV, tableur, PDF, image), et l'exécuteur qui le
dépose dans `/work` avant le programme.

```json
{"language": "python", "code": "import pandas as pd\nprint(pd.read_csv('iris.csv').describe())",
 "files": [{"url": "https://exemple.org/donnees/iris.csv"}]}
```

- **Téléchargement** : par `net.download`, comme `web_fetch`. Adresses
  publiques seulement, listes de `[tools.net]` et du client, vérifiées à
  chaque redirection ; 4 de front. Un fichier au-delà des bornes est
  refusé, jamais coupé.
- **Nom dans le bac** : le `name` donné par le modèle ; sinon le dernier
  élément de l'URL (`…/iris.csv` → `iris.csv`) ; sinon celui de
  `Content-Disposition` ; sinon `file` et l'extension du type annoncé. Le
  nom est assaini (ni chemin, ni espace, ni point ou tiret en tête) et
  dédoublonné (`iris-2.csv`). Un fichier du même nom déjà dans le bac est
  remplacé.
- **Dépôt** : une archive `tar` construite en mémoire, dépliée par l'entrée
  standard de `podman exec`, sous l'uid du bac, sans chemin de l'hôte. Un
  lien symbolique laissé par un programme précédent est **remplacé, pas
  suivi**. Un fichier coupé par un `/work` plein (256 Mo) est retiré, et
  c'est dit.
- **Échec partiel** : le programme tourne avec ce qui est entré, et le
  modèle apprend ce qui manque et pourquoi. Si **aucun** fichier n'entre,
  rien n'est exécuté et l'outil rend l'erreur du refus (`not_allowed`,
  `not_accessible`, `unsupported`…).
- Les fichiers entrés restent dans le bac de la conversation. Ils ne
  repartent pas chez l'utilisateur, sauf si le programme les a
  **modifiés** : ils deviennent alors des fichiers produits.
- Pour seulement lire une page ou un document, le modèle est orienté vers
  `web_fetch` et `ocr`.
- Un exécuteur trop ancien lance le programme sans les fichiers ; le modèle
  lit « the sandbox service is too old to receive files ». Reconstruire
  l'image.

### Erreurs

Un programme en erreur est un résultat, pas une erreur de l'outil : le
modèle lit `Exit code: 1` et la trace, et corrige. L'outil n'est en erreur
que s'il n'a pas pu exécuter :

| Erreur | Cause |
|---|---|
| `unavailable` | Outil non configuré, exécuteur éteint |
| `too_many_requests` | Tous les bacs occupés |
| `invalid_input` | Langage inconnu, par exemple |
| `not_allowed`, `not_accessible`, `unsupported`… | Aucun fichier d'entrée n'a pu entrer |

## Déroulé d'un appel

Tout ce qui touche au bac est dans `services/executor/executor/sandbox.py`.

1. **Le proxy** (`proxy/llm_proxy/tools/code_execution.py`) télécharge les
   `files`, puis appelle `POST http://executor:8080/v1/execute` sur le
   réseau interne `sandbox` (`Authorization: Bearer <EXECUTOR_TOKEN>`)
   avec `client` (condensé de la clé), `session` (la conversation),
   `language`, `code`, `timeout` et les fichiers d'entrée en base64.
2. **L'exécuteur trouve le bac** du couple (client, session), ou le crée
   par un `podman run` **sans root** : `--rootfs /opt/sandbox/rootfs`
   (partagé en lecture seule), `--read-only`, `--network none`, `/work` et
   `/tmp` en tmpfs bornés, un uid propre, `--cap-drop ALL`,
   `no-new-privileges`, bornes de cgroups et de `ulimit`, `--init`,
   `--timeout` (la durée de vie). Le bac vit sur `sleep infinity`.
3. **Les fichiers d'entrée** sont déposés dans `/work` (voir plus haut).
4. **Le programme** :
   `podman exec --interactive --workdir /work <bac> sh -c 'touch .sbx_stamp && exec timeout -k 2 <délai> <interpréteur>'`.
   Le code arrive par l'entrée standard (`python3 -`, `bash -s`, `node -`,
   `php`). Pour Go et Rust, le source est compilé sous `/tmp`. `timeout`
   envoie TERM au délai, KILL 2 s après ; un bac qui ne répond plus est
   détruit. La sortie (stdout et stderr mêlés) est lue sous plafond, début
   et fin gardés.
5. **La récolte** : un `find` liste les fichiers de `/work` plus récents
   que `.sbx_stamp` (dossiers cachés et `node_modules` écartés).
   L'exécuteur choisit sous ses plafonds, et les fichiers sortent par une
   archive `tar` lue sur la sortie standard. Tout ce qui revient d'un bac
   est traité comme hostile : lu sous plafond, jamais interprété.
6. **Le retour** : code de sortie, sortie, fichiers, et si le bac est neuf
   ou perdu. Le proxy en fait un texte pour le modèle (« Exit code: …
   Output: … »), range les fichiers et ajoute leurs liens
   `/v1/files/<jeton>/<nom>` à la fin de la réponse.

## Installer des paquets

Fermé par défaut. Ouvert, le modèle peut lancer `pip install <nom>`,
`npm install -g <nom>`, `devbox dev-env <nom>`, et un projet cargo ou Go
peut tirer ses dépendances. **Rien d'autre** : le bac reste sans réseau.

```text
programme du bac ──127.0.0.1:3128──▶ relais du bac (forward.py)
      (sans réseau)                      │ prise Unix /run/egress.sock
                                         ▼
                      exécuteur : relais CONNECT (egress.py) ──HTTPS──▶ PyPI, npm,
                      juge l'hôte, l'adresse, le volume                crates.io, Go
```

### Activer

Trois réglages vont ensemble :

1. dans `docker-compose.override.yml`, `SANDBOX_EGRESS: "true"` ;
2. le réseau `sandbox-egress` de l'exécuteur (ces deux blocs sont
   commentés dans `docker-compose.override.example.yml`) ;
3. dans `config.toml`, `[tools.code_execution] packages = true`, pour que
   le modèle le sache.

`GET /v1/status` de l'exécuteur indique `egress.enabled` et la liste des
hôtes.

### Ce qui est permis

- **Le bac n'a toujours aucune interface** (`--network none`) : ni DNS, ni
  route. L'exécuteur lui monte une prise Unix. Dans le bac, un relais
  l'expose sur `127.0.0.1:3128`, l'adresse de `HTTPS_PROXY` : pip, npm,
  cargo et go y passent d'eux-mêmes.
- **L'exécuteur juge chaque connexion**, hors du bac :
  - `CONNECT` vers le port 443 seulement (ni HTTP en clair, ni autre port) ;
  - vers un hôte de la liste ; le nom est résolu par l'exécuteur, et toutes
    ses adresses doivent être publiques.
- **Liste par défaut** : `pypi.org`, `files.pythonhosted.org`,
  `registry.npmjs.org`, `index.crates.io`, `static.crates.io`,
  `crates.io`, `proxy.golang.org`, `sum.golang.org`, Packagist, et les
  sources de mise pour `devbox dev-env` : ses listes de versions,
  **GitHub** (releases), nodejs.org, static.rust-lang.org, dl.google.com,
  Adoptium, Deno, Zig, .NET, Ruby, Clojure, Maven. La changer avec
  `SANDBOX_EGRESS_HOSTS` (`*.domaine` pour des sous-domaines).
- **Bornes par bac** : 500 Mo dans les deux sens, 16 connexions
  simultanées, 60 s d'inactivité.
- **Journal** : chaque refus, et le bilan de chaque bac à sa destruction
  (tunnels, refus, octets, hôtes).

### `devbox dev-env`

Installe une recette de langage de dev-box par mise, dans le tmpfs
`/opt/devbox` du bac, shims dans le `PATH`. `devbox` dans un bac ne connaît
que `dev-env`.

- Disponibles : java, go, rust, python, node, ruby, dotnet, deno, bun,
  zig, scala, clojure, php.
- Écartées : android, ios, desktop, gpu, browser, media, network, flutter
  (inutiles sans écran ni matériel) ; swift, db-clients (passent par
  pacman) ; laravel, symfony, phoenix, ansible (cadriciels) ; elixir
  (Erlang se compile).
- Une installation dure jusqu'à une minute : monter `SANDBOX_TIMEOUT` et
  `[tools.code_execution].timeout` ensemble.

### Où vont les paquets

Sous `/tmp` (pip pour l'utilisateur, `npm -g` avec `NODE_PATH`, caches
cargo et Go), jamais dans `/work` : un paquet n'est pas un fichier produit.
Ils restent pour la conversation et partent avec le bac. `/tmp` est compté
dans la mémoire du bac : agrandir `SANDBOX_TMP_SIZE` et `SANDBOX_MEMORY`
ensemble (768m et 1536m suggérés).

### Ce que cela coûte

- L'exécuteur a une sortie : un programme qui s'évaderait d'un bac serait
  dans un conteneur connecté.
- Un dépôt de paquets sert n'importe quoi, et GitHub aussi. Un modèle mal
  guidé, ou une page qu'il a lue, peut installer un paquet malveillant. Il
  ne sort pas du bac, mais y tourne avec ce que le bac contient.
- Les tunnels sont chiffrés de bout en bout : le relais ne voit que l'hôte.
- Publier vers un dépôt exige un jeton que le bac n'a pas, sauf s'il l'a
  reçu dans le prompt. **N'ouvrez pas cette sortie à un modèle à qui l'on
  confie des secrets.**

## Ce que le bac à sable vaut

Activer `code_execution`, c'est laisser tourner sur cette machine du code
que personne n'a relu. Il est écrit par un modèle, donc dicté au besoin
par une page web qu'il vient de lire. L'isolation et les bornes ont été
constatées sur le déploiement (`validate.py`, 0 échec). Les réserves
suivantes restent entières.

- **Le noyau est partagé.** Un bac est un conteneur, pas une machine
  virtuelle. Une faille du noyau exploitable sans privilège donne l'hôte.
  Tenir le noyau à jour est la première défense, et il n'y en a pas de
  seconde de cette nature.
- **Deux enceintes, la seconde desserrée.**
  - Le programme tourne dans un bac podman sans root : pas de réseau,
    racine en lecture seule, aucune capacité, `no-new-privileges`, profil
    seccomp de podman, uid sans droit. Vu dans un bac : seule interface
    `lo`, « Network is unreachable », `CapEff: 0`, `NoNewPrivs: 1`,
    `Seccomp: 2`.
  - Ce bac tourne dans le conteneur `executor`. Pour faire marcher podman,
    celui-ci renonce à son profil seccomp, à AppArmor, au masquage de
    `/proc` et à `no-new-privileges`, et a son cgroup en écriture.
  - Qui s'évade d'un bac se trouve donc dans un conteneur Docker **moins**
    étanche qu'un conteneur ordinaire. Il y est sans capacité
    d'administration, sans volume, sans secret hormis le jeton de
    l'exécuteur, sans route vers l'extérieur, sans binaire setuid
    (`newuidmap` et `newgidmap` tirent leur droit de capacités de fichier).
- **Les bornes par bac ne tiennent que si le cgroup est délégué.**
  - Sans délégation, podman **accepte** `--memory`, `--cpus` et
    `--pids-limit` mais ne borne rien : 1 Go alloué dans un bac à 512 Mo,
    observé.
  - Avec `security_opt: writable-cgroups=true` (Docker 28 et plus, posé
    dans l'override), le point d'entrée délègue le cgroup à l'utilisateur
    `executor`. Les trois bornes deviennent effectives : la même
    allocation est tuée (code 137), la bombe de processus arrêtée à 124.
  - La sonde de démarrage vérifie l'effet, pas l'acceptation. Elle le dit
    dans le journal, `GET /v1/status` et `validate.py` (« NON BORNÉ »).
  - Tiennent dans tous les cas : la durée d'un programme, le nombre de
    processus par bac (`RLIMIT_NPROC`), la taille de ses disques en
    mémoire.
  - Sans délégation, le reste repose sur le **plafond du conteneur
    `executor`** (`mem_limit`, `cpus`, `pids_limit`), à régler sur la
    machine. Un programme peut épuiser ce plafond, faire tuer les autres
    bacs, voire redémarrer l'exécuteur, mais pas l'hôte.
  - Un bac tué par sa borne mémoire peut être recréé à l'appel suivant :
    il repart vide.
- **Un programme peut laisser un processus derrière lui.** Détaché
  (`setsid`), il échappe au délai de l'appel. Il tourne jusqu'à la fin de
  son bac (3 h sans appel, 4 h au plus), dans les bornes du bac, et côtoie
  les appels suivants de la conversation. Il ne sort pas du bac.
- **Le réseau interne joint le proxy.** Depuis le conteneur `executor`
  (pas depuis un bac, qui n'a que `lo`), le proxy est joignable. Sur un
  proxy **sans** `proxy.api_keys`, un programme évadé pourrait appeler
  `/v1/tools/web_fetch` et sortir par là. **Posez des clés.**
- **Les fichiers sont un canal de sortie.** Ce qu'un bac écrit dans
  `/work` est publié par le proxy, depuis son origine, à qui a le lien,
  sans clé. Un programme dicté par une injection peut y mettre le contenu
  de son bac, donc de la conversation. Seules les images matricielles
  s'affichent ; le reste se télécharge, et reste un fichier écrit par un
  programme non relu.
- **Un fichier d'entrée est un contenu du web.** Il ne s'exécute pas de
  lui-même : c'est le programme du modèle qui le lit, dans le bac. Mais ce
  que le programme en imprime revient au modèle. Une cellule de CSV ou un
  texte de PDF peut porter une injection de prompt, comme une page lue par
  `web_fetch`, avec ici un outil d'exécution à portée de main et les
  fichiers produits pour canal de sortie. Le téléchargement part du proxy :
  seul `[tools.net]` (adresses publiques, `allowed_domains`,
  `blocked_domains`) dit ce qu'il peut joindre.
- **Entre clients.** Les bacs sont cloisonnés par clé du proxy, ou par
  compte avec les [comptes OIDC](tableau-de-bord.md#comptes-oidc) (les clés
  d'un compte partagent ses bacs et leur plafond). Sur un proxy ouvert,
  tous les clients sont un seul client : seul l'identifiant de
  conversation, interne au proxy, sépare leurs bacs, et qui rejoue à
  l'identique la conversation d'un autre retrouve son bac. Les 8 bacs et
  les 128 Mo de fichiers sont communs : un client actif évince les bacs
  inactifs des autres.
- **Hors d'atteinte** : les secrets du proxy (clés des backends, `data/`)
  ne sont ni dans l'exécuteur ni à sa portée. Aucun socket Docker n'est
  monté nulle part.

**N'activez pas l'outil sur un proxy ouvert à des inconnus.** Ne le
présentez pas d'office (`[chat].always`) à des clients qui lisent le web
sans l'avoir pesé : le réglage vaut pour toutes les clés.

## Le compose de base et son override

La pile tient en deux fichiers :

| Fichier | Contenu |
|---|---|
| `docker-compose.yml`, versionné | Le proxy et SearXNG. Ne pas le modifier : un `git pull` le remplace sans conflit |
| `docker-compose.override.yml`, **non versionné** | Le facultatif et le propre à la machine. Docker Compose le lit tout seul et **fusionne** les deux |

Le dépôt livre un modèle commenté ligne à ligne :

```bash
cp docker-compose.override.example.yml docker-compose.override.yml
```

puis n'y garder que les blocs voulus.

**Bloc 1, l'exécution de code.** Trois morceaux qui vont ensemble : le
proxy rejoint le réseau `sandbox` (en gardant `default`), le service
`executor`, et le réseau `sandbox` en `internal`. Copier ce bloc met le
service en route : ni profil compose ni drapeau. Il faut en plus
`EXECUTOR_TOKEN` dans `.env`, puis `[tools.code_execution]` et `[files]`
dans `data/config.toml`.

**Bloc 2, l'adaptation à la machine.** Des exemples commentés :
`container_name`, `ports: !reset []` derrière un reverse proxy du même
hôte, `extra_hosts` pour un backend que le DNS du conteneur ne connaît pas,
`extends` d'une base commune (redémarrage, journaux,
`no-new-privileges`). Une clé de l'override remplace celle de base pour
une valeur simple et s'y ajoute pour une liste. `!reset` vide une liste
héritée, `!override` la remplace (Docker Compose 2.24 et plus).

**Règle : `executor` n'étend PAS une base `no-new-privileges`.** Cette
option neutralise les capacités de fichier de `newuidmap` et `newgidmap` :
podman n'écrit plus la table d'uid de ses bacs (« newuidmap: write to
uid_map failed: Operation not permitted »). Et `extends` ajoute des
`security_opt`, il n'en retire pas. Le service recopie donc à la main ce
qu'il veut de la base (`restart`, `logging`). Le proxy et SearXNG peuvent
l'étendre.

Dans une pile à `include`, Compose ne cherche pas l'override tout seul :
nommer les deux fichiers.

```yaml
include:
  - path:
      - ./llm-proxy/docker-compose.yml
      - ./llm-proxy/docker-compose.override.yml
```

`docker compose config` montre le résultat de la fusion.

## Valider l'exécuteur sur le déploiement

Une validation ne vaut que pour la machine où elle a tourné. Sur une autre
machine (autre Docker, autre noyau, AppArmor actif, arm64), tout est à
refaire, et l'outil ne s'active que sur un verdict « activable ».

Tout se fait par `docker compose`, depuis le dossier de la pile. `$PROXY`
est l'URL du proxy, `$CLE` une clé de `proxy.api_keys`.

### 0. Préparer

```bash
cp docker-compose.override.example.yml docker-compose.override.yml
```

Y garder le bloc 1, plus ce qu'il faut du bloc 2. Dans `.env` :
`EXECUTOR_TOKEN=` suivi de `openssl rand -hex 32`. Le `data/config.toml`
du déploiement n'est **pas** régénéré depuis l'exemple : y copier les
tables `[tools.code_execution]` et `[files]` de `data/config.example.toml`,
en laissant `enabled = false`.

```bash
docker compose config | less
docker compose config executor | grep -A6 security_opt
```

Attendu dans la pile fusionnée :

- le service `executor` ;
- `albert-proxy` sur les réseaux `default` **et** `sandbox` ;
- le réseau `sandbox` en `internal: true` ;
- pour `executor` : les trois `unconfined` et `writable-cgroups=true`, et
  **pas** `no-new-privileges` (s'il y est, le service étend une base qui
  le pose).

Si `executor` n'apparaît pas, l'override n'est pas lu : mauvais nom de
fichier, ou pile à `include` qui ne le nomme pas.

### 1. Construire

```bash
docker compose build albert-proxy
docker compose build executor 2>&1 | tee /tmp/build-executor.log
grep -E "bac à sable|podman version" /tmp/build-executor.log
```

Attendu : « bac à sable complet : Python 3.x, 15 modules, … commandes,
… sur l'entrée standard, devbox dev-env prêt », puis `podman version 5.x`.
En cas d'échec, le contrôle nomme ce qui manque. Image d'environ 2 Go ;
premier build de plusieurs minutes. Au-delà de ~100 Mo de cache de Go,
monter `SANDBOX_TMP_SIZE` ; au-delà de ~20 s pour Go, monter
`[tools.code_execution].timeout`.

### 2. Démarrer et lire le journal

```bash
docker compose up -d
sleep 30; docker compose ps; docker compose logs executor | tail -20
```

Attendu : `executor` « healthy », et la ligne « podman prêt : 0
orphelin(s) détruit(s) ; bornes de cgroups tenues : cpu, memory, pids ».
La sonde compare ce qu'un bac lit dans son propre cgroup avec et sans la
borne.

Si la ligne dit « AUCUNE » puis « bornes NON tenues par bac (cgroups non
délégués) » : `writable-cgroups=true` manque à `security_opt`, ou Docker
est antérieur à la version 28. Le service marche, mais seul le plafond du
conteneur retient les bacs (voir
[Ce que le bac à sable vaut](#ce-que-le-bac-à-sable-vaut)).

Si le journal dit « exécuteur HORS SERVICE : podman ne démarre aucun
conteneur : <message> » :

| Message | Cause probable |
|---|---|
| `newuidmap: write to uid_map failed: Operation not permitted` | `no-new-privileges` posé (extends d'une base) ; ou une image ancienne où `newuidmap` était le binaire setuid de Debian : reconstruire ; ou l'hôte n'offre pas les uid 100000–165535 (Docker sans root, userns-remap) |
| `cannot clone: Operation not permitted` | `seccomp=unconfined` manque |
| `mount proc … Operation not permitted` | `systempaths=unconfined` manque |
| `… apparmor … denied`, `mount … permission denied` | `apparmor=unconfined` manque |
| `catatonit … not found`, `executable file not found` | L'init : `init_path` dans `services/executor/containers/containers.conf` |
| Erreur sur `--rootfs`, `--tmpfs … exec`, `overlay`, `vfs` | Noter le message **entier** : un drapeau de `_create` dans `services/executor/executor/sandbox.py` à corriger |

Le proxy doit être « healthy » quoi que fasse l'exécuteur.

### 3. L'isolation, jugée

```bash
docker compose exec executor python -m executor.validate | tee /tmp/validate.log
```

`docker compose exec` entre en root ; le script repart seul sous
l'utilisateur `executor`. Il n'a besoin ni du jeton ni du proxy. Il juge
chaque cas `OK`, `ÉCHEC` ou `NON BORNÉ`, et finit par une ligne `VERDICT`.
Code de sortie 0 = aucun `ÉCHEC`.

| Cas | Attendu |
|---|---|
| 1. Python à froid | `OK` ; `uid 21000 cwd /work` ; `graphique.png` et `notes.txt` rendus, `gros.bin` écarté |
| 2. shell à chaud | `OK` : retrouve `notes.txt` |
| 3. sortie trop longue | `OK` : coupée |
| 4. délai dépassé | `OK` en ~3 s, `début` gardé |
| 5. le bac survit au délai | `OK` |
| 6. réseau | `OK` : « Network is unreachable », seule interface `lo` (demandée au noyau). **ÉCHEC = ne pas activer** |
| 6bis. sortie ouverte : pip | `OK` : un paquet PyPI s'installe par le relais |
| 6ter. sortie ouverte : site ordinaire | `OK` : refusé par le relais (403) |
| 6quater. `devbox dev-env go` | `OK` : Go installé par mise sous `/opt/devbox`, puis lancé |
| 6quinquies. go (installé) | `OK` deux fois, chacune en moins de 30 s ; le second `go` est celui d'un cache chaud |
| 7. bombe de processus | `OK` : fork refusé avant 128. ÉCHEC = ni `RLIMIT_NPROC` ni `--pids-limit` ne tiennent par bac |
| 8. mémoire | `OK` : l'allocation de 1 Go est tuée (code 137) dans un bac à 512 Mo. `NON BORNÉ` si le cgroup n'est pas délégué |
| 8bis. CPU | N'apparaît (`NON BORNÉ`) que si `--cpus` n'est pas tenu |
| 9. lecture seule, capacités | `OK` : 4 × `Read-only file system`, `CapEff: 0`, `NoNewPrivs: 1`, `Seccomp: 2`, `/work` plein avant 300 Mo. **ÉCHEC = ne pas activer** |
| 10. autre client, même session | `OK` : bac vide |
| 11. expiration | `OK` : 2 bacs détruits, bac neuf |
| 12. contenu du bac | `OK` : « bac à sable complet », hors ligne |
| 14. processus en arrière-plan | `NON BORNÉ` **attendu** : un processus détaché survit jusqu'à la fin du bac |
| 15. fichiers d'entrée | `OK` : `data.csv` et `notes.txt` déposés sous l'uid du bac (`True`, `0o644`) à la place de deux liens symboliques piégés (ni `piege.txt` ni `/tmp/sbx-piege.txt` n'existent) ; `../evade.txt` et `.cache` refusés (`invalid name`) ; rien à la récolte. **ÉCHEC = poser `SANDBOX_MAX_INPUTS: "0"`** et le signaler |
| 15bis. fichier d'entrée modifié | `OK` : `data.csv`, complété, revient comme fichier produit ; `notes.txt`, seulement lu, non |
| 16. aucun zombie | `OK` : le PID 1 du conteneur exécuteur est un init, aucun processus zombie ne reste |

Les cas 6bis à 6quinquies ne tournent qu'avec la sortie ouverte.

À relever : la ligne `VERDICT`, les deux lignes `MESURES` et les temps du
cas 6quinquies. Ordres de grandeur sur le déploiement de référence :

| Mesure | Valeur |
|---|---|
| bac à froid (création comprise) | 0,6 s |
| appel à chaud | 0,2 à 0,5 s |
| `import pandas` + `matplotlib` et un tracé | 1,4 s |
| première compilation Go dans un bac | 8,6 s (0,6 s ensuite) |
| `/tmp` après les compilations | 46 Mo sur 128 |

Si « à chaud » dépasse 1 s, le coût est dans `podman exec` lui-même.
Pendant le cas 8, dans un autre terminal : `docker stats --no-stream`.
L'exécuteur doit rester sous `mem_limit`. S'il redémarre, le plafond est
trop juste pour `SANDBOX_MAX_SESSIONS`.

Pas un échec : le `/sys/class/net` d'un bac montre les interfaces de
l'exécuteur (sysfs repris de son hôte), alors que le bac n'a que `lo`. Le
cas 6 interroge donc le noyau.

### 4. Le réseau interne

```bash
docker compose exec executor python -c "import urllib.request; urllib.request.urlopen('https://example.org', timeout=5)"   # doit ÉCHOUER
docker compose exec executor python -c "import socket; socket.create_connection(('searxng', 8080), 3)"                    # doit ÉCHOUER
docker compose exec albert-proxy python -c "import urllib.request; print(urllib.request.urlopen('http://executor:8080/healthz', timeout=5).read())"   # {"status":"ok"}
```

### 5. Activer, et un appel direct

Dans `data/config.toml` : `[tools.code_execution] enabled = true` et
`[files] public_url = "$PROXY"`. Puis `docker compose up -d albert-proxy`
et `docker compose logs albert-proxy | grep code_execution` : la ligne
« code_execution : exécuteur http://executor:8080 … », sans avertissement.

```bash
curl -s $PROXY/v1/tools -H "Authorization: Bearer $CLE" | jq '.data[].name'     # … "code_execution"
curl -s $PROXY/v1/tools/code_execution -H "Authorization: Bearer $CLE" -H 'Content-Type: application/json' -d '{
  "language": "python",
  "code": "import matplotlib.pyplot as plt\nplt.plot([1,4,9]); plt.savefig(\"courbe.png\")\nopen(\"t.csv\",\"w\").write(\"a,b\\n1,2\\n\")\nprint(\"fait\")"}' | tee /tmp/outil.json | jq
```

Attendu : `is_error: false`, deux liens `$PROXY/v1/files/<jeton>/…` dans
`files`, et `result` :

```text
Exit code: 0
Sandbox: single-use — nothing is kept after this call.
Files delivered to the user (shown with your answer; do not write links or paths to them):
- courbe.png (image/png, … kB)
- t.csv (text/csv, 8 bytes)
Output:
fait
```

Puis, **sans clé** :

```bash
curl -sI "$(jq -r '.files[0].url' /tmp/outil.json)"    # 200, image/png, inline, nosniff, CSP sandbox
curl -sI "$(jq -r '.files[1].url' /tmp/outil.json)"    # 200, application/octet-stream, attachment
curl -s -o /dev/null -w '%{http_code}\n' $PROXY/v1/files/jeton-invente/courbe.png   # 404
curl -s -o /dev/null -w '%{http_code}\n' $PROXY/v1/tools                             # 401 : le reste est sous clé
```

Si les deux premiers rendent 401 ou 404 alors que le proxy répond 200 de
l'intérieur : le reverse proxy ne route pas `/v1/files/`, ou `public_url`
est fausse. À provoquer aussi :

```bash
… -d '{"language":"cobol","code":"x"}'                          # is_error: true, error: invalid_input
… -d '{"language":"python","code":"raise SystemExit(3)"}'       # is_error: false, « Exit code: 3 »
… -d '{"language":"rust","code":"fn main() { println!(\"{}\", 6 * 7); }"}'   # « Exit code: 0 », 42
docker compose stop executor      # puis un appel : error: unavailable, « unreachable », en ~5 s
docker compose start executor
```

### 5bis. Un fichier d'entrée

L'image de l'exécuteur doit être à jour (`docker compose build executor &&
docker compose up -d executor`), sinon le résultat dit « the sandbox
service is too old to receive files ». Avec un CSV public et stable (150
lignes, environ 4 ko) :

```bash
CSV=https://raw.githubusercontent.com/mwaskom/seaborn-data/master/iris.csv
curl -s $PROXY/v1/tools/code_execution -H "Authorization: Bearer $CLE" -H 'Content-Type: application/json' -d '{
  "language": "python",
  "code": "import pandas as pd\ndf = pd.read_csv(\"iris.csv\")\nprint(df.shape)\nprint(df.groupby(\"species\").sepal_length.mean().round(3).to_dict())",
  "files": [{"url": "'$CSV'"}]}' | jq
```

Attendu : `is_error: false`, aucun lien dans `files`, et `result` :

```text
Exit code: 0
Sandbox: single-use — nothing is kept after this call.
Files copied into the working directory before the run:
- iris.csv (… kB), from https://raw.githubusercontent.com/mwaskom/seaborn-data/master/iris.csv
Output:
(150, 5)
{'setosa': 5.006, 'versicolor': 5.936, 'virginica': 6.588}
```

Dans le journal de l'exécuteur : « …, 1 déposé(s), 0 refusé(s) [bac
neuf] ». À provoquer aussi :

```bash
… "files": [{"url": "'$CSV'", "name": "fleurs.csv"}]            # déposé sous fleurs.csv : adapter read_csv
… "files": [{"url": "'$CSV'"}, {"url": "https://example.org/absent.csv"}]   # is_error: false ; « Files NOT copied — the program ran without them: … returned HTTP 404. »
… "files": [{"url": "http://executor:8080/healthz"}]            # is_error: true, error: not_allowed (adresse privée) ; rien n'est exécuté
… "files": [{"url": "http://169.254.169.254/latest/meta-data"}] # idem
… "code": "open(\"iris.csv\", \"a\").write(\"x\\n\")", "files": [{"url": "'$CSV'"}]   # modifié : iris.csv revient en lien dans `files`
```

### 6. Par un modèle

Avec `[chat].hosted_tools = true` et un modèle qui sait appeler des
outils :

```bash
curl -s $PROXY/v1/chat/completions -H "Authorization: Bearer $CLE" -H 'Content-Type: application/json' -d '{
  "model": "<backend>/<modèle>", "tools": [{"type": "code_execution"}],
  "messages": [{"role": "user", "content": "Trace sin(x) entre 0 et 10 dans un PNG, garde aussi les points dans /tmp/points.csv, et dis-moi sin(7) à 6 décimales."}]}' | tee /tmp/tour1.json | jq -r '.choices[0].message.content'
```

Attendu : `0.656987`, suivi de
`[![….png]($PROXY/v1/files/…)]($PROXY/v1/files/…)` ; aucun `tool_calls`
pour le client.

Puis la **conversation** : la même requête, suivie du message assistant
reçu **tel quel** (liens compris) et d'un message user « Ajoute cos(x) au
même graphique, à partir de /tmp/points.csv ». Attendu dans le journal du
proxy : « 1 échange(s) d'outils hébergés réinséré(s) » ; dans celui de
l'exécuteur : la seconde exécution **sans** « [bac neuf] ».

Un fichier d'entrée, par le modèle :

```bash
curl -s $PROXY/v1/chat/completions -H "Authorization: Bearer $CLE" -H 'Content-Type: application/json' -d '{
  "model": "<backend>/<modèle>", "tools": [{"type": "code_execution"}],
  "messages": [{"role": "user", "content": "Voici un jeu de données : https://raw.githubusercontent.com/mwaskom/seaborn-data/master/iris.csv — combien de lignes, et quelle est la longueur moyenne des sépales par espèce ? Calcule-le, ne devine pas."}]}' | tee /tmp/csv1.json | jq -r '.choices[0].message.content'
```

Attendu : 150 lignes ; setosa 5,006, versicolor 5,936, virginica 6,588 ;
aucun lien ajouté. Dans le journal de l'exécuteur, « 1 déposé(s) » : le
modèle a passé l'URL dans `files`. S'il a d'abord tenté `urllib` ou
`curl`, il a lu « Network is unreachable » et doit s'être corrigé (certains
modèles essaient aussi `web_fetch` avant d'y arriver).

Suite de la conversation, message assistant renvoyé tel quel : « Trace
maintenant un nuage de points longueur × largeur des pétales, coloré par
espèce ». Attendu : une image en lien, et la seconde exécution **sans**
« [bac neuf] », avec « 0 déposé(s) » (le fichier est resté dans le bac).

### 7. Interface de chat

Avec `always = ["code_execution"]`, depuis une interface qui ne déclare
pas d'outils : l'image s'affiche-t-elle dans la réponse ? La conversation
retrouve-t-elle son bac au tour suivant (l'interface renvoie-t-elle le
contenu avec les liens, inchangé) ? Le modèle de tâches de l'interface
doit être dans `always_except`.

### 8. Redémarrages

- `docker compose restart executor` avec un bac ouvert : au journal,
  « 1 orphelin(s) détruit(s) » ou 0 ; l'appel suivant de la conversation
  dit « Sandbox: new and empty ».
- `docker compose restart albert-proxy` : les liens d'avant rendent 404
  (magasin en mémoire), et la conversation repart d'un bac neuf.

### 9. Durcissements, à essayer en dernier

Rejouer `validate.py` après chacun :

- `cap_drop: [ALL]` avec `cap_add: [SETUID, SETGID]` sur `executor`, dans
  l'override. `newuidmap` et `newgidmap` n'ont besoin que de ces deux
  capacités. Le point d'entrée fait un `chown` sur le cgroup, qui pourrait
  demander `CHOWN` en plus (pas essayé) : la ligne « bornes de cgroups
  tenues » du journal le dira.
- Régler `mem_limit`, `cpus`, `pids_limit` et `SANDBOX_MAX_SESSIONS` sur la
  machine.

### État de la validation

Validé le 07/10/2026 sur le déploiement de référence (Docker 29.7.1,
Compose 5.4.0, noyau 6.1, cgroup v2 à pilote systemd, Debian sans AppArmor
actif) : étapes 0 à 6, `validate.py` à 0 échec avec le seul `NON BORNÉ`
attendu (cas 14), y compris les cas 15 et 15bis sur le vrai podman.

Pas encore joué : les liens ouverts dans un navigateur, une interface de chat (7), les
redémarrages (8), les durcissements (9), une machine arm64.
