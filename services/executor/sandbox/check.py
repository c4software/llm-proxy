"""Contrôle du système de fichiers d'un bac : tout ce que l'outil promet
au modèle s'importe et tourne. Lancé à la CONSTRUCTION de l'image (le
build échoue sinon), et relançable dans un vrai bac :

    exec(open("/usr/local/share/sandbox/check.py").read())

N'écrit que dans le dossier courant et /tmp, comme un programme du modèle.
"""
import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import time

MODULES = ("numpy", "pandas", "scipy", "sympy", "sklearn", "networkx",
           "matplotlib", "seaborn", "PIL", "openpyxl", "docx", "pypdf",
           "reportlab", "tabulate", "yaml")
# Ce dont l'exécuteur lui-même a besoin dans un bac (sandbox.py), puis
# les outils offerts au modèle.
COMMANDS = ("sh", "bash", "timeout", "touch", "find", "tar", "sleep",
            "cat", "mkdir", "rm",
            "node", "jq", "sqlite3", "bc", "file", "zip", "unzip", "gawk",
            "gcc", "g++", "make", "go", "rustc", "cargo")
# Un programme par langage compilé : il doit se compiler HORS LIGNE, avec
# la bibliothèque standard seule, et afficher 42. La commande est celle de
# l'exécuteur (sandbox.LANGS), écrite ici une seconde fois : ce fichier
# tourne seul, dans un bac.
COMPILED = {
    "c": ("main.c", 'gcc -O1 -o main main.c -lm',
          '#include <stdio.h>\n#include <math.h>\n'
          'int main(void) { printf("%.0f\\n", sqrt(1764)); return 0; }\n'),
    "cpp": ("main.cpp", 'g++ -std=c++20 -O1 -o main main.cpp',
            '#include <iostream>\n#include <vector>\n#include <numeric>\n'
            'int main() { std::vector<int> v{20, 22}; '
            'std::cout << std::accumulate(v.begin(), v.end(), 0) << "\\n"; }\n'),
    "go": ("main.go", 'go build -o main main.go',
           'package main\nimport "fmt"\nfunc main() { fmt.Println(6 * 7) }\n'),
    "rust": ("main.rs", 'rustc --edition 2021 -o main main.rs',
             'fn main() { println!("{}", 6 * 7); }\n'),
}

failed = []
for name in MODULES:
    try:
        importlib.import_module(name)
    except Exception as exc:
        failed.append(f"module {name}: {type(exc).__name__}: {exc}")
for name in COMMANDS:
    if shutil.which(name) is None:
        failed.append(f"commande {name}: absente")

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.plot([0, 1, 2], [0, 1, 4])
    plt.savefig("/tmp/_check.png")
    with open("/tmp/_check.png", "rb") as fh:
        assert fh.read(8) == b"\x89PNG\r\n\x1a\n"
except Exception as exc:
    failed.append(f"matplotlib → PNG: {type(exc).__name__}: {exc}")

if shutil.which("node"):
    out = subprocess.run(["node", "-"], input=b"console.log(6 * 7)",
                         capture_output=True)
    if out.stdout.strip() != b"42":
        failed.append(f"node sur l'entrée standard: {out.stderr[-200:]!r}")

built = []
for name, (source, build, program) in COMPILED.items():
    work = tempfile.mkdtemp(prefix=f"_check_{name}_", dir="/tmp")
    try:
        with open(os.path.join(work, source), "w") as fh:
            fh.write(program)
        started = time.monotonic()
        out = subprocess.run(build + " && ./main", shell=True, cwd=work,
                             capture_output=True, timeout=600)
        if out.returncode != 0 or out.stdout.strip() != b"42":
            failed.append(f"{name} (« {build} ») : code {out.returncode}, "
                          f"{(out.stderr or out.stdout)[-300:]!r}")
        built.append(f"{name} {time.monotonic() - started:.1f}s")
    except Exception as exc:
        failed.append(f"{name}: {type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def tree_size(path):
    return sum(os.path.getsize(os.path.join(d, f))
               for d, _, names in os.walk(path) for f in names)


if failed:
    sys.exit("bac à sable INCOMPLET :\n  " + "\n  ".join(failed))
print(f"bac à sable complet : Python {sys.version.split()[0]}, "
      f"{len(MODULES)} modules, {len(COMMANDS)} commandes, "
      f"{len(COMPILED)} langages compilés ({', '.join(built)} — compilation "
      f"et exécution, caches où ils en sont ; cache de Go : "
      f"{tree_size(os.environ.get('GOCACHE', '/tmp/.cache/go-build')) // 1_000_000} Mo)")
