# La VM Linux du navigateur

Dans le [chat](chat.md) de la page, la famille **Linux** fournit au modèle
une machine Linux émulée dans le navigateur. L'outil `run_shell` y exécute
des scripts bash ; l'utilisateur accède à la même machine dans le panneau
**Terminal**.

L'exécution a lieu côté client. Le proxy sert uniquement les fichiers de la
VM, comme ceux des autres bacs du navigateur ([Les outils du
navigateur](navigateur.md)). Le code, les fichiers et `/work` ne transitent
pas par lui.

À la différence de `code_execution` ([Exécution de code](execution-de-code.md)),
outil hébergé exécuté dans un conteneur sur le serveur avec Python, Node,
Go et des paquets installables lorsque le réseau est ouvert, la VM Linux est
émulée dans l'onglet. Elle ne sollicite le serveur que pour le service de
fichiers statiques ; elle est plus lente et ne permet pas d'installer des
paquets.


## Ce qu'elle est

| | |
|---|---|
| Émulateur | [v86](https://github.com/copy/v86) 0.5.470 : un PC x86 32 bits en WebAssembly (BSD-2-Clause) |
| Système | Alpine Linux 3.24 i386, noyau `linux-virt` |
| Paquets | bash, python3, les outils GNU (coreutils, findutils, grep, sed, diffutils, patch), git, jq, sqlite, make, bc, curl, htop, less, tree, procps, xz, et busybox ; pas d'éditeur (le `vi` de busybox) |
| Mémoire | 256 Mo ; tout le système tient dans l'initramfs, en mémoire, sans disque |
| Réseau | coupé ; l'utilisateur peut l'ouvrir pour une conversation (par le relais du proxy, ou des requêtes web par le navigateur) |
| Téléchargé | ≈ 35 Mo au premier usage, puis revalidé par le navigateur (`304`) |
| Démarrage | ≈ 1,5 s : la VM reprend un état déjà démarré, sauvé au build |
| Un appel | ≈ 0,3 s pour un script court, sauvegarde de `/work` comprise |

L'émulation est lente : un calcul Python important peut prendre plusieurs
secondes (`sum(i*i for i in range(10**6))` prend environ 3 s).

## Côté utilisateur

![Panneau Terminal ouvert : le modèle tape dans le terminal le décompte par adresse des erreurs 404 d'un journal nginx joint (awk, sort, uniq) et en résume le résultat dans un tableau ; l'utilisateur lance ensuite, dans le même terminal, la commande qu'il suggère](navigateur-linux.png)

Le bouton **Terminal** de l'en-tête du chat ouvre le panneau. Celui-ci
s'ouvre automatiquement dès que le modèle utilise la VM (`run_shell`,
`shell_read`, `shell_grep`, `shell_edit`, `shell_write`), dans la
conversation affichée. Pour permettre au **modèle** d'utiliser la VM,
cocher **Linux** dans le menu **Outils**, section *Navigateur* : le modèle
reçoit alors `run_shell`.

**Le panneau Terminal** affiche la VM de la conversation ou la démarre.
Il donne accès à un bash exécuté en root dans `/work`, sur la même machine
que celle du modèle.

| Bouton | Effet |
|---|---|
| Télécharger | `/work` en `work.tar.gz` |
| Réseau | ouvrir ou couper le réseau de la VM pour cette conversation (ci-dessous) ; la VM redémarre, `/work` gardé |
| Redémarrer | une VM neuve, `/work` gardé |
| Arrêter | la VM s'arrête et rend sa mémoire, `/work` gardé |
| Repartir de zéro | une VM neuve et un `/work` **vide**, après confirmation |
| Large | élargit le panneau ; sa largeur se tire aussi par son bord gauche (gardée dans ce navigateur), comme celle des artifacts |
| Fermer | le panneau seulement : la VM continue |

**`/work` est conservé par conversation**, dans une archive `tar.gz` stockée
dans ce navigateur (IndexedDB). Le contenu est rechargé à la reprise de la
VM. Il est enregistré :

- après chaque appel du modèle ;
- 10 s après la dernière frappe dans le terminal ;
- au changement de conversation ;
- à la fermeture du panneau ;
- quand l'onglet passe en arrière-plan.

En navigation privée, le contenu reste en mémoire uniquement. La
suppression de la conversation efface son `/work`. Au-delà de 100 Mo
compressés, `/work` n'est plus conservé (le résultat le signale). Le reste
du système (`/tmp`, `/root` et les autres emplacements d'écriture) est
réinitialisé à chaque démarrage.

**Une seule VM est active à la fois** : celle de la conversation qui
l'utilise. Le passage à une autre conversation conserve le `/work` de la
première, puis démarre une nouvelle VM pour la seconde. Le panneau affiche
uniquement la VM de sa conversation. Si le modèle d'une autre conversation
utilise la VM (sa réponse continue en arrière-plan), le panneau le signale,
masque le terminal et propose *La reprendre pour celle-ci*. Les commandes
saisies pendant le démarrage sont conservées et envoyées lorsque la VM est
prête.
## Le modèle tape dans le terminal

Lorsque le panneau Terminal est **ouvert** sur la conversation (le modèle
l'ouvre en utilisant la VM), `run_shell` **tape la commande dans le
terminal** de l'utilisateur, qui en voit l'exécution : une commande d'une
ligne telle quelle, ou un script multiligne dans un bloc `( … )`. Bash
affiche le bloc ligne à ligne (invite `>`) et l'exécute d'un coup. Il s'agit
d'un sous-shell : un `exit` ou un `cd` du script ne modifie pas le shell de
l'utilisateur. La consigne indique au modèle que le terminal est ouvert ;
les demandes « lance le script » ou « montre-moi les fichiers » suffisent.
Le modèle reçoit la sortie et le code de sortie.
- **Une commande qui dépasse le délai** (un serveur, une boucle, `top`)
  reste dans le terminal à la disposition de l'utilisateur, qui l'arrête
  avec Ctrl-C. Le modèle en est informé ; la consigne lui demande de définir
  un `timeout` court pour ce type de commande.
- **Un terminal occupé** — commande en cours ou ligne en saisie — n'est
  **jamais** modifié : la commande passe par le canal invisible et le
  résultat le signale.
- **Un script de plus de 6 000 caractères** ou contenant des tabulations
  (qui déclencheraient la complétion de bash lors de la saisie) passe par le
  canal invisible ; le résultat le signale.
- `terminal: false` exécute sans afficher la commande, même lorsque le
  panneau est ouvert.

La page détecte la fin d'une commande grâce aux séquences **OSC 633** émises
par bash dans le terminal. xterm.js ne les affiche pas ; le mécanisme est
analogue à l'intégration shell de VS Code (`guest/bashrc`). `PS0` émet `C`
au démarrage d'une commande et `PROMPT_COMMAND` émet `D;<code>` au retour de
l'invite. La sortie correspond au texte affiché entre ces marqueurs, après
retrait des séquences d'échappement (`vm.mjs`, `Console` et `typeExec`).

## Le réseau

Le réseau est coupé par défaut. Le bouton **Réseau** du panneau l'active
pour **cette conversation** (`conv.linuxNetwork`, conservé avec elle), après
un avertissement comme pour un artifact. Le code exécuté dans la VM, qu'il
ait été écrit par le modèle ou par l'utilisateur, peut alors transmettre à
un site qui l'accepte le contenu de `/work`.

Deux façons de relier la VM, selon le compte :

- **Le relais du proxy** (`[ui].linux_relay`) fournit un accès à Internet.
- **Le backend `fetch` de v86**, sans relais, est utilisé pour les autres
  comptes et permet des requêtes HTTP vers les sites qui autorisent CORS.

Dans les deux cas :

- La carte réseau de la VM (virtio) est toujours là, mais n'est reliée à
  rien tant que le réseau est coupé.
- L'invité a des adresses fixes : `192.168.86.100`, passerelle et DNS
  `192.168.86.1`. v86 répond au DNS lui-même, par DoH (`cloudflare-dns.com`,
  depuis le navigateur).
- Le cadre ouvre alors `connect-src` (https, et wss pour le relais).

### Le relais (Wisp)

`[ui].linux_relay` définit les comptes autorisés (`"*"` pour tous les
comptes actifs, ou des adresses de compte ; valeur vide par défaut). La page
lit cette configuration dans `/ui/session` (`chat.linux_relay`).

Au démarrage d'une VM avec réseau, la page demande un jeton
(`POST /ui/linux/relay`, avec sa session ; valable 24 h). v86 ouvre alors
un WebSocket vers `/ui/linux/wisp/<jeton>` : le protocole
[Wisp](https://github.com/MercuryWorkshop/wisp-protocol) v1, que v86 parle
nativement (`relay_url: "wisps://…"`). Chaque connexion TCP de l'invité y
devient un flux, que le proxy ouvre vers la cible (`ui/linux_relay.py`) :
https, `apk add`, `pip install`, `git clone`, ssh marchent.

Le trafic **sort du serveur**. Le relais se connecte uniquement aux
adresses publiques (`tools/net.py`, `is_public`) : il ne peut pas atteindre
le réseau local, Tailscale, le loopback ou les conteneurs voisins, quelle
que soit la valeur de `[tools.net].allow_private`. Les noms sont résolus
dans la VM ; le relais vérifie l'adresse IP et non le domaine. Les listes de
domaines de `[tools.net]` ne s'appliquent pas. Les ports configurés dans
`[ui].linux_relay_blocked_ports` sont refusés (25 par défaut), ainsi que
UDP. Limites : 64 flux par VM, 4 relais ouverts par compte et
`[ui].linux_relay_max_bytes` octets par compte sur 24 h, dans les deux sens
(2 Go par défaut). Une fois cette limite atteinte, les connexions du compte
sont fermées jusqu'à la fenêtre suivante. Une connexion sans trafic pendant
`[ui].linux_relay_idle_seconds` (900 s) est fermée. La valeur 0 désactive
l'une ou l'autre limite. Chaque connexion est consignée (compte, cible).

Le droit est vérifié à chaque connexion WebSocket : un compte bloqué perd
l'accès au relais à la reconnexion suivante.

### Sans relais (`fetch`)

Chaque requête HTTP de l'invité est exécutée par un `fetch()` du
navigateur en **https**. Le tableau récapitule les adresses utilisées, les
sites accessibles et les opérations non prises en charge.

| | |
|---|---|
| Adresses | écrites en `http://…` dans la VM ; le navigateur les demande en https |
| Sites | seulement ceux qui permettent CORS à une origine `null` (`Access-Control-Allow-Origin: *`) : API publiques, `pypi.org/pypi/<paquet>/json`, `raw.githubusercontent.com`, `api.github.com`, `registry.npmjs.org`… Les autres répondent `502 Fetch Error` |
| Non pris en charge | `https://` direct depuis la VM (port 443), ssh, `git clone`, `apk add` (son miroir ne permet pas CORS), `pip install` (ses fichiers sont en https) |

## Éditer les fichiers de la VM

Pour modifier un fichier, le modèle utilise les mêmes outils que pour un
artifact plutôt que `sed` ou un heredoc dans `run_shell` : il recherche le
fichier et le passage concerné, lit le texte exact, puis remplace ce
passage. La logique est partagée avec `grep_artifact`, `read_artifact` et
`edit_artifact` (`tools/text.js`).

| Outil | Rôle |
|---|---|
| `shell_grep` | `pattern` (texte, ou expression régulière JavaScript avec `regex`) dans un fichier ou un dossier (`path`, défaut `/work` ; récursif, fichiers et dossiers cachés exclus, 500 fichiers d'1 Mo au plus) : les lignes trouvées, numérotées, avec `context` lignes autour, fichier par fichier, 60 au plus |
| `shell_read` | un fichier texte, entier, ou une tranche de lignes numérotées (`start_line`, `end_line`, 400 lignes au plus) ; au-delà de 60 000 caractères, la première tranche, avec où reprendre |
| `shell_edit` | des retouches (`old_string` exact et unique → `new_string`, ou `regex` avec groupes `$1`, `replace_all`, `ignore_case`) : toutes ou aucune, le fichier garde ses droits |
| `shell_write` | un fichier entier, créé (dossiers compris) ou remplacé |

Les chemins peuvent être absolus ou relatifs à `/work`. Un fichier binaire,
un dossier passé à `shell_read` ou un fichier absent provoque une erreur
explicite. Comme les scripts, les fichiers transitent par le dossier 9p.
Dans l'historique, les lectures, recherches et écritures précédentes ne
sont pas rejouées au modèle, car le fichier a pu changer depuis.

## Côté modèle : `run_shell`

| Paramètre | Rôle |
|---|---|
| `script` | le script bash, exécuté dans `/work`, sans entrée standard |
| `files` | des fichiers du **dossier ouvert** ou des **pièces jointes**, copiés sous `/work` à leur chemin (`..` refusé) ; ils ne partent pas au modèle |
| `timeout` | en secondes : 60 par défaut, 600 au plus ; le script est tué au-delà (code 137) |
| `reset` | repartir d'une VM neuve et d'un `/work` vide |
| `terminal` | `false` : ne pas taper la commande dans le terminal ouvert (défaut : la taper) |

Le résultat contient le code de sortie, puis la sortie combinée (stdout et
stderr). Au-delà de 20 000 caractères, il conserve le début et principalement
la fin, où les erreurs sont souvent indiquées.

Les fichiers créés ou modifiés sous **`/out`** pendant l'appel sont joints
sous la réponse, comme avec `run_php`. Un fichier texte s'ouvre en aperçu ;
une image s'affiche et est transmise au modèle s'il lit les images. Les
fichiers **restent dans la VM** : `/out` est un lien vers `/work/out`, qui
est conservé avec `/work`. Le terminal affiche donc les mêmes fichiers que
la réponse.

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
  Sa politique de sécurité désactive tout accès réseau (`default-src 'none')` ;
  la page, sa session et ses cookies ne lui sont pas accessibles. La page
  télécharge les fichiers de `vendor/linux/` et les transmet sous forme
  d'`ArrayBuffer`. v86 ne reçoit aucune URL.
- **v86 s'exécute dans un Worker** du cadre, ce qui évite de bloquer la page
  pendant les calculs. Le terminal (xterm.js) est rendu dans le cadre.
- Le cadre reste dans `<body>` et est **placé par-dessus** le panneau
  lorsqu'il est ouvert. Son déplacement dans le DOM rechargerait l'`<iframe>`
  et réinitialiserait la VM. Lorsqu'il est fermé, il est hors écran.
- **Deux ports série** :
  - `ttyS0` correspond au terminal de l'utilisateur, un bash de connexion
    (`console-shell`, sans getty ni mot de passe) ;
  - `ttyS1` correspond au canal du modèle, `vm-agent`, brut et sans écho,
    invisible pour l'utilisateur.
- **Le dossier 9p** (`/mnt/host` dans la VM, en mémoire dans v86) contient
  les scripts, leurs sorties et les fichiers. Le débit du port série est
  d'environ 8 Ko/s ; celui de 9p atteint plusieurs Mo/s.

### Le protocole de `vm-agent`

Le protocole utilise une requête par ligne sur `ttyS1` et délimite chaque
réponse par des marqueurs :

| Requête | Effet | Réponse |
|---|---|---|
| `<id> <délai>` | exécuter `/mnt/host/.vm/<id>.sh` dans `/work`, sortie dans `<id>.log`, tué après le délai | `__END_<id>_<code>__` |
| `done <id>` | effacer `/mnt/host/.vm/<id>.*` | `__DONE_<id>__` |
| `ping` | — | `__PONG__` |
| `init <epoch> <hex>` | remettre l'horloge, réensemencer l'aléa du noyau | `__INIT_OK__` |

Un appel `run_shell` suit les étapes suivantes (`vm.mjs`, `VM.exec`) :

1. La page écrit sur 9p le script du modèle (`<id>.user`), les fichiers
   fournis (`<id>.in<k>`) et un script d'enveloppe (`<id>.sh`). Cette
   enveloppe :
   - range les fichiers sous `/work` ;
   - lance le script avec `timeout` ;
   - archive `/out` dans `<id>.tar`, puis vide `/out`.
2. Elle envoie `<id> <délai>` sur `ttyS1` et attend `__END_<id>_<code>__`.
3. Elle lit `<id>.log` et `<id>.tar`, puis envoie `done <id>`.

La persistance de `/work` utilise le même mécanisme : `tar -czf - .` écrit
l'archive dans le journal ; `tar -xzf` la restaure au démarrage.

**Après une reprise d'état**, toutes les VM utilisent la même mémoire
sauvegardée, avec la même horloge (celle du build) et le même générateur
aléatoire. `init` rétablit l'heure de la page et mélange au noyau 64 octets
d'aléa fournis par la page (`vm-reseed`, ioctl `RNDADDENTROPY` puis
`RNDRESEEDCRNG`). Écrire dans `/dev/urandom` ne suffirait pas.

## Construire la VM

Avec Docker (BuildKit activé par défaut), exécuter :

```bash
proxy/linux/build.sh       # → proxy/llm_proxy/ui/web/static/vendor/linux/
```

Le script exécute les étapes `linux-guest`, `linux-initrd` et `linux` de
`proxy/Dockerfile`, puis exporte leur résultat. Le premier build prend
environ 1 min ; les builds suivants prennent quelques secondes grâce au
cache. L'image du proxy (`docker compose up -d --build`) construit la même
VM avec les mêmes étapes ; aucune opération manuelle supplémentaire n'est
nécessaire en production.

Hors Docker, la page sert la VM lorsque `vendor/linux/` existe. Sinon,
`run_shell` et le panneau indiquent que la VM est absente et qu'elle doit
être construite avec `proxy/linux/build.sh`.

### Les étapes

| Étape (`proxy/Dockerfile`) | Ce qu'elle fait |
|---|---|
| `linux-guest` (`alpine:3.24.2`, `linux/386`) | Installe `LINUX_PACKAGES` et `linux-virt` avec `apk add`. Conserve le noyau et uniquement les modules 9p et `virtio_net` (les autres occupent 20 Mo). Supprime les pages de manuel, la documentation, le cache apk et les tests Python. Applique la configuration de `proxy/linux/guest/`. |
| `linux-initrd` | Regroupe le système dans une archive cpio (`initrd.cpio`, ≈ 50 Mo) et ajoute `/dev/console` et `/dev/null` (`devnodes-cpio.py`, car `mknod` n'est pas autorisé dans un build). |
| `linux` (depuis l'étape `wasm`, où v86 est installé) | `node /linux/state.mjs` démarre une fois la VM sous Node (≈ 4 s), vide ses caches, enregistre son état (zstd niveau 19), puis **le vérifie**. La vérification exécute `python3`, lit un fichier fourni et écrit dans `/out` ; en cas d'échec, le build échoue. |
| `linux-vendor` | Exporte uniquement les fichiers de la VM pour `build.sh` (`--output`). |

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

La VM ne dispose pas de réseau ; seuls les paquets inclus au build sont
disponibles. Définir les paquets avec `LINUX_PACKAGES`, une liste de
paquets Alpine **i386** (`x86`) :

```bash
LINUX_PACKAGES="bash python3 jq sqlite git coreutils" proxy/linux/build.sh
```

Pour configurer l'image du proxy, transmettre la même variable comme
argument de build dans `docker-compose.override.yml` :

```yaml
services:
  albert-proxy:
    build:
      args:
        LINUX_PACKAGES: "bash python3 jq sqlite git coreutils"
```

Chaque Mo supplémentaire augmente l'usage mémoire de la VM, la taille de
l'état téléchargé et la description de `run_shell`. Cette dernière doit
rester cohérente avec les outils disponibles dans la VM (`sandbox/linux.js`).
L'installation à la volée avec `apk add` n'est pas prise en charge : aucun
miroir Alpine n'autorise CORS, et le navigateur refuserait ses réponses.
Ajouter au build tout paquet nécessaire. Le tableau indique la taille
compressée en zstd ajoutée à l'initramfs par chaque paquet :

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

La configuration par défaut (`LINUX_PACKAGES` dans `proxy/Dockerfile`)
produit un état d'environ 33 Mo ; le minimum strict (`bash python3 jq sqlite
htop`) produit un état d'environ 26 Mo. **L'initramfs est décompressé dans
un tmpfs limité à la moitié de la mémoire** (128 Mo sur 256). Si cette
limite est dépassée, les derniers fichiers copiés, dont l'agent, manquent
et la VM ne démarre pas. `state.mjs` signale alors l'erreur avec la fin de
la console. Le système occupe environ 72 Mo par défaut.

Une configuration comportant de nombreux paquets peut manquer de mémoire :
avec `bash python3`, 192 Mo constituent le minimum ; la VM est configurée
avec 256 Mo (`MEMORY_MB` dans `vm.mjs`).

### Mettre à jour v86

La version de v86 est fixée dans `proxy/wasm/package.json` (`npm i
--save-exact v86@<version>`). Un **état ne peut être repris qu'avec le
même build de v86** et les mêmes périphériques (`uart1`, `filesystem` de
`createVM`). Après une mise à jour ou une modification de ces options,
reconstruire la VM. Le build Docker le fait automatiquement ; `build.sh`
effectue cette opération sur une machine de développement. Le BIOS provient
du dépôt v86 au commit correspondant à la version npm ; il change rarement.

Le pilote (`vm.js`) et l'état (`state.bin.zst`) doivent **correspondre** :
un ancien pilote ne peut pas reprendre un état récent (`Cannot read
properties of null` dans `set_state`). Un proxy placé devant la page peut
mettre les fichiers `.js` en cache indépendamment des demandes du navigateur.
Par exemple, Nginx Proxy Manager avec *Cache Assets* les conserve 30 min.
La page demande donc chaque fichier de `vendor/` **avec son empreinte**
(`vm.js?v=db5a66eba01e`), lue dans le `versions.json` du dossier. Ce fichier
est généré au build et n'est jamais mis en cache. Une modification change
l'adresse du fichier ; une version inchangée conserve la même adresse et
n'est pas retéléchargée (`chat/tools/sandbox/vendor.js`).

## Licences

- **v86** : BSD-2-Clause.
- **SeaBIOS** : LGPL-3.0.
- **xterm.js** : MIT.
- **L'invité**, dans `state.bin.zst` : Linux (GPL-2.0), busybox
  (GPL-2.0), bash (GPL-3.0+), Python (PSF), musl (MIT), et les licences des
  autres paquets.

La distribution de l'état implique la distribution de ces binaires. Leurs
sources correspondent aux versions exactes des paquets Alpine Linux
(aports) répertoriées dans `vendor/linux/packages.txt`.

## Limites

- **Réseau coupé par défaut** ; lorsqu'il est activé, les comptes autorisés
  par `[ui].linux_relay` accèdent à Internet via le relais du proxy. Les
  autres comptes disposent uniquement de requêtes web vers les sites qui
  autorisent CORS (voir ci-dessus).
- **x86 32 bits** : les binaires sont i686.
- **Émulation lente** : environ 10 à 50 fois plus lente que la machine hôte.
- **Mémoire** : la VM utilise environ 300 Mo dans l'onglet pendant son
  exécution. Le bouton *Arrêter* libère cette mémoire.
- **Un seul utilisateur, root** : le modèle et l'utilisateur sont root sur
  la même machine et accèdent aux mêmes fichiers.
- **`/work` seul est conservé.** Une archive `/work` de plus de 100 Mo
  compressés n'est pas conservée.
- **Le terminal** affiche les commandes exécutées par le modèle, sauf les
  scripts très longs ou contenant des tabulations, qui passent par `ttyS1`
  (la puce de l'appel dans le fil l'indique). Les outils `shell_*` ne sont
  pas affichés dans le terminal.
