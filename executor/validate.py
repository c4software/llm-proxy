"""
VALIDATION de l'exécuteur sur un vrai podman — à lancer DANS le conteneur
exécuteur, sur le déploiement :

    docker compose exec executor python -m executor.validate

Rien de ce que les tests du dépôt vérifient ne touche un vrai moteur de
conteneurs (tests/fake_podman.py n'isole rien) : ce script est ce qui dit
si l'ISOLATION tient ici. Il crée ses propres bacs, à côté de ceux du
serveur (mêmes réglages SANDBOX_*, mais des délais courts), déroule les
cas de la maquette — état gardé, sortie bornée, délai, réseau, bombe de
processus, mémoire, racine en lecture seule, cloisonnement entre clients,
expiration, langages compilés, processus laissés derrière soi —, juge
chacun, mesure le démarrage à froid et à chaud, puis détruit ses bacs.
Code de sortie 0 si aucun cas n'ÉCHOUE. Il n'a besoin ni du jeton, ni du
proxy, ni du réseau : il parle à podman, pas au serveur. Ses bacs ont
leurs propres uid (UID_BASE + 1000…) : ils ne pèsent pas sur ceux d'une
conversation en cours.

Trois verdicts : OK ; ÉCHEC (l'isolation ou la logique ne tient pas : ne
pas activer l'outil) ; NON BORNÉ (une limite de ressources que podman ne
tient pas ici faute de cgroups — attendu, et alors seul le plafond du
conteneur exécuteur protège : voir le README).

    --podman CMD   la commande podman (défaut : podman)
    --light        sans les cas qui chargent la machine ou veulent un vrai
                   bac (bombe de processus, mémoire, lecture seule,
                   contenu, compilateurs) — obligatoire contre la doublure.

La dernière ligne, « VERDICT : … », est celle à relever ; avec la ligne
« MESURES » et celles des cas 13, elle dit tout ce que la procédure du
README demande de noter.
"""

import argparse
import asyncio
import os
import re
import shlex
import sys
import time

from .sandbox import UID_BASE, Limits, SandboxError, Sandboxes

# Les uid des bacs d'ici, à l'écart de ceux du serveur.
UID = UID_BASE + 1000

FAILED, UNBOUNDED = [], []


