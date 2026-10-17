"""
VALIDATION de l'exécuteur sur un vrai podman — à lancer DANS le conteneur
exécuteur, sur le déploiement :

    docker compose exec executor python -m executor.validate

Rien de ce que les tests du dépôt vérifient ne touche un vrai moteur de
conteneurs (tests/fake_podman.py n'isole rien) : ce script est ce qui dit
si l'ISOLATION tient ici. Il crée ses propres bacs, à côté de ceux du
serveur (mêmes réglages SANDBOX_*, mais des délais courts), déroule les
cas — état gardé, sortie bornée, délai, réseau, bombe de processus,
mémoire, racine en lecture seule, cloisonnement entre clients, expiration,
langages compilés, processus laissés derrière soi, fichiers d'entrée
déposés —, juge chacun, mesure le démarrage à froid et à chaud, puis
détruit ses bacs.
Code de sortie 0 si aucun cas n'ÉCHOUE. Il n'a besoin ni du jeton, ni du
proxy, ni du réseau : il parle à podman, pas au serveur. Ses bacs ont
leurs propres uid (UID_BASE + 1000…) : ils ne pèsent pas sur ceux d'une
conversation en cours.

Trois verdicts : OK ; ÉCHEC (l'isolation ou la logique ne tient pas : ne
pas activer l'outil) ; NON BORNÉ (une limite que rien ne tient par bac :
mémoire, CPU ou processus si le cgroup du conteneur n'est pas délégué —
`writable-cgroups=true` manque — et alors seul le plafond du conteneur
exécuteur protège ; ou un processus laissé en arrière-plan, attendu : voir
docs/execution-de-code.md).

    --podman CMD   la commande podman (défaut : podman)
    --light        sans les cas qui chargent la machine ou veulent un vrai
                   bac (bombe de processus, mémoire, lecture seule,
                   contenu du bac, processus laissé derrière soi) —
                   obligatoire contre la doublure.

La dernière ligne, « VERDICT : … », est celle à relever, avec les lignes
« MESURES » et celles du cas 6quinquies (Go) : ce que la procédure de
docs/execution-de-code.md demande de noter.
"""

import argparse
import asyncio
import os
import re
import shlex
import sys
import time

from .egress import Egress, parse_hosts
from .sandbox import UID_BASE, File, Limits, SandboxError, Sandboxes

# Les uid des bacs d'ici, à l'écart de ceux du serveur.
UID = UID_BASE + 1000

FAILED, UNBOUNDED = [], []


def zombies() -> int:
    """Processus zombies visibles d'ici (/proc)."""
    count = 0
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            try:
                with open(f"/proc/{entry}/stat") as fh:
                    count += fh.read().rsplit(")", 1)[1].split()[0] == "Z"
            except OSError:
                pass
    return count


def pid1() -> str:
    try:
        with open("/proc/1/comm") as fh:
            return fh.read().strip()
    except OSError:
        return "?"


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


