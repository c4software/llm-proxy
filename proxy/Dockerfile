# Contexte de build : la racine du dépôt (l'image embarque
# data/config.example.toml). À la main :
#   docker build -f proxy/Dockerfile .

# Les runtimes WASM des bacs du navigateur (PHP, SQLite, PostgreSQL…) : construits
# depuis proxy/wasm, versions et empreintes figées par package-lock.json.
FROM node:24-slim AS wasm
WORKDIR /wasm
COPY proxy/wasm/package.json proxy/wasm/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY proxy/wasm/build.mjs proxy/wasm/php.js proxy/wasm/sql.js proxy/wasm/pglite.js proxy/wasm/chart.js proxy/wasm/sucrase.js proxy/wasm/typescript.js proxy/wasm/xterm.js ./
RUN node build.mjs /vendor

# La VM LINUX du navigateur (run_shell, panneau Terminal ; docs/linux.md) :
# Alpine i386 — v86 émule un PC x86 32 bits — dont tout le système tient
# dans l'initramfs, chargé en mémoire. Les paquets : LINUX_PACKAGES (pas de
# réseau dans la VM : ce qui n'est pas installé ici n'y sera pas).
# Sur un hôte arm64, cette étape demande l'émulation i386 (binfmt/QEMU).
FROM --platform=linux/386 alpine:3.24.2 AS linux-guest
# Chaque Mo pèse sur le téléchargement et la mémoire de la VM (docs/linux.md,
# « Ajouter des paquets ») : ni éditeur (vim pèse 32 Mo), ni file (10 Mo).
ARG LINUX_PACKAGES="bash python3 jq sqlite htop git coreutils findutils grep sed diffutils patch less tree make bc procps curl xz"
# Le noyau linux-virt, et de ses modules, ceux du partage 9p et de la
# carte réseau virtio (le Réseau du panneau) seuls ; sans pages de manuel,
# documentation, cache apk ni tests de Python.
RUN apk add --no-cache $LINUX_PACKAGES linux-virt \
 && KVER=$(ls /lib/modules) && mkdir -p /kernel /keep \
 && cp /boot/vmlinuz-virt /kernel/bzImage \
 && cd /lib/modules/$KVER && cp -a modules.order modules.builtin modules.builtin.modinfo /keep/ \
 && find kernel/fs/9p kernel/fs/netfs kernel/net/9p -name '*.ko*' ! -name '9pnet_xen*' ! -name '9pnet_fd*' > /tmp/keep \
 && find kernel -name 'virtio_net.ko*' -o -name 'net_failover.ko*' -o -name 'failover.ko*' >> /tmp/keep \
 && tar -cf - -T /tmp/keep | tar -xf - -C /keep && rm /tmp/keep \
 && apk del --no-cache linux-virt \
 && mkdir -p /lib/modules/$KVER && cp -a /keep/. /lib/modules/$KVER/ && rm -rf /keep && depmod $KVER \
 && rm -rf /boot /var/cache/apk/* /usr/share/man /usr/share/doc /usr/share/info \
 && for py in /usr/lib/python3.*; do [ -d "$py" ] && rm -rf "$py/test" "$py/idlelib" "$py/tkinter" \
      "$py/turtledemo" "$py/ensurepip" "$py/lib2to3" "$py/pydoc_data" "$py"/config-*; done; true
COPY proxy/linux/guest/inittab /etc/inittab
COPY proxy/linux/guest/rc.vm /etc/rc.vm
COPY proxy/linux/guest/vm-agent proxy/linux/guest/console-shell proxy/linux/guest/vm-reseed /usr/local/bin/
COPY proxy/linux/guest/bashrc /root/.bashrc
RUN chmod +x /etc/rc.vm /usr/local/bin/vm-agent /usr/local/bin/console-shell /usr/local/bin/vm-reseed \
 && echo '[ -f ~/.bashrc ] && . ~/.bashrc' > /root/.bash_profile \
 && sed -i 's#^root:.*#root:x:0:0:root:/root:/bin/bash#' /etc/passwd \
 && ln -sf /sbin/init /init && mkdir -p /work /out /mnt && echo vm > /etc/hostname \
 && apk info -vv 2>/dev/null | sort > /packages.txt

# L'initramfs : tout le système de l'étape précédente en une archive cpio,
# plus /dev/console et /dev/null (devnodes-cpio.py).
FROM --platform=linux/386 alpine:3.24.2 AS linux-initrd
RUN apk add --no-cache cpio python3
COPY proxy/linux/devnodes-cpio.py /devnodes-cpio.py
COPY --from=linux-guest / /rootfs/
RUN mkdir /guest && mv /rootfs/kernel/bzImage /rootfs/packages.txt /guest/ && rmdir /rootfs/kernel \
 && rm -f /rootfs/.dockerenv && cd /rootfs \
 && find . -mindepth 1 | LC_ALL=C sort | cpio -o -H newc -R 0:0 --quiet > /guest/initrd.cpio \
 && python3 /devnodes-cpio.py >> /guest/initrd.cpio

# L'état démarré : la VM lancée une fois sous Node, sauvée, reprise et
# vérifiée (proxy/linux/state.mjs) → /vendor/linux.
FROM wasm AS linux
COPY --from=linux-initrd /guest/ /guest/
COPY proxy/linux/ /linux/
RUN node /linux/state.mjs /guest /vendor/linux

# Les seuls fichiers de la VM, pour proxy/linux/build.sh (docker build
# --target linux-vendor --output …).
FROM scratch AS linux-vendor
COPY --from=linux /vendor/linux/ /

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# ffmpeg : `transcribe` convertit en WAV l'audio que le modèle ne lit
# pas ([tools.transcribe].convert) ; sans lui, ces fichiers sont refusés.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

COPY proxy/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY proxy/main.py .
COPY proxy/llm_proxy/ llm_proxy/
COPY --from=linux /vendor/ llm_proxy/ui/web/static/vendor/
# Le modèle seul : data/ est un volume, où le proxy crée config.toml
# s'il est absent.
COPY data/config.example.toml data/

RUN useradd --create-home --uid 10001 appuser \
 && chown -R appuser:appuser /app/data
USER appuser

VOLUME ["/app/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status==200 else 1)"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