def verdict(title: str, ok: bool, out=None, detail: str = "",
            unbounded: bool = False) -> None:
    mark = "OK      " if ok else "NON BORNÉ" if unbounded else "ÉCHEC   "
    if not ok:
        (UNBOUNDED if unbounded else FAILED).append(title)
    line = f"[{mark}] {title}"
    if out is not None:
        line += (f"  ({out.seconds:.2f}s, code={out.exit_code}, "
                 f"neuf={out.fresh}, délai={out.timed_out})")
    print(line + (f"\n           {detail}" if detail else ""))
    if out is not None and not ok:
        print("           " + out.output.strip()[-600:].replace(
            "\n", "\n           "))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--podman", default="podman")
    ap.add_argument("--light", action="store_true")
    a = ap.parse_args()

    base = Limits.from_env()
    # 180 s au plus par programme (le contrôle du bac compile quatre
    # langages à froid) ; chaque cas demande le sien (3 s).
    limits = Limits(**{**base.__dict__, "timeout": 180, "idle": 2.0,
                       "max_output": 2000, "max_file_bytes": 50_000,
                       "max_sessions": 4, "max_sessions_per_client": 4})
    image = os.environ.get("SANDBOX_IMAGE", "").strip()
    rootfs = "" if image else os.environ.get(
        "SANDBOX_ROOTFS", "/opt/sandbox/rootfs").strip()
    box = Sandboxes(rootfs, image, limits, podman=shlex.split(a.podman),
                    uid_base=UID)

    code, out, _ = await box._run(
        "info", "--format",
        "rootless={{.Host.Security.Rootless}} cgroups=v{{.Host.CgroupsVersion}} "
        "gestionnaire={{.Host.CgroupManager}} "
        "contrôleurs={{.Host.CgroupControllers}} "
        "runtime={{.Host.OCIRuntime.Name}} pilote={{.Store.GraphDriverName}}",
        timeout=30)
    print("podman info :", out.decode("utf-8", "replace").strip())
    try:
        t = time.monotonic()
        kept = await box.probe()
    except SandboxError as exc:
        print(f"[ÉCHEC   ] podman ne démarre aucun conteneur ici :\n  {exc}")
        return 1
    print(f"sonde ({time.monotonic() - t:.1f}s) — bornes de cgroups TENUES "
          f"par bac : {', '.join(sorted(kept)) or 'aucune'}\n")
    held = {name: name in kept for name in ("memory", "cpu", "pids")}

    try:
        def run(*call, t=3):
            return box.execute(*call, timeout=t)

        t = time.monotonic()
        out = await run("clientA", "conv1", "python", (
            "import os\n"
            "open('notes.txt', 'w').write('état gardé\\n')\n"
            "open('graphique.png', 'wb').write(b'\\x89PNG\\r\\n\\x1a\\n' + os.urandom(64))\n"
            "open('gros.bin', 'wb').write(b'0' * 100_000)\n"
            "print('uid', os.getuid(), 'cwd', os.getcwd())\n"))
        cold = time.monotonic() - t
        names = [f.name for f in out.files]
        verdict("1. Python à froid : bac créé, fichiers rendus, le trop gros écarté",
                out.exit_code == 0 and out.fresh
                and names == ["graphique.png", "notes.txt"]
                and [s["name"] for s in out.skipped] == ["gros.bin"]
                and (a.light or out.output.split()[:4]
                     == ["uid", str(UID), "cwd", "/work"]),
                out, f"rendus : {names} ; écartés : {out.skipped} ; "
                     f"{out.output.strip()}")

        out = await run("clientA", "conv1", "bash",
                        "cat notes.txt; echo suite >> notes.txt; ls")
        warm = out.seconds
        verdict("2. shell à chaud : retrouve le fichier de l'appel 1",
                out.exit_code == 0 and not out.fresh
                and out.output.startswith("état gardé")
                and [f.name for f in out.files] == ["notes.txt"], out)

        times = []
        for _ in range(5):
            times.append((await run("clientA", "conv1", "python",
                                    "print(1)")).seconds)
        t = time.monotonic()
        heavy = await run("clientA", "conv1", "python", (
            "import pandas, matplotlib.pyplot as plt\n"
            "plt.plot([1, 2, 3]); plt.savefig('/tmp/x.png'); print('ok')\n"),
            t=30)
        print(f"   MESURES : à froid {cold:.2f}s ; à chaud {warm:.2f}s ; "
              f"5 × print(1) : min {min(times):.2f}s, max {max(times):.2f}s ; "
              f"import pandas + matplotlib et un tracé : "
              f"{time.monotonic() - t:.2f}s (code {heavy.exit_code})")

        out = await run("clientA", "conv1", "python", "print('x' * 1_000_000)")
        verdict("3. sortie trop longue : coupée",
                out.truncated and len(out.output) < 2300, out)

        out = await run("clientA", "conv1", "python",
                        "import time; print('début', flush=True); time.sleep(60)")
        verdict("4. délai dépassé (3 s) : programme tué, sortie gardée",
                out.timed_out and "début" in out.output and out.seconds < 9, out)

        out = await run("clientA", "conv1", "sh", "ls")
        verdict("5. le bac a survécu au délai",
                not out.fresh and "notes.txt" in out.output, out)

        out = await run("clientA", "conv1", "python", (
            "import os, socket\n"
            "try:\n"
            "    socket.create_connection(('1.1.1.1', 53), 2); print('RESEAU OUVERT')\n"
            "except OSError as e:\n"
            "    print('pas de réseau :', e)\n"
            "print('interfaces', sorted(os.listdir('/sys/class/net')))\n"))
        verdict("6. réseau : aucun (ni route, ni interface autre que lo)",
                "RESEAU OUVERT" not in out.output
                and (a.light or "interfaces ['lo']" in out.output), out,
                out.output.strip().replace("\n", " ; "))

        if not a.light:
            out = await run("clientA", "conv1", "python", (
                "import os, time\n"
                "n = 0\n"
                "try:\n"
                "    while n < 5000:\n"
                "        if os.fork() == 0:\n"
                "            time.sleep(2); os._exit(0)\n"
                "        n += 1\n"
                "    print('NON BORNE', n)\n"
                "except OSError as e:\n"
                "    print('fork refusé après', n, ':', e)\n"))
            # Arrêtée par le plafond du CONTENEUR (pids_limit du compose)
            # et non par celui du bac : un échec, les autres bacs sont
            # affamés avec lui.
            stopped = re.search(r"fork refusé après (\d+)", out.output)
            verdict(f"7. bombe de processus : arrêtée avant {limits.pids} "
                    f"(RLIMIT_NPROC par uid de bac"
                    f"{', --pids-limit' if held['pids'] else ''})",
                    bool(stopped) and int(stopped.group(1)) <= limits.pids,
                    out, out.output.strip()[-200:])

            out = await run("clientA", "conv1", "python", (
                "b = bytearray(b'\\x01') * 1_000_000_000\n"
                "print('ALLOUE', len(b))\n"))
            bounded = "ALLOUE" not in out.output
            verdict(f"8. mémoire : 1 Go demandé, bac borné à {limits.memory}",
                    bounded, out,
                    "" if bounded else "podman ne tient PAS --memory ici "
                    "(cgroups non délégués) : mem_limit du conteneur "
                    "exécuteur est le seul filet.",
                    unbounded=not held["memory"])
            if not held["cpu"]:
                verdict(f"8bis. CPU : bac borné à {limits.cpus} cœur(s)", False,
                        detail="podman ne tient PAS --cpus ici : `cpus` du "
                               "conteneur exécuteur est le seul filet.",
                        unbounded=True)

            out = await run("clientA", "conv1", "sh", (
                "id; touch /etc/x /usr/x /var/tmp/x /run/x 2>&1; "
                "grep -E 'CapEff|NoNewPrivs|Seccomp:' /proc/self/status; "
                "head -c 300000000 /dev/zero > /work/plein 2>&1; "
                "stat -c 'écrit=%s' /work/plein; rm -f /work/plein"))
            text = out.output
            written = re.search(r"écrit=(\d+)", text)
            verdict("9. racine en lecture seule, aucune capacité, "
                    "no-new-privileges, seccomp, /work borné",
                    text.count("Read-only file system") == 4
                    and f"uid={UID}" in text
                    and "CapEff:\t0000000000000000" in text
                    and "NoNewPrivs:\t1" in text and "Seccomp:\t2" in text
                    and bool(written) and int(written.group(1)) < 300_000_000,
                    out, text.strip().replace("\n", " ; ")[:700])

        out = await run("clientB", "conv1", "sh", "ls; echo '(vide attendu)'")
        verdict("10. AUTRE client, même identifiant de session : bac vide",
                out.fresh and out.output.strip() == "(vide attendu)", out)

        await asyncio.sleep(limits.idle + 0.5)
        gone = await box.sweep()
        out = await run("clientA", "conv1", "sh", "ls; echo fin")
        verdict("11. après expiration : bacs détruits, bac NEUF, fichiers perdus",
                gone == 2 and out.fresh and out.output.strip() == "fin", out,
                f"détruits par sweep() : {gone}")

        if not a.light:
            out = await run("clientA", "", "python", (
                "exec(open('/usr/local/share/sandbox/check.py').read())"), t=180)
            verdict("12. contenu du bac (check.py) : bibliothèques, commandes, "
                    "compilateurs hors ligne", out.exit_code == 0, out,
                    out.output.strip()[-400:])

            # Chaque langage compilé par le chemin de l'outil : le source
            # sur l'entrée standard, compilé dans /tmp, exécuté — sous le
            # délai par défaut de l'outil (30 s). Go une seconde fois : la
            # première paie le cache de sa bibliothèque standard.
            hello = {
                "c": '#include <stdio.h>\nint main(void) { puts("42"); return 0; }',
                "cpp": '#include <iostream>\nint main() { std::cout << 42 << "\\n"; }',
                "go": 'package main\nimport "fmt"\nfunc main() { fmt.Println(42) }',
                "rust": 'fn main() { println!("42"); }',
            }
            slow = []
            for name in ("c", "cpp", "rust", "go", "go"):
                out = await run("clientA", "compile", name, hello[name], t=30)
                good = out.exit_code == 0 and out.output.strip() == "42" \
                    and not out.files
                verdict(f"13. {name} : compilé et exécuté en moins de 30 s, "
                        f"rien dans /work", good, out)
                if good and out.seconds > 15:
                    slow.append(f"{name} {out.seconds:.0f}s")
            out = await run("clientA", "compile", "sh",
                            "du -sm /tmp 2>/dev/null | cut -f1; df -m /tmp | tail -1")
            print(f"   MESURES : /tmp après les compilations (Mo utilisés, "
                  f"puis df) : {out.output.strip()!r}"
                  + (f" ; LENT (plus de 15 s) : {', '.join(slow)} — monter "
                     f"[tools.code_execution].timeout" if slow else ""))

            # Un processus détaché (setsid) échappe à `timeout` : il vit
            # jusqu'à la fin du bac. Borné par le bac, pas par l'appel.
            await run("clientA", "fond", "python", (
                "import os, time\n"
                "if os.fork() == 0:\n"
                "    os.setsid()\n"
                "    for fd in (0, 1, 2): os.close(fd)\n"
                "    time.sleep(600)\n"))
            out = await run("clientA", "fond", "python", (
                "import os\n"
                "n = 0\n"
                "for p in os.listdir('/proc'):\n"
                "    if p.isdigit() and int(p) != os.getpid():\n"
                "        try:\n"
                "            n += b'python3' in open(f'/proc/{p}/cmdline', 'rb').read()\n"
                "        except OSError:\n"
                "            pass\n"
                "print('restants', n)\n"))
            left = out.output.split()[-1:] != ["0"]
            verdict("14. processus laissé en arrière-plan : tué avec l'appel",
                    not left, out,
                    "un programme peut laisser tourner un processus jusqu'à "
                    "la fin de son bac (inactivité, durée de vie) : il reste "
                    "dans les bornes du bac, et l'appel suivant du MÊME bac "
                    "le voit. Attendu : `timeout` ne tue que son groupe."
                    if left else "", unbounded=left)
    except SandboxError as exc:
        print(f"[ÉCHEC   ] PANNE DU BAC À SABLE : {exc}")
        FAILED.append("panne")
    finally:
        await box.close()
        code, out, _ = await box._run(
            "ps", "--all", "--filter", "label=llm-proxy.sandbox=1",
            "--format", "{{.Names}}", timeout=30)
        print(f"\nbacs restants après nettoyage (ceux du serveur compris) : "
              f"{out.decode('utf-8', 'replace').split()}")

    print(f"\n{len(FAILED)} ÉCHEC(S), {len(UNBOUNDED)} ressource(s) NON "
          f"BORNÉE(S) par bac.")
    for title in FAILED:
        print("  ÉCHEC     :", title)
    for title in UNBOUNDED:
        print("  NON BORNÉ :", title)
    print("VERDICT : " + (
        "NE PAS ACTIVER l'outil — l'isolation ou la logique ne tient pas ici."
        if FAILED else
        "activable ; les ressources NON BORNÉES ne le sont que par le "
        "plafond du conteneur exécuteur (mem_limit, cpus, pids_limit)."
        if UNBOUNDED else "activable, toutes les bornes par bac sont tenues.")
        + (" [--light : isolation NON vérifiée]" if a.light else ""))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
