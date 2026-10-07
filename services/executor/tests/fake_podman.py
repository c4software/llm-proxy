#!/usr/bin/env python3
"""
FAUX podman — DOUBLURE DE TEST, AUCUNE ISOLATION.

Sert uniquement à dérouler la logique de l'exécuteur (executor/ :
sessions, plafonds, récolte des fichiers, expiration, sonde des cgroups)
sur une machine sans moteur de conteneurs. Un « conteneur » est un
dossier sous $FAKE_PODMAN_ROOT ; `exec` y lance la commande TELLE QUELLE,
avec les droits de l'appelant. Tous les drapeaux d'isolation de `run`
sont IGNORÉS — seulement notés (fichier `.args` du conteneur), pour qu'un
test vérifie qu'ils sont DEMANDÉS. Ne rien en conclure sur le
cloisonnement ni sur les temps de podman.

  FAKE_PODMAN_DENY   drapeaux de `run` refusés, séparés par des virgules
                     («--memory,--cpus») : un podman sans ces cgroups ;
  FAKE_PODMAN_IGNORE drapeaux de `run` acceptés SANS effet : un podman sans
                     cgroup délégué, qui prend --memory et ne borne rien ;
  FAKE_PODMAN_DOWN   non vide : tout échoue (podman ne démarre pas).
"""
import json
import os
import shutil
import sys

root = os.environ["FAKE_PODMAN_ROOT"]
cmd, args = sys.argv[1], sys.argv[2:]


def value(flag):
    return args[args.index(flag) + 1] if flag in args else None


def refuse(message):
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(125)


if os.environ.get("FAKE_PODMAN_DOWN"):
    refuse("cannot clone: Operation not permitted")
if cmd == "run":
    denied = [f for f in os.environ.get("FAKE_PODMAN_DENY", "").split(",")
              if f and f in args]
    if denied:
        refuse(f"crun: cgroup controller for {denied[0]} is not available")
    if "--rm" in args:              # un conteneur jetable de la sonde : il
        # lit ses bornes dans son cgroup. Un drapeau de FAKE_PODMAN_IGNORE
        # est accepté sans rien borner (un podman sans cgroup délégué).
        ignored = os.environ.get("FAKE_PODMAN_IGNORE", "").split(",")
        for flag, name in (("--memory", "memory.max"), ("--cpus", "cpu.max"),
                           ("--pids-limit", "pids.max")):
            print(name, value(flag) if flag in args and flag not in ignored
                  else "max")
    else:
        name = value("--name")
        os.makedirs(os.path.join(root, name, value("--workdir").lstrip("/")))
        with open(os.path.join(root, name, ".args"), "w") as fh:
            json.dump(args, fh)
        print(name)
elif cmd == "exec":
    workdir = value("--workdir")
    rest = [a for a in args if a != "--interactive"]
    rest = rest[rest.index("--workdir") + 2:]
    name, command = rest[0], rest[1:]
    path = os.path.join(root, name, workdir.lstrip("/"))
    if not os.path.isdir(path):
        refuse(f"no such container {name}")
    os.chdir(path)
    os.execvp(command[0], command)
elif cmd == "inspect":
    if not os.path.isdir(os.path.join(root, args[-1])):
        refuse(f"no such object {args[-1]}")
    print("true")
elif cmd == "rm":
    for name in args:
        if not name.startswith("-") and name != "0":
            shutil.rmtree(os.path.join(root, name), ignore_errors=True)
elif cmd == "ps":
    print("\n".join(sorted(os.listdir(root))))
else:
    sys.exit(f"fake podman: {cmd} non géré")
