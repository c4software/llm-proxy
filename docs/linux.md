# La VM Linux du navigateur

Dans le [chat](chat.md) de la page, la famille **Linux** donne au modèle
une vraie machine Linux, qui tourne **dans le navigateur** : l'outil
`run_shell` y exécute des scripts bash, et l'utilisateur ouvre la même
machine dans le panneau **Terminal**.

Tout se passe côté client. Le proxy ne fait que servir les fichiers de la
VM, comme ceux des autres bacs du navigateur ([Les outils du
navigateur](navigateur.md)). Le code, les fichiers et `/work` ne
repassent jamais par lui.

Ce n'est pas `code_execution` ([Exécution de code](execution-de-code.md)).
`code_execution` est un outil **hébergé** : un conteneur sur le serveur, avec
Python, Node, Go, et des paquets installables si le réseau est ouvert. La VM
Linux est une machine **émulée dans l'onglet**. Elle coûte au serveur ce
que coûte un fichier statique, mais elle est plus lente, et n'a pas de
paquets à installer.

## Ce qu'elle est

| | |
|---|---|
| Émulateur | [v86](https://github.com/copy/v86) 0.5.470 : un PC x86 32 bits en WebAssembly (BSD-2-Clause) |
| Système | Alpine Linux 3.24 i386, noyau `linux-virt` |
| Paquets | bash, python3, les outils GNU (coreutils, findutils, grep, sed, diffutils, patch), git, jq, sqlite, make, bc, curl, htop, less, tree, procps, xz, et busybox ; pas d'éditeur (le `vi` de busybox) |
| Mémoire | 256 Mo ; tout le système tient dans l'initramfs, en mémoire, sans disque |
| Réseau | coupé ; l'utilisateur peut l'ouvrir pour une conversation (des requêtes web, par le navigateur) |
| Téléchargé | ≈ 35 Mo au premier usage, puis revalidé par le navigateur (`304`) |
| Démarrage | ≈ 1,5 s : la VM reprend un état déjà démarré, sauvé au build |
| Un appel | ≈ 0,3 s pour un script court, sauvegarde de `/work` comprise |

L'émulation est lente : compter quelques secondes pour un gros calcul
Python (`sum(i*i for i in range(10**6))` prend environ 3 s).

## Côté utilisateur

![Panneau Terminal ouvert : le modèle tape dans le terminal le décompte par adresse des erreurs 404 d'un journal nginx joint (awk, sort, uniq) et en résume le résultat dans un tableau ; l'utilisateur lance ensuite, dans le même terminal, la commande qu'il suggère](navigateur-linux.png)

**Le terminal** est toujours là : bouton **Terminal** de l'en-tête du
chat. Il s'**ouvre** de lui-même dès que le modèle se sert de la VM
(`run_shell`, `shell_read`, `shell_grep`, `shell_edit`, `shell_write`), dans
la conversation affichée. Pour que le **modèle** s'en serve aussi, cocher **Linux** dans le
menu **Outils**, section *Navigateur* : il reçoit alors `run_shell`.

**Le panneau Terminal.** Il montre la VM de la conversation, ou la démarre.
On y tape comme dans n'importe quel terminal : c'est un bash root dans
`/work`, sur la même machine que celle du modèle.

| Bouton | Effet |
|---|---|
| Télécharger | `/work` en `work.tar.gz` |
| Réseau | ouvrir ou couper le réseau de la VM pour cette conversation (ci-dessous) ; la VM redémarre, `/work` gardé |
| Redémarrer | une VM neuve, `/work` gardé |
| Arrêter | la VM s'arrête et rend sa mémoire, `/work` gardé |
| Repartir de zéro | une VM neuve et un `/work` **vide**, après confirmation |
| Large | élargit le panneau ; sa largeur se tire aussi par son bord gauche (gardée dans ce navigateur), comme celle des artifacts |
| Fermer | le panneau seulement : la VM continue |

**`/work` est gardé par conversation**, en `tar.gz`, dans ce navigateur
(IndexedDB). Il est rechargé quand la conversation reprend la VM. Il est
gardé :

- après chaque appel du modèle ;
- 10 s après la dernière frappe dans le terminal ;
- au changement de conversation ;
- à la fermeture du panneau ;
- quand l'onglet passe en arrière-plan.

Une conversation incognito le garde en mémoire seulement. Supprimer la
conversation efface son `/work`. Au-delà de 100 Mo compressés, `/work`
n'est plus gardé (le résultat le dit). Le reste du système (`/tmp`, `/root`,
ce qui serait écrit ailleurs) repart propre à chaque démarrage.

**Une seule VM à la fois** : celle de la conversation qui s'en sert. Passer
à une autre conversation garde le `/work` de la première, puis démarre une
VM neuve pour la seconde. Le panneau ne montre que la VM de **sa**
conversation : si le modèle d'une autre conversation s'en sert (sa réponse
continue en arrière-plan), le panneau le dit, masque le terminal, et
propose *La reprendre pour celle-ci*. Ce qu'on tape pendant que la VM
démarre est gardé, puis envoyé quand elle est prête.

## Le modèle tape dans le terminal

Quand le panneau Terminal est **ouvert** sur la conversation (le modèle
l'ouvre en se servant de la VM), ce que `run_shell` exécute est **tapé
dans le terminal** de l'utilisateur, qui le voit partir et s'exécuter : une
commande d'une ligne telle quelle, un script de plusieurs lignes en un bloc
`( … )`. Bash affiche le bloc ligne à ligne (invite `>`) et l'exécute d'un
coup. C'est un sous-shell : un `exit` ou un `cd` du script ne touchent pas
le shell de l'utilisateur. Demander « lance le
script », « montre-moi les fichiers » suffit : la consigne du modèle lui
dit que le terminal est ouvert. Le modèle reçoit la sortie et le code de
sortie, comme d'habitude.

- **Une commande qui ne finit pas** dans le délai (un serveur, une boucle,
  `top`) reste dans le terminal, laissée à l'utilisateur, qui l'arrête par
  Ctrl-C. Le modèle le sait ; pour ce genre de commande, la consigne lui
  demande un `timeout` court.
- **Un terminal pas libre** — une commande en cours, ou une ligne que
  l'utilisateur est en train de taper — n'est **jamais** touché : la
  commande passe par le canal invisible, et le résultat le dit.
- **Un script de plus de 6 000 caractères**, ou avec des tabulations
  (tapées, elles déclencheraient la complétion de bash), passe par le
  canal invisible, et le résultat le dit.
- `terminal: false` exécute sans montrer, même panneau ouvert.

Comment la page sait qu'une commande est finie : le bash du terminal émet
des séquences **OSC 633** que xterm.js n'affiche pas, comme l'intégration
shell de VS Code (`guest/bashrc`). `PS0` émet `C` quand une commande part,
`PROMPT_COMMAND` émet `D;<code>` quand l'invite revient. La sortie est ce
qui s'affiche entre les deux, séquences d'échappement retirées
(`vm.mjs`, `Console` et `typeExec`).

## Le réseau

Coupé par défaut. Le bouton **Réseau** du panneau l'ouvre pour **cette
conversation** (`conv.linuxNetwork`, gardé avec elle), après un
avertissement, comme pour un artifact : le code qui tourne dans la VM, écrit
par le modèle ou par l'utilisateur, peut alors envoyer ce que contient
`/work` à un site qui l'accepte.

Il n'y a **aucun relais serveur** : c'est le backend `fetch` de v86.

- La carte réseau de la VM (virtio) est toujours là, mais n'est reliée à
  rien tant que le réseau est coupé.
- Ouvert, chaque requête HTTP de l'invité devient un `fetch()` du
  navigateur, fait en **https**. Le cadre ouvre alors `connect-src`.
- L'invité a des adresses fixes : `192.168.86.100`, passerelle et DNS
  `192.168.86.1`.

Ce que ça permet, et ce que ça ne permet pas :

| | |
|---|---|
| Adresses | écrites en `http://…` dans la VM ; le navigateur les demande en https |
| Sites | seulement ceux qui permettent CORS à une origine `null` (`Access-Control-Allow-Origin: *`) : API publiques, `pypi.org/pypi/<paquet>/json`, `raw.githubusercontent.com`, `api.github.com`, `registry.npmjs.org`… Les autres répondent `502 Fetch Error` |
| Pas possible | `https://` direct depuis la VM (port 443), ssh, `git clone`, `apk add` (son miroir ne permet pas CORS), `pip install` (ses fichiers sont en https) |

## Éditer les fichiers de la VM

Pour modifier un fichier, le modèle a le même chemin que pour un artifact,
plutôt que `sed` ou un heredoc dans `run_shell` : il cherche (où ?), lit
la tranche (le texte exact), puis remplace le passage. Le code est commun
avec `grep_artifact`, `read_artifact` et `edit_artifact` (`tools/text.js`).

| Outil | Rôle |
|---|---|
| `shell_grep` | `pattern` (texte, ou expression régulière JavaScript avec `regex`) dans un fichier ou un dossier (`path`, défaut `/work` ; récursif, fichiers et dossiers cachés exclus, 500 fichiers d'1 Mo au plus) : les lignes trouvées, numérotées, avec `context` lignes autour, fichier par fichier, 60 au plus |
| `shell_read` | un fichier texte, entier, ou une tranche de lignes numérotées (`start_line`, `end_line`, 400 lignes au plus) ; au-delà de 60 000 caractères, la première tranche, avec où reprendre |
| `shell_edit` | des retouches (`old_string` exact et unique → `new_string`, ou `regex` avec groupes `$1`, `replace_all`, `ignore_case`) : toutes ou aucune, le fichier garde ses droits |
| `shell_write` | un fichier entier, créé (dossiers compris) ou remplacé |

Les chemins sont absolus, ou relatifs à `/work`. Un fichier binaire, un
dossier passé à `shell_read` ou un fichier absent donnent une erreur
claire. Les fichiers passent par le dossier 9p, comme les scripts. Dans
l'historique, une lecture, une recherche ou une écriture passée n'est pas
rejouée au modèle : le fichier a pu changer depuis.

## Côté modèle : `run_shell`

| Paramètre | Rôle |
|---|---|
| `script` | le script bash, exécuté dans `/work`, sans entrée standard |
| `files` | des fichiers du **dossier ouvert** ou des **pièces jointes**, copiés sous `/work` à leur chemin (`..` refusé) ; ils ne partent pas au modèle |
| `timeout` | en secondes : 60 par défaut, 600 au plus ; le script est tué au-delà (code 137) |
| `reset` | repartir d'une VM neuve et d'un `/work` vide |
| `terminal` | `false` : ne pas taper la commande dans le terminal ouvert (défaut : la taper) |

Le résultat donne le code de sortie, puis la sortie (stdout et stderr
mêlés). Au-delà de 20 000 caractères, seuls le début et surtout la fin sont
gardés, puisqu'une erreur est souvent à la fin.

Les fichiers que le script écrit sous **`/out`** pendant l'appel (neufs ou
modifiés) arrivent sous la réponse, comme avec `run_php` : un fichier
texte s'ouvre en aperçu, une image s'affiche (et part au modèle s'il lit
les images). Ils **restent dans la VM** : `/out` est un lien vers
`/work/out`, gardé avec `/work`. Le terminal montre donc ce que la réponse
présente.

## Comment elle tourne dans la page

```
page (chat)                       cadre isolé (sandbox, sans origine)
───────────                       ───────────────────────────────────
run_shell ──► linuxvm.js ──postMessage──► xterm.js (le terminal)
LinuxPanel ─┘   │                         │   ▲
                │                         ▼   │ ttyS0 (octets)
                │                    Worker : v86 + vm.js
                │                         │  ttyS1 : l'agent (vm-agent)
                │                         │  9p    : /mnt/host/.vm
                └── IndexedDB (/work)     ▼
                                     Alpine i386 (state.bin.zst)
```

- **Le cadre** est un `<iframe sandbox="allow-scripts">` sans origine.
  Sa politique de sécurité coupe tout réseau (`default-src 'none'`) : ni
  la page, ni sa session, ni ses cookies ne lui sont visibles. La page
  télécharge les fichiers de `vendor/linux/` et les lui passe en
  `ArrayBuffer`. v86 ne reçoit jamais d'URL.
- **v86 tourne dans un Worker** du cadre : la page reste fluide pendant
  un calcul. Le terminal (xterm.js) est dans le cadre lui-même.
- Le cadre est posé dans `<body>` et **placé par-dessus** le panneau
  quand il est ouvert : un `<iframe>` déplacé dans le DOM se recharge, et
  la VM serait perdue. Fermé, il est hors de l'écran.
- **Deux ports série** :
  - `ttyS0` est le terminal de l'utilisateur, un bash de connexion
    (`console-shell`, sans getty ni mot de passe) ;
  - `ttyS1` est le canal du modèle, `vm-agent`, brut et sans écho, que
    l'utilisateur ne voit pas.
- **Le dossier 9p** (`/mnt/host` dans la VM, en mémoire dans v86) porte
  les scripts, leurs sorties et les fichiers. Le port série ne passe
  qu'environ 8 Ko/s, le 9p plusieurs Mo/s.

### Le protocole de `vm-agent`

Une requête par ligne sur `ttyS1`, une réponse encadrée de repères :

| Requête | Effet | Réponse |
|---|---|---|
| `<id> <délai>` | exécuter `/mnt/host/.vm/<id>.sh` dans `/work`, sortie dans `<id>.log`, tué après le délai | `__END_<id>_<code>__` |
| `done <id>` | effacer `/mnt/host/.vm/<id>.*` | `__DONE_<id>__` |
| `ping` | — | `__PONG__` |
| `init <epoch> <hex>` | remettre l'horloge, réensemencer l'aléa du noyau | `__INIT_OK__` |

Un appel de `run_shell` se passe ainsi (`vm.mjs`, `VM.exec`) :

1. La page pose sur le 9p le script du modèle (`<id>.user`), les fichiers
   fournis (`<id>.in<k>`), et un script d'enveloppe (`<id>.sh`). Cette
   enveloppe :
   - range les fichiers sous `/work` ;
   - lance le script sous `timeout` ;
   - archive `/out` en `<id>.tar`, puis vide `/out`.
2. Elle envoie `<id> <délai>` sur `ttyS1` et attend `__END_<id>_<code>__`.
3. Elle lit `<id>.log` et `<id>.tar`, puis envoie `done <id>`.

`/work` se garde par le même chemin : `tar -czf - .` rend l'archive dans le
journal, et `tar -xzf` la reprend au démarrage.

**Après une reprise d'état**, toutes les VM partent de la même mémoire :
même horloge (celle du build), même générateur aléatoire. `init` les
remet : l'heure de la page, et 64 octets d'aléa de la page mêlés au noyau
(`vm-reseed`, ioctl `RNDADDENTROPY` puis `RNDRESEEDCRNG`). Écrire dans
`/dev/urandom` ne suffirait pas.

## Construire la VM

Une seule commande, avec Docker (BuildKit, le défaut) :

```bash
proxy/linux/build.sh       # → proxy/llm_proxy/ui/web/static/vendor/linux/
```

Le script lance les étapes `linux-guest`, `linux-initrd` et `linux` de
`proxy/Dockerfile` et en exporte le résultat. Il faut compter environ
1 min la première fois, puis quelques secondes grâce au cache. L'image du
proxy (`docker compose up -d --build`) construit la **même** VM par les
mêmes étapes : rien à faire à la main en production.

Hors Docker, la page sert la VM si `vendor/linux/` existe. Sans elle,
`run_shell` et le panneau répondent que la VM manque et qu'elle se
construit par `proxy/linux/build.sh`.

### Les étapes

| Étape (`proxy/Dockerfile`) | Ce qu'elle fait |
|---|---|
| `linux-guest` (`alpine:3.24.2`, `linux/386`) | `apk add $LINUX_PACKAGES linux-virt`. Garde le noyau, et de ses modules ceux du 9p et de `virtio_net` seuls (le reste pèse 20 Mo). Retire pages de manuel, documentation, cache apk, tests de Python. Pose la configuration de `proxy/linux/guest/`. |
| `linux-initrd` | Tout le système en une archive cpio (`initrd.cpio`, ≈ 50 Mo), plus `/dev/console` et `/dev/null` (`devnodes-cpio.py`, car `mknod` n'est pas permis dans un build). |
| `linux` (depuis l'étape `wasm`, où v86 est installé) | `node /linux/state.mjs` : démarre la VM une fois sous Node (≈ 4 s), vide ses caches, sauve son état (zstd niveau 19), puis **le vérifie**. Une VM reprise doit exécuter `python3`, lire un fichier fourni et rendre `/out`, sinon le build échoue. |
| `linux-vendor` | Les seuls fichiers de la VM, pour `build.sh` (`--output`). |

### Les fichiers produits (`vendor/linux/`)

| Fichier | Rôle | Taille |
|---|---|---|
| `state.bin.zst` | la VM démarrée, mémoire comprise ; v86 la décompresse lui-même | ≈ 26 Mo |
| `v86.wasm`, `v86.js` | l'émulateur | 2 Mo + 360 Ko |
| `seabios.bin`, `vgabios.bin` | le BIOS (SeaBIOS 1.16.2, LGPL-3.0), de `proxy/linux/bios/` | 170 Ko |
| `vm.js` | le pilote (`proxy/linux/vm.mjs`), chargé par le Worker | 8 Ko |
| `manifest.json` | version de v86, mémoire, temps de démarrage et de reprise, paquets | — |
| `packages.txt`, `LICENSE-*` | les paquets de l'invité, les licences | — |

Le terminal, xterm.js, est servi avec les autres runtimes
(`vendor/xterm.js`, `vendor/xterm.css`), par `npm run build` dans
`proxy/wasm`.

### Les sources (`proxy/linux/`)

| Fichier | Rôle |
|---|---|
| `build.sh` | le build local (ci-dessus) |
| `state.mjs` | le démarrage, l'état, la vérification, `vendor/linux/` |
| `vm.mjs` | le **pilote**, le même au build (Node) et dans la page (Worker) : options de v86, `ready`, `exec`, le terminal |
| `guest/inittab` | init de busybox (pas d'OpenRC) : `rc.vm`, le bash de `ttyS0`, l'agent de `ttyS1` |
| `guest/rc.vm` | `proc`, `sys`, `devpts`, des `tmpfs` (`/tmp`, `/work`), le montage 9p, l'horloge |
| `guest/vm-agent` | le canal du modèle (protocole ci-dessus) |
| `guest/console-shell`, `guest/bashrc` | le bash du terminal : root, `/work`, invite `\w# `, repères OSC 633 |
| `guest/vm-reseed` | le réensemencement de l'aléa après une reprise |
| `devnodes-cpio.py` | `/dev/console` et `/dev/null` dans l'initramfs |
| `bios/` | SeaBIOS et VGABIOS de v86 (commit `6db8b15`), et leur licence |

### Ajouter des paquets

La VM n'a pas de réseau : ce qui n'est pas installé au build n'y sera pas.
Les paquets se choisissent par `LINUX_PACKAGES`, une liste de paquets
Alpine **i386** (`x86`) :

```bash
LINUX_PACKAGES="bash python3 jq sqlite git coreutils" proxy/linux/build.sh
```

Pour l'image du proxy, la même variable se passe en argument de build
(`docker-compose.override.yml`) :

```yaml
services:
  albert-proxy:
    build:
      args:
        LINUX_PACKAGES: "bash python3 jq sqlite git coreutils"
```

Chaque Mo ajouté pèse sur la mémoire de la VM, sur l'état téléchargé, et
sur la description de `run_shell` à garder juste : elle annonce les
outils de la VM (`sandbox/linux.js`). **Pas d'`apk add` à la volée** :
aucun miroir Alpine n'autorise CORS, le navigateur refuserait leurs
réponses à la VM ; un paquet utile s'ajoute ici. Ce qu'ajoute chaque paquet
à l'initramfs, compressé en zstd :

| Paquets | Initramfs compressé |
|---|---|
| busybox seul | 3,3 Mo |
| `bash python3` | 14,4 Mo |
| `+ coreutils` | +0,6 Mo |
| `+ sqlite` (le module `sqlite3` de Python est déjà là) | +1,0 Mo |
| `+ jq` | +0,3 Mo |
| `+ git` | +4,6 Mo |
| `+ nodejs` | +18 Mo (déconseillé) |
| `+ vim` | +32 Mo bruts, son runtime (déconseillé : `nano`, et le `vi` de busybox) |
| `+ file` | +10 Mo bruts, sa base `magic` |

Le défaut (`LINUX_PACKAGES` de `proxy/Dockerfile`) donne un état de
≈ 33 Mo ; le strict minimum (`bash python3 jq sqlite htop`), ≈ 26 Mo. **L'initramfs se décompresse dans un tmpfs limité à la moitié de
la mémoire** (128 Mo sur 256) : au-delà, ses derniers fichiers manquent,
l'agent avec eux, et la VM ne démarre plus ; `state.mjs` le dit alors, avec
la fin de la console. Le système fait ≈ 72 Mo par défaut.

Avec beaucoup de paquets, la mémoire peut manquer : 192 Mo est le
minimum avec `bash python3`, et la VM en a 256 (`MEMORY_MB` de
`vm.mjs`).

### Mettre à jour v86

v86 est figé dans `proxy/wasm/package.json` (`npm i --save-exact
v86@<version>`). Un **état ne se reprend qu'avec le même build de v86** et
les mêmes périphériques (`uart1`, `filesystem` de `createVM`) : après
une mise à jour, ou un changement de ces options, il faut reconstruire la
VM. L'image Docker le fait toute seule, `build.sh` le fait sur une machine
de développement. Le BIOS vient du dépôt de v86, au commit de la version
npm ; il change rarement.

Le pilote (`vm.js`) et l'état (`state.bin.zst`) vont **ensemble** : un
ancien pilote ne reprend pas un état neuf (`Cannot read properties of null`
dans `set_state`). Un proxy devant la page peut pourtant garder les `.js`
en cache de son côté, quoi que demande le navigateur : Nginx Proxy Manager,
avec *Cache Assets*, les garde 30 min. La page demande donc chaque fichier
de `vendor/` **à son empreinte** (`vm.js?v=db5a66eba01e`), lue dans le
`versions.json` de son dossier, écrit au build et jamais mis en cache.
Un fichier qui change change d'adresse ; inchangé, il garde la sienne et
n'est pas retéléchargé (`chat/tools/sandbox/vendor.js`).

## Licences

- **v86** : BSD-2-Clause.
- **SeaBIOS** : LGPL-3.0.
- **xterm.js** : MIT.
- **L'invité**, dans `state.bin.zst` : Linux (GPL-2.0), busybox
  (GPL-2.0), bash (GPL-3.0+), Python (PSF), musl (MIT), et les licences des
  autres paquets.

Servir l'état, c'est distribuer ces binaires. Leurs sources sont celles
d'Alpine Linux (aports) aux versions exactes de
`vendor/linux/packages.txt`.

## Limites

- **Réseau limité**, et coupé par défaut : ouvert, des requêtes web vers
  les sites qui permettent CORS, rien d'autre (ci-dessus).
- **x86 32 bits** : les binaires sont i686.
- **Lente** : une émulation, environ 10 à 50 fois plus lente que la
  machine.
- **Mémoire** : la VM prend environ 300 Mo à l'onglet tant qu'elle
  tourne. *Arrêter* les rend.
- **Un seul utilisateur, root** : le modèle et l'utilisateur sont tous
  deux root sur la même machine, et voient les mêmes fichiers.
- **`/work` seul est gardé.** Un `/work` de plus de 100 Mo compressés ne
  l'est plus.
- **Le terminal** montre ce que le modèle exécute, sauf un script très
  long ou avec des tabulations, qui passe par `ttyS1` (la puce de l'appel
  dans le fil le montre). Les outils `shell_*` ne s'y voient pas.
