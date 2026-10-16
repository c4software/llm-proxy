# syntax=docker/dockerfile:1
#
# L'image de l'EXÉCUTEUR de code. Deux étapes :
#   1. `sandbox` : le système de fichiers d'un BAC, sur la base de dev-box
#      (Arch), RÉDUIT à l'essentiel : shell, Python (nu), Node.js, PHP, et
#      mise / `devbox dev-env` pour tout le reste, à la demande ;
#   2. l'exécuteur : podman sans root + le serveur, et le système de
#      fichiers de l'étape 1 DÉPLIÉ dans /opt/sandbox/rootfs.
#
# QUAND ET COMMENT L'IMAGE D'UN BAC ARRIVE : ici, à `docker compose build`.
# En service l'exécuteur ne tire ni ne charge aucune image (sa seule
# sortie, s'il en a une, est le relais des dépôts de paquets des bacs). Un bac démarre par `podman run
# --rootfs /opt/sandbox/rootfs`, en lecture seule, partagé par tous les
# bacs ; il n'y a pas d'image dans le magasin de podman, donc ni pull, ni
# `podman load` au démarrage, ni volume à garder.
# Mettre à jour un bac (une bibliothèque de plus) = reconstruire cette image.
#
# Bases : Arch (celle de dev-box) pour le BAC ; python:3.13-slim-trixie
# pour l'EXÉCUTEUR —
#   * la même que le proxy (proxy/Dockerfile) : une seule base à
#     suivre, et ses couches sont déjà sur la machine ;
#   * Debian 13 a podman 5.4, crun, uidmap et catatonit en paquets : la
#     recette de dev-box (podman sans root dans un conteneur Docker, sans
#     --privileged) s'y transpose paquet pour paquet. Arch n'y est pas
#     reprise : ses binaires newuidmap perdent leurs capacités de fichier
#     dans l'image (dev-box les repose par setcap). Ceux de Debian sont
#     setuid, et NE MARCHENT PAS ici : setcap les remplace aussi, plus
#     bas. Un bac, lui, n'a besoin d'aucun binaire privilégié.

# ── 1. Le système de fichiers d'un bac ──────────────────────────────────
# La BASE de dev-box (github.com/c4software/dev-box) : Arch Linux, la même
# image de départ que son Dockerfile, et SEULEMENT ce qui sert à un bac —
# ni Tailscale, ni sshd, ni podman, ni éditeurs, ni shell interactif.
# Rien n'est figé : `latest` d'Arch, et les scripts de dev-box tels qu'ils
# sont sur leur branche au moment du build (contexte `devbox`, plus bas).
# Mettre à jour un bac = reconstruire (`docker compose build --pull
# executor`), puis rejouer `python -m executor.validate`.

# Les scripts de dev-box : le contexte NOMMÉ `devbox`, que le compose pose
# sur le dépôt (additional_contexts, docker-compose.override.example.yml)
# — un fork, une branche s'y choisissent. Sans lui, ce stage le tire
# lui-même de la branche principale.
FROM scratch AS devbox
ADD https://github.com/c4software/dev-box.git#main /

FROM ghcr.io/archlinux/archlinux:latest AS base-amd64
FROM menci/archlinuxarm:base AS base-arm64
ARG TARGETARCH
FROM base-${TARGETARCH} AS sandbox

ENV LANG=C.UTF-8

# Ce qu'un bac a DE BASE (décision du 16/10/2026 : le reste à la demande),
# en paquets Arch :
#   * le shell : bash, et ce qu'un script appelle sans y penser — jq,
#     sqlite, bc, file, zip/unzip, gawk ; `timeout`, `find` et `tar` (GNU)
#     servent aussi à l'exécuteur (sandbox.py) ;
#   * Python et pip, NUS : aucune bibliothèque — numpy, pandas, matplotlib…
#     s'installent par pip quand il en faut (sortie ouverte). Python sert
#     aussi au relais de la sortie (sandbox/forward.py) ;
#   * Node.js et npm ;
#   * PHP et composer (le seul environnement que dev-box tient en paquets
#     plutôt que par mise : `devbox dev-env php` ne fait que vérifier) ;
#   * mise, et ce dont ses installations ont besoin (git, curl, xz) : Go,
#     Rust, Java, Ruby, .NET, Zig… par `devbox dev-env`, à la demande.
# Ni gcc ni C/C++ : mise ne les installe pas, et ils ne sont plus offerts.
RUN pacman -Syu --noconfirm --needed --disable-sandbox \
      python python-pip nodejs npm \
      php composer php-sqlite \
      jq sqlite bc file zip unzip gawk which \
      mise git curl xz \
 && pacman -Scc --noconfirm --disable-sandbox \
 && rm -rf /var/cache/pacman/pkg/* /var/lib/pacman/sync/*

# `devbox dev-env` : le script de dev-box, sa bibliothèque, et ses recettes
# de LANGAGE. Écartées : celles qui ne servent pas sans écran ni matériel
# (android, ios, desktop, gpu, browser, media, network, flutter), celles
# qui passent par `dev-box-pkg` (pacman, absent d'un bac : swift,
# db-clients), les cadriciels qui tirent d'autres dépôts (laravel,
# symfony, phoenix, ansible), et elixir (Erlang se compile : trop long).
COPY --from=devbox rootfs/usr/local/bin/dev-box-dev-env /usr/local/bin/
COPY --from=devbox rootfs/usr/share/devbox/lib/dev-env.sh /usr/share/devbox/lib/
COPY --from=devbox rootfs/usr/share/devbox/dev-envs/ /usr/share/devbox/dev-envs/
RUN cd /usr/share/devbox/dev-envs \
 && rm -f android.sh android-sdk.sh ios.sh desktop.sh gpu.sh browser.sh \
          media.sh network.sh flutter.sh swift.sh db-clients.sh laravel.sh \
          symfony.sh phoenix.sh ansible.sh elixir.sh
# `devbox` dans un bac : `dev-env` seulement.
COPY sandbox/devbox /usr/local/bin/devbox
RUN chmod 755 /usr/local/bin/devbox /usr/local/bin/dev-box-dev-env \
 && mkdir -p /opt/devbox

# Aucun binaire setuid/setgid : un bac tourne sans capacité et sous
# no-new-privileges, ils n'y serviraient à rien — autant qu'ils n'y
# soient pas.
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} +

# Le contrôle : tout ce que l'outil promet au modèle s'importe et tourne.
# Le build ÉCHOUE sinon. Laissé dans l'image, pour le relancer dans un
# vrai bac (procédure de validation).
COPY sandbox/check.py /usr/local/share/sandbox/check.py
# Le relais de la sortie vers les dépôts de paquets (SANDBOX_EGRESS,
# executor/egress.py) : lancé au démarrage du bac quand la sortie est
# ouverte. Et le point de montage de sa prise : la racine d'un bac est en
# lecture seule, podman n'y crée rien.
COPY sandbox/forward.py /usr/local/share/sandbox/forward.py
RUN mkdir -p /run && touch /run/egress.sock
# Le contrôle, sous l'environnement d'un bac.
RUN cd /tmp && HOME=/tmp python3 /usr/local/share/sandbox/check.py \
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
