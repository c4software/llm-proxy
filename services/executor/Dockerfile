# syntax=docker/dockerfile:1
#
# L'image de l'exécuteur de code, en deux étapes :
#   1. `sandbox` : le système de fichiers d'un bac, sur la base Arch de
#      dev-box, réduit à l'essentiel ;
#   2. l'exécuteur : podman sans root, le serveur, et ce système de
#      fichiers déplié dans /opt/sandbox/rootfs.
#
# Un bac démarre par `podman run --rootfs /opt/sandbox/rootfs`, en lecture
# seule et partagé : aucune image dans le magasin de podman, ni pull ni
# `podman load` en service. Mettre à jour un bac = reconstruire cette
# image (`docker compose build --pull executor`), puis rejouer
# `python -m executor.validate`.
#
# L'exécuteur sur python:3.13-slim-trixie : la base du proxy, et Debian 13
# a podman 5.4, crun, uidmap et catatonit en paquets. Arch n'y est pas
# reprise : ses newuidmap perdent leurs capacités de fichier dans l'image.

# ── 1. Le système de fichiers d'un bac ──────────────────────────────────
# Seulement ce qui sert à un bac : ni Tailscale, ni sshd, ni podman, ni
# éditeurs. Rien n'est figé : `latest` d'Arch, scripts de dev-box pris sur
# leur branche au build.

# Contexte nommé `devbox`, que le compose pose (additional_contexts) pour
# choisir un fork ou une branche ; sans lui, la branche principale.
FROM scratch AS devbox
ADD https://github.com/c4software/dev-box.git#main /

FROM ghcr.io/archlinux/archlinux:latest AS base-amd64
FROM menci/archlinuxarm:base AS base-arm64
ARG TARGETARCH
FROM base-${TARGETARCH} AS sandbox

ENV LANG=C.UTF-8

# La base d'un bac, le reste à la demande par `devbox dev-env` (mise :
# Go, Rust, Java…). `timeout`, `find`, `tar` servent aussi à sandbox.py,
# Python au relais (forward.py). PHP en paquets : dev-box ne le tient pas
# par mise. Ni gcc ni C/C++ : mise ne les installe pas.
RUN pacman -Syu --noconfirm --needed --disable-sandbox \
      python python-pip nodejs npm \
      php composer php-sqlite \
      jq sqlite bc file zip unzip gawk which \
      mise git curl xz \
 && pacman -Scc --noconfirm --disable-sandbox \
 && rm -rf /var/cache/pacman/pkg/* /var/lib/pacman/sync/*

COPY sandbox/requirements.txt /tmp/requirements.txt
# Bibliothèques de données dans le Python système (PEP 668, d'où
# --break-system-packages), bytecode compilé ici : la racine d'un bac est
# en lecture seule, sans .pyc chaque exécution recompilerait pandas.
RUN pip install --no-cache-dir --break-system-packages --prefer-binary \
      -r /tmp/requirements.txt && rm /tmp/requirements.txt

# `devbox dev-env` et ses recettes de langage. Écartées : celles qui
# veulent un écran ou du matériel, celles qui passent par pacman (absent
# d'un bac), les cadriciels qui tirent d'autres dépôts, et elixir (Erlang
# se compile : trop long).
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

# Aucun binaire setuid/setgid : sans capacité et sous no-new-privileges,
# ils ne serviraient à rien dans un bac.
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} +

# Le contrôle de ce que l'outil promet au modèle : le build échoue sinon.
# Laissé dans l'image pour la validation.
COPY sandbox/check.py /usr/local/share/sandbox/check.py
# Relais vers les dépôts de paquets (SANDBOX_EGRESS), et le point de
# montage de sa prise, créé d'avance : la racine d'un bac est en lecture
# seule.
COPY sandbox/forward.py /usr/local/share/sandbox/forward.py
RUN mkdir -p /run && touch /run/egress.sock
RUN cd /tmp && HOME=/tmp python3 /usr/local/share/sandbox/check.py \
 && rm -rf /tmp/* /tmp/.[!.]* /root/.cache

# ── 2. L'exécuteur ──────────────────────────────────────────────────────
FROM python:3.13-slim-trixie

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# uidmap : un bac tourne sous un uid à lui, dans un userns à plusieurs
# uid. catatonit : l'init de `podman run --init`, qui enterre les zombies
# d'un programme tué par son délai. Ni fuse-overlayfs (magasin `vfs`) ni
# passt/slirp4netns (un bac n'a pas de réseau).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      podman crun uidmap catatonit libcap2-bin \
 && rm -rf /var/lib/apt/lists/* \
 && test -x /usr/libexec/podman/catatonit \
 && podman --version
# newuidmap/newgidmap par capacités de fichier, pas setuid : setuid, ils
# écrivent uid_map sous l'uid 0, à qui le noyau demande CAP_SYS_ADMIN
# (« write to uid_map failed ») ; avec cap_setuid ils restent sous l'uid
# de l'exécuteur, propriétaire du userns. Et plus aucun binaire setuid :
# ce conteneur tourne sans no-new-privileges, un évadé d'un bac n'en
# trouvera pas à lancer.
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + \
 && setcap cap_setuid=ep /usr/bin/newuidmap \
 && setcap cap_setgid=ep /usr/bin/newgidmap \
 && getcap /usr/bin/newuidmap /usr/bin/newgidmap | grep -c cap_set | grep -qx 2

COPY containers/storage.conf /etc/containers/storage.conf
COPY containers/containers.conf /etc/containers/containers.conf.d/10-executor.conf

# Plages d'uid/gid délégués, sans lesquelles podman sans root refuse de
# démarrer. Le conteneur doit les avoir : faux sous un Docker sans root
# ou à userns-remap étroit.
RUN useradd --create-home --uid 10001 executor \
 && echo "executor:100000:65536" > /etc/subuid \
 && echo "executor:100000:65536" > /etc/subgid \
 && install -d -m 700 -o executor -g executor /run/user/10001
ENV XDG_RUNTIME_DIR=/run/user/10001

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --chown : « root » dans le userns de podman, pour que crun y pose ses
# points de montage ; le programme d'un bac, uid sans droit, n'y écrit rien.
COPY --from=sandbox --chown=executor:executor / /opt/sandbox/rootfs/

COPY executor/ /app/executor/
COPY entrypoint.sh /usr/local/bin/entrypoint.sh

USER executor
# Points de montage créés d'avance, la racine sera en lecture seule
# (hosts, hostname, resolv.conf : montés par Docker pendant le build, donc
# absents de l'étape copiée).
RUN cd /opt/sandbox/rootfs \
 && mkdir -p work tmp proc sys dev run etc \
 && touch etc/hosts etc/hostname etc/resolv.conf run/.containerenv

# Root le temps de déléguer le cgroup à `executor` (entrypoint.sh) ; le
# serveur et podman ne tournent jamais en root. 8080 : jamais publié.
USER root

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status==200 else 1)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "executor.server:from_env", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
