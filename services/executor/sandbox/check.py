"""Contrôle du système de fichiers d'un bac : tout ce que l'outil promet
au modèle DE BASE est là et tourne. Lancé à la CONSTRUCTION de l'image (le
build échoue sinon), et relançable dans un vrai bac :

    exec(open("/usr/local/share/sandbox/check.py").read())

N'écrit que dans le dossier courant et /tmp, comme un programme du modèle.

Le bac de base est réduit (décision du 16/10/2026) : le shell, Python NU,
Node.js, PHP, et mise / `devbox dev-env` pour le reste — qui s'installe à
la demande, quand la sortie vers les dépôts est ouverte : rien de cela
n'est contrôlé ici (pas de réseau au contrôle).
"""
import os
import shutil
import subprocess
import sys

COMMANDS = ("sh", "bash", "timeout", "touch", "find", "tar", "sleep",
            "cat", "mkdir", "rm",
            "python3", "pip", "node", "npm", "php", "composer",
            "jq", "sqlite3", "bc", "file", "zip", "unzip", "gawk",
            "mise", "devbox", "dev-box-dev-env")

# Chaque interpréteur lit un programme sur son entrée standard, comme
# l'exécuteur le lui donne (sandbox.LANGS).
PROGRAMS = {
    "python3": (["python3", "-"], b"print(6 * 7)"),
    "node": (["node", "-"], b"console.log(6 * 7)"),
    "php": (["php"], b"<?php echo 6 * 7, \"\\n\";"),
    "bash": (["bash", "-s"], b"echo $((6 * 7))"),
}

failed = []
for name in COMMANDS:
    if shutil.which(name) is None:
        failed.append(f"commande {name}: absente")

for name, (argv, program) in PROGRAMS.items():
    if shutil.which(argv[0]) is None:
        continue
    out = subprocess.run(argv, input=program, capture_output=True)
    if out.stdout.strip() != b"42":
        failed.append(f"{name} sur l'entrée standard: code {out.returncode}, "
                      f"{(out.stderr or out.stdout)[-200:]!r}")

# `devbox dev-env` : le script de dev-box, sa bibliothèque et ses recettes
# se chargent (la liste ne télécharge rien).
if shutil.which("devbox"):
    out = subprocess.run(["devbox", "dev-env", "--list"], capture_output=True,
                         env={**os.environ, "HOME": "/tmp", "NO_COLOR": "1"})
    if out.returncode != 0 or b"java" not in out.stdout:
        failed.append(f"devbox dev-env --list: code {out.returncode}, "
                      f"{(out.stderr or out.stdout)[-300:]!r}")

if failed:
    sys.exit("bac à sable INCOMPLET :\n  " + "\n  ".join(failed))
print(f"bac à sable complet : Python {sys.version.split()[0]} (nu), "
      f"{len(COMMANDS)} commandes, {', '.join(PROGRAMS)} sur l'entrée "
      f"standard, devbox dev-env prêt")