def _bytes(size: str) -> int:
    """Une taille à la podman (512m, 1.5g, 1536m, 1000000) en octets."""
    size = size.strip().lower()
    unit = {"k": 2**10, "m": 2**20, "g": 2**30}.get(size[-1:], 1)
    return int(float(size.rstrip("kmgb") or 0) * unit)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--podman", default="podman")
    ap.add_argument("--light", action="store_true")
    a = ap.parse_args()

    base = Limits.from_env()
    # 180 s au plus par programme (le contrôle du bac, l'installation de
    # Go) ; chaque cas demande le sien (3 s par défaut).
    limits = Limits(**{**base.__dict__, "timeout": 180, "idle": 2.0,
                       "max_output": 2000, "max_file_bytes": 50_000,
                       "max_sessions": 4, "max_sessions_per_client": 4})
    image = os.environ.get("SANDBOX_IMAGE", "").strip()
    rootfs = "" if image else os.environ.get(
        "SANDBOX_ROOTFS", "/opt/sandbox/rootfs").strip()
    # Les prises de la sortie dans un dossier à part de celui du serveur.
    relay = Egress("/tmp/sbx-egress-validate", parse_hosts(base.egress_hosts),
                   max_bytes=base.egress_max_bytes) if base.egress else None
    box = Sandboxes(rootfs, image, limits, podman=shlex.split(a.podman),
                    uid_base=UID, egress=relay)

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
        t = time.monotonic()
        heavy = await run("clientA", "conv1", "python", (
            "import pandas, matplotlib.pyplot as plt\n"
            "plt.plot([1, 2, 3]); plt.savefig('/tmp/x.png'); print('ok')\n"),
            t=30)
        heavy_s = time.monotonic() - t
        node = await run("clientA", "conv1", "node", "console.log(6 * 7)")
        php = await run("clientA", "conv1", "php", "<?php echo 6 * 7;")
        print(f"   MESURES : à froid {cold:.2f}s ; à chaud {warm:.2f}s ; "
              f"5 × print(1) : min {min(times):.2f}s, max {max(times):.2f}s ; "
              f"import pandas + matplotlib et un tracé : {heavy_s:.2f}s "
              f"(code {heavy.exit_code}) ; "
              f"node {node.seconds:.2f}s ({node.output.strip()}) ; "
              f"php {php.seconds:.2f}s ({php.output.strip()})")

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
            # Les interfaces du bac, demandées au NOYAU : /sys/class/net,
            # lui, montre celles du conteneur exécuteur (podman imbriqué
            # ne monte pas un sysfs neuf, il reprend celui de son hôte).
            "print('interfaces', sorted(n for _, n in socket.if_nameindex()))\n"))
        verdict("6. réseau : aucun (ni route, ni interface autre que lo)",
                "RESEAU OUVERT" not in out.output
                and (a.light or "interfaces ['lo']" in out.output), out,
                out.output.strip().replace("\n", " ; "))

        if relay:
            # La sortie ouverte : un paquet s'installe par le relais, un site
            # ordinaire est refusé PAR LE RELAIS, et le bac n'a toujours ni
            # route ni interface (le cas 6 vient de le dire).
            out = await run("clientA", "pkg", "bash",
                            "pip install --quiet cowsay && python3 -c "
                            "'import cowsay; print(\"PAQUET\", cowsay.__name__)'",
                            t=120)
            verdict("6bis. sortie ouverte : pip installe un paquet (PyPI)",
                    out.exit_code == 0 and "PAQUET cowsay" in out.output, out)
            out = await run("clientA", "pkg", "python", (
                "import urllib.request\n"
                "try:\n"
                "    urllib.request.urlopen('https://example.com', timeout=10); print('SITE OUVERT')\n"
                "except Exception as e:\n"
                "    print('refusé :', e)\n"), t=30)
            verdict("6ter. sortie ouverte : un site ordinaire est refusé",
                    "SITE OUVERT" not in out.output and "403" in out.output, out,
                    out.output.strip()[:300])
            # `devbox dev-env` : un environnement léger, par mise, dans le
            # tmpfs /opt/devbox du bac ; il sert dans l'appel suivant.
            out = await run("clientA", "pkg", "bash",
                            "devbox dev-env go >/tmp/devenv.log 2>&1; "
                            "echo code=$?; tail -3 /tmp/devenv.log", t=180)
            # Le Go de mise (/opt/devbox), pas celui de l'image.
            out2 = await run("clientA", "pkg", "bash",
                             "command -v go; go version", t=30)
            verdict("6quater. devbox dev-env go : installé par mise, puis lancé",
                    "code=0" in out.output and out2.exit_code == 0
                    and "/opt/devbox/" in out2.output, out2,
                    (out.output + " | " + out2.output).strip()
                    .replace("\n", " ; ")[:400])
            # Go par le chemin de l'outil, dans CE bac (les cas suivants le
            # font expirer) : le source sur l'entrée standard, compilé dans
            # /tmp, exécuté — sous le délai de l'outil (30 s). Une seconde
            # fois : la première paie le cache de sa bibliothèque standard.
            hello = 'package main\nimport "fmt"\nfunc main() { fmt.Println(42) }'
            slow = []
            for _ in range(2):
                out = await run("clientA", "pkg", "go", hello, t=30)
                good = out.exit_code == 0 and out.output.strip() == "42" \
                    and not out.files
                verdict("6quinquies. go (installé) : compilé et exécuté en "
                        "moins de 30 s, rien dans /work", good, out)
                if good and out.seconds > 15:
                    slow.append(f"go {out.seconds:.0f}s")
            out = await run("clientA", "pkg", "sh",
                            "du -sm /tmp /opt/devbox 2>/dev/null | cut -f1 | tr '\\n' ' '; "
                            "df -m /tmp | tail -1")
            print(f"   MESURES : Mo de /tmp et de /opt/devbox après Go, puis "
                  f"df /tmp : {out.output.strip()!r}"
                  + (f" ; LENT (plus de 15 s) : {', '.join(slow)} — monter "
                     f"[tools.code_execution].timeout" if slow else ""))

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

            # Les enfants de la bombe dorment 2 s : les laisser finir,
            # sinon ils tiennent encore le plafond de processus du bac
            # et le cas 9 n'a plus de quoi lancer une commande.
            await asyncio.sleep(2.5)

            # Le DOUBLE de la borne du bac (SANDBOX_MEMORY), quelle qu'elle
            # soit : demander moins ne prouverait rien.
            ask = 2 * _bytes(limits.memory)
            out = await run("clientA", "conv1", "python", (
                f"b = bytearray(b'\\x01') * {ask}\n"
                "print('ALLOUE', len(b))\n"))
            bounded = "ALLOUE" not in out.output
            verdict(f"8. mémoire : {ask / 1e9:.1f} Go demandés, bac borné à "
                    f"{limits.memory}",
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
            verdict("12. contenu du bac (check.py) : bibliothèques Python, "
                    "commandes, Node.js, PHP, devbox dev-env", out.exit_code == 0, out,
                    out.output.strip()[-400:])


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

        # Un fichier d'ENTRÉE : déposé par l'entrée standard de `podman
        # exec` (tar), sous l'uid du bac, à la place du lien qu'un
        # programme précédent a posé à son nom — jamais à travers lui.
        await run("clientA", "depot", "python", (
            "import os\n"
            "os.symlink('piege.txt', 'data.csv')\n"
            "os.symlink('/tmp/sbx-piege.txt', 'notes.txt')\n"))
        out = await box.execute("clientA", "depot", "python", (
            "import os\n"
            "print(open('data.csv').read().strip(), open('notes.txt').read(),\n"
            "      os.path.islink('data.csv'), os.path.islink('notes.txt'),\n"
            "      os.path.lexists('piege.txt'),\n"
            "      os.path.lexists('/tmp/sbx-piege.txt'),\n"
            "      os.stat('data.csv').st_uid == os.getuid(),\n"
            "      oct(os.stat('data.csv').st_mode & 0o777))\n"),
            timeout=3, inputs=[File("data.csv", b"a,b\n1,2\n"),
                               File("notes.txt", b"notes"),
                               File("../evade.txt", b"x"),
                               File(".cache", b"x")])
        verdict("15. fichiers d'entrée : déposés sous l'uid du bac, les liens "
                "piégés remplacés et non suivis, les noms à chemin refusés, "
                "rien à la récolte",
                out.exit_code == 0 and out.output.split() == [
                    "a,b", "1,2", "notes", "False", "False", "False", "False",
                    "True", "0o644"]
                and out.inputs == [{"name": "data.csv", "size": 8},
                                   {"name": "notes.txt", "size": 5}]
                and [r["reason"] for r in out.rejected] == ["invalid name"] * 2
                and not out.files, out,
                f"déposés : {out.inputs} ; refusés : {out.rejected} ; "
                f"récoltés : {[f.name for f in out.files]} ; "
                + out.output.strip().replace("\n", " ; "))
        out = await run("clientA", "depot", "sh",
                        "echo '3,4' >> data.csv; cat notes.txt > /dev/null")
        verdict("15bis. fichier d'entrée MODIFIÉ par le programme : rendu "
                "comme un fichier produit, l'autre non",
                [f.name for f in out.files] == ["data.csv"]
                and out.files[0].data == b"a,b\n1,2\n3,4\n", out,
                f"récoltés : {[f.name for f in out.files]}")
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
        # Chaque `podman run` / `podman exec` laisse un conmon orphelin :
        # c'est au PID 1 du conteneur de l'enterrer (l'init du point
        # d'entrée). Sans lui les zombies s'accumulent jusqu'au plafond de
        # processus, et plus aucun bac ne se crée.
        await asyncio.sleep(1)
        verdict("16. aucun zombie laissé dans le conteneur exécuteur "
                "(le PID 1 est un init)", zombies() == 0,
                detail=f"zombies : {zombies()} ; PID 1 : {pid1()}")

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
    if os.geteuid() == 0:
        # `docker compose exec executor …` entre en root (le point d'entrée
        # de l'image y démarre) : podman, lui, est celui de `executor`.
        os.execvp("setpriv", [
            "setpriv", "--reuid", "executor", "--regid", "executor",
            "--init-groups", "env", "HOME=/home/executor",
            sys.executable, "-m", "executor.validate", *sys.argv[1:]])
    sys.exit(asyncio.run(main()))
