#!/bin/sh
# Démarrage de l'exécuteur.
#
# 1. XDG_RUNTIME_DIR vidé : c'est à cela que podman reconnaît qu'aucun
#    conteneur ne tourne plus (le compose en fait un tmpfs ; ceci couvre
#    un `docker run` à la main).
#
# 2. Le cgroup du conteneur délégué à `executor`, pour que podman y crée
#    un cgroup par bac : sans cela --memory, --cpus et --pids-limit ne
#    bornent rien. Exige `writable-cgroups=true` ; sinon l'exécuteur le
#    signale et seul le plafond du conteneur tient. Un cgroup aux enfants
#    bornés ne peut porter de processus : ceux du conteneur passent dans
#    `init` d'abord. Puis root cède la place à `executor`.
#
# 3. PID 1 = catatonit, pas le serveur : les conmon détachés de chaque
#    `podman run`/`exec` lui reviennent, et uvicorn ne les enterrerait
#    pas — les zombies épuisent le plafond de processus et plus aucun bac
#    ne se crée (« crun: clone: Resource temporarily unavailable »).
set -eu
INIT=/usr/libexec/podman/catatonit
rm -rf "${XDG_RUNTIME_DIR:?}"/* 2>/dev/null || true
[ "$(id -u)" = 0 ] || exec "$INIT" -- "$@"

cg=/sys/fs/cgroup
if [ -w "$cg/cgroup.subtree_control" ] && mkdir -p "$cg/init" 2>/dev/null; then
    for pid in $(cat "$cg/cgroup.procs"); do
        echo "$pid" > "$cg/init/cgroup.procs" 2>/dev/null || true
    done
    if echo "+cpu +memory +pids" > "$cg/cgroup.subtree_control" 2>/dev/null; then
        chown executor:executor "$cg" "$cg/cgroup.procs" \
            "$cg/cgroup.subtree_control" "$cg/cgroup.threads" \
            "$cg/init" "$cg/init/cgroup.procs"
    fi
fi
exec setpriv --reuid executor --regid executor --init-groups \
    env HOME=/home/executor "$INIT" -- "$@"
