# syntax=docker/dockerfile:1
#
# L'image de l'EXÉCUTEUR de code. Deux étapes :
#   1. `sandbox` : le système de fichiers d'un BAC (Python et ses
#      bibliothèques, shell, Node.js, compilateurs C/C++, Go et Rust) ;
#   2. l'exécuteur : podman sans root + le serveur, et le système de
#      fichiers de l'étape 1 DÉPLIÉ dans /opt/sandbox/rootfs.
#
# QUAND ET COMMENT L'IMAGE D'UN BAC ARRIVE : ici, à `docker compose build`
# — le seul moment où il y a du réseau. En service l'exécuteur n'a aucune
# sortie : il ne tire rien, ne charge rien. Un bac démarre par `podman run
# --rootfs /opt/sandbox/rootfs`, en lecture seule, partagé par tous les
# bacs ; il n'y a pas d'image dans le magasin de podman, donc ni pull, ni
# `podman load` au démarrage, ni volume à garder.
# Mettre à jour un bac (une bibliothèque de plus) = reconstruire cette image.
#
# Base : python:3.13-slim-trixie, pour les deux étapes.
#   * la même que le proxy (proxy/Dockerfile) : une seule base à
#     suivre, et ses couches sont déjà sur la machine ;
#   * Debian 13 a podman 5.4, crun, uidmap et catatonit en paquets : la
#     recette de dev-box (podman sans root dans un conteneur Docker, sans
#     --privileged) s'y transpose paquet pour paquet. Arch, la base de
#     dev-box, n'est pas reprise : image glissante, et ses binaires
#     newuidmap perdent leurs capacités de fichier dans l'image (dev-box
#     les repose par setcap). Ceux de Debian sont setuid, et NE MARCHENT
#     PAS ici : setcap les remplace aussi, plus bas.

