#!/bin/sh
# Démarrage de l'exécuteur. Une seule chose avant le serveur : l'état
# d'exécution de podman (XDG_RUNTIME_DIR) doit être VIDE à chaque démarrage
# du conteneur, comme après un redémarrage de machine — c'est à cela que
# podman reconnaît que plus aucun conteneur ne tourne. Le compose en fait
# un tmpfs ; sans lui (`docker run` à la main), ce ménage y supplée. Les
# conteneurs eux-mêmes sont détruits ensuite par le serveur (orphelins).
set -eu
rm -rf "${XDG_RUNTIME_DIR:?}"/* 2>/dev/null || true
exec "$@"
