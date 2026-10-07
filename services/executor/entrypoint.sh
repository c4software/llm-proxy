#!/bin/sh
# Démarrage de l'exécuteur. Trois choses avant le serveur.
#
# 1. L'état d'exécution de podman (XDG_RUNTIME_DIR) doit être VIDE à chaque
#    démarrage du conteneur, comme après un redémarrage de machine — c'est
#    à cela que podman reconnaît que plus aucun conteneur ne tourne. Le
#    compose en fait un tmpfs ; sans lui (`docker run` à la main), ce
#    ménage y supplée. Les conteneurs eux-mêmes sont détruits ensuite par
#    le serveur (orphelins).
#
# 2. Le cgroup du conteneur est DÉLÉGUÉ à l'utilisateur `executor`, pour
#    que podman puisse y créer un cgroup par bac : sans cela il accepte
#    --memory, --cpus et --pids-limit et ne borne RIEN (vu sur Docker 29 :
#    1 Go alloué dans un bac « à 512 Mo »). Il faut que Docker ait monté
#    ce cgroup en écriture (`security_opt: writable-cgroups=true`, Docker
#    28 et plus) ; sinon rien n'est délégué, l'exécuteur le constate par sa
#    sonde et le dit, et seul le plafond du conteneur borne les bacs.
#    Un cgroup qui a des enfants bornés ne peut pas porter de processus :
#    ceux du conteneur passent donc dans `init` avant d'ouvrir les
#    contrôleurs aux enfants. Ce que Docker impose au conteneur lui-même
#    (mem_limit, cpus, pids_limit) est écrit un cran plus haut, hors de
#    portée d'ici.
#
# Puis root cède la place : le serveur et podman tournent sous `executor`.
#
# 3. Le PID 1 est un INIT (catatonit, celui que podman embarque), pas le
#    serveur. Chaque `podman run` et chaque `podman exec` laisse un conmon
#    qui se détache : orphelin, il revient au PID 1, et uvicorn n'enterre
#    pas des enfants qu'il n'a pas lancés. Vu sur le déploiement le
#    07/10/2026 : 111 zombies en vingt minutes d'usage, comptés dans le
#    plafond de processus de l'utilisateur — et plus aucun bac ne se créait
#    (« crun: clone: Resource temporarily unavailable », 503).
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