# ── 1. Le système de fichiers d'un bac ──────────────────────────────────
FROM python:3.13-slim-trixie AS sandbox

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Les autres langages, et pourquoi ceux-là :
#   * bash, sh et les outils GNU sont dans la base ; `timeout`, `find` et
#     `tar` servent aussi à l'exécuteur (sandbox.py) ;
#   * nodejs (Debian : 20 LTS) : le second langage que les modèles
#     écrivent le plus, sans rien à compiler ; ~150 Mo ;
#   * jq, sqlite3, bc, file, zip/unzip, gawk : ce qu'un script shell
#     appelle sans y penser ; quelques Mo ;
#   * des COMPILATEURS (décision du 07/10/2026) : gcc et g++ avec la libc
#     de développement et make, Go (golang-go) et Rust (rustc, cargo),
#     ceux de Debian 13. Lourds — l'image entière, bac compris, pèse
#     2,09 Go — et bornés à leur BIBLIOTHÈQUE STANDARD : le bac n'a pas de
#     réseau, donc ni module Go, ni crate, ni paquet à chercher. cargo et
#     `go build` marchent hors ligne pour un projet sans dépendance.
# Ni Java, ni curl, ni git, ni pip utile : pas de réseau.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      nodejs jq sqlite3 bc file zip unzip gawk \
      gcc g++ libc6-dev make golang-go rustc cargo \
 && rm -rf /var/lib/apt/lists/*

COPY sandbox/requirements.txt /tmp/requirements.txt
# pip compile le bytecode à l'installation : il le faut, la racine d'un
# bac est en lecture seule (sans .pyc, chaque exécution recompilerait
# pandas).
RUN pip install -r /tmp/requirements.txt && rm /tmp/requirements.txt

# Aucun binaire setuid/setgid : un bac tourne sans capacité et sous
# no-new-privileges, ils n'y serviraient à rien — autant qu'ils n'y
# soient pas.
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} +

# Le contrôle : tout ce que l'outil promet au modèle s'importe et tourne.
# Le build ÉCHOUE sinon. Laissé dans l'image, pour le relancer dans un
# vrai bac (procédure de validation).
COPY sandbox/check.py /usr/local/share/sandbox/check.py
# Sous l'environnement d'un bac pour Go (sandbox.py) : hors ligne, cache
# dans /tmp — le contrôle dit le temps et la taille d'une première
# compilation, à comparer à SANDBOX_TMP_SIZE et au délai de l'outil.
RUN cd /tmp && HOME=/tmp MPLCONFIGDIR=/tmp/mpl \
    GOCACHE=/tmp/.cache/go-build GOPATH=/tmp/go GOPROXY=off \
    GOTOOLCHAIN=local GOFLAGS=-buildvcs=false \
    CARGO_HOME=/tmp/.cargo CARGO_NET_OFFLINE=true \
    python3 /usr/local/share/sandbox/check.py \
 && rm -rf /tmp/* /tmp/.[!.]* /root/.cache

# ── 2. L'exécuteur ──────────────────────────────────────────────────────
FROM python:3.13-slim-trixie

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# podman sans root, la recette de dev-box moins ce qu'un bac n'utilise pas :
#   podman     le moteur (tire conmon, netavark, containers-common) ;
#   crun       le runtime (containers.conf) ;
#   uidmap     newuidmap/newgidmap : sans eux, pas d'espace d'utilisateurs
#              à plus d'un uid, et un bac tourne sous un uid à lui ;
#   catatonit  l'init de `podman run --init` (il enterre les zombies que
#              laisse un programme tué par son délai).
# Pas de fuse-overlayfs (magasin `vfs`, voir containers/storage.conf), ni
# passt/slirp4netns (un bac n'a PAS de réseau).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      podman crun uidmap catatonit libcap2-bin \
 && rm -rf /var/lib/apt/lists/* \
 && test -x /usr/libexec/podman/catatonit \
 && podman --version
# newuidmap et newgidmap par CAPACITÉS DE FICHIER (libcap2-bin), pas par
# setuid — la recette de dev-box. Vu sur Docker 29, noyau 6.1 : le binaire
# setuid de Debian échoue sur « newuidmap: write to uid_map failed:
# Operation not permitted » — il écrit la table sous l'uid 0, à qui le
# noyau demande alors CAP_SYS_ADMIN, que ce conteneur n'a pas ; avec
# cap_setuid posée sur le fichier il reste sous l'uid de l'exécuteur,
# propriétaire de l'espace d'utilisateurs, et l'écriture passe.
# Et plus AUCUN binaire setuid : ce conteneur tourne sans
# no-new-privileges (les capacités de fichier en dépendent), qui s'y
# trouverait après s'être évadé d'un bac n'en trouvera pas à lancer.
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + \
 && setcap cap_setuid=ep /usr/bin/newuidmap \
 && setcap cap_setgid=ep /usr/bin/newgidmap \
 && getcap /usr/bin/newuidmap /usr/bin/newgidmap | grep -c cap_set | grep -qx 2

COPY containers/storage.conf /etc/containers/storage.conf
COPY containers/containers.conf /etc/containers/containers.conf.d/10-executor.conf

# L'utilisateur du service, et ses plages d'uid/gid délégués : sans elles
# podman sans root refuse de démarrer (« cannot find UID/GID for user »).
# 65536 identifiants à partir de 100000 : il faut que le conteneur
# exécuteur les AIT — vrai sous un Docker ordinaire, faux sous un Docker
# lui-même sans root ou à userns-remap étroit.
RUN useradd --create-home --uid 10001 executor \
 && echo "executor:100000:65536" > /etc/subuid \
 && echo "executor:100000:65536" > /etc/subgid \
 && install -d -m 700 -o executor -g executor /run/user/10001
ENV XDG_RUNTIME_DIR=/run/user/10001

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Le système de fichiers des bacs. --chown : il appartient à l'utilisateur
# du service, donc à « root » DANS l'espace d'utilisateurs de podman —
# crun peut y poser ses points de montage, et le programme d'un bac (un
# uid sans droit, racine en lecture seule) n'y écrit rien.
COPY --from=sandbox --chown=executor:executor / /opt/sandbox/rootfs/

COPY executor/ /app/executor/
COPY entrypoint.sh /usr/local/bin/entrypoint.sh

USER executor
# Les points de montage qu'un conteneur attend, créés d'avance : la racine
# sera en lecture seule. (/etc/hosts, hostname et resolv.conf sont montés
# par Docker pendant un build : ils n'existent pas dans l'étape copiée.)
RUN cd /opt/sandbox/rootfs \
 && mkdir -p work tmp proc sys dev run etc \
 && touch etc/hosts etc/hostname etc/resolv.conf run/.containerenv

# 8080, sur le réseau interne du compose seulement : JAMAIS publié.
# Le point d'entrée démarre en ROOT, le temps de déléguer le cgroup du
# conteneur à l'utilisateur `executor` (les bornes par bac en dépendent),
# puis lui cède la place : le serveur et podman ne tournent jamais en root.
USER root

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status==200 else 1)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "executor.server:from_env", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
