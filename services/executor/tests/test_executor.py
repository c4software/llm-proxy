"""L'exécuteur de code (executor/) : son API HTTP, par le client de test
de Starlette, contre une DOUBLURE de podman (tests/fake_podman.py) — un
« conteneur » y est un dossier, et le programme tourne sur la machine du
test, SANS aucune isolation. Ce qui est vérifié ici : la logique —
sessions et leur clé, état gardé, dépôt des fichiers d'entrée, récolte
et plafonds des fichiers, délais, quotas, expiration, orphelins, sonde
des cgroups, jeton. Ce qui
ne l'est PAS : tout ce que podman fait des drapeaux qu'on lui passe
(réseau, mémoire, processus, lecture seule, uid) — un test vérifie
seulement qu'ils sont demandés.

Les programmes d'essai sont minuscules et n'écrivent que dans le dossier
du « conteneur », sous tmp_path."""

import base64
import json
import os
import shutil
import sys
import time

import pytest
from fastapi.testclient import TestClient

from executor import sandbox, server

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "fake_podman.py")
TOKEN = "jeton-d-essai"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def service(tmp_path, monkeypatch):
    """Fabrique un exécuteur démarré : `service(**bornes)` rend (client
    HTTP, bacs). Le dossier des faux conteneurs est `service.root`."""
    monkeypatch.setenv("FAKE_PODMAN_ROOT", str(tmp_path))
    opened = []

    def make(token=TOKEN, **limits):
        box = sandbox.Sandboxes(
            rootfs="/opt/sandbox/rootfs",
            limits=sandbox.Limits(**{"timeout": 10, **limits}),
            podman=(sys.executable, FAKE))
        client = TestClient(server.create_app(box, token))
        client.__enter__()          # le démarrage : orphelins, sonde
        opened.append(client)
        return client, box

    make.root = tmp_path
    yield make
    for client in opened:
        client.__exit__(None, None, None)


def run(client, code, language="python", session="conv1", who="clientA",
        **extra):
    r = client.post("/v1/execute", headers=AUTH, json={
        "client": who, "session": session, "language": language,
        "code": code, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def names(doc):
    return [f["name"] for f in doc["files"]]


def test_jeton_exige_sauf_sante(service):
    client, _ = service()
    assert client.get("/healthz").json() == {"status": "ok"}
    for headers in ({}, {"Authorization": "Bearer autre"},
                    {"Authorization": TOKEN},
                    {"Authorization": "Bearer é".encode()}):
        for method, path in (("GET", "/v1/status"), ("POST", "/v1/execute"),
                             ("POST", "/v1/destroy")):
            r = client.request(method, path, headers=headers, json={})
            assert r.status_code == 401, (headers, path)
            assert r.json()["error"]["code"] == "unauthorized"
    assert client.get("/v1/status", headers=AUTH).json()["status"] == "ok"
    # Sans jeton configuré : jamais ouvert, et la santé le dit.
    client, _ = service(token="")
    assert client.get("/healthz").status_code == 503
    r = client.post("/v1/execute", headers=AUTH, json={})
    assert (r.status_code, r.json()["error"]["code"]) == (503, "not_ready")


def test_etat_garde_et_fichiers_rendus(service):
    client, box = service()
    png = b"\x89PNG\r\n\x1a\n" + bytes(range(64))
    doc = run(client, (
        "import os\n"
        "open('notes.txt', 'w').write('état gardé\\n')\n"
        f"open('graphique.png', 'wb').write({png!r})\n"
        "os.makedirs('out/__pycache__'); os.makedirs('.cache')\n"
        "open('out/table.csv', 'w').write('a,b\\n1,2\\n')\n"
        "open('out/__pycache__/x.pyc', 'w').write('x')\n"
        "open('.cache/y', 'w').write('y'); open('.env', 'w').write('z')\n"
        "print('fait')\n"))
    assert (doc["exit_code"], doc["timed_out"], doc["output"]) == (0, False, "fait\n")
    assert doc["fresh"] is True and doc["reset"] is False
    # Ni fichier caché, ni cache, ni le repère : les fichiers PRODUITS.
    assert names(doc) == ["graphique.png", "notes.txt", "out/table.csv"]
    assert base64.b64decode(doc["files"][0]["data"]) == png
    assert doc["files"][0]["size"] == len(png) and doc["skipped"] == []

    # Autre langage, même bac : le fichier est là. Seul ce qui CHANGE revient.
    doc = run(client, "cat notes.txt; echo suite >> notes.txt; ls", "bash")
    assert doc["fresh"] is False
    assert doc["output"].startswith("état gardé\n") and "graphique.png" in doc["output"]
    assert names(doc) == ["notes.txt"]
    assert base64.b64decode(doc["files"][0]["data"]).decode() == "état gardé\nsuite\n"
    assert run(client, "print(1)")["files"] == []
    assert len(box) == 1

    # Ce que podman reçoit pour créer le bac : l'isolation est DEMANDÉE
    # (la doublure ne l'applique pas).
    (name,) = os.listdir(service.root)
    args = json.load(open(service.root / name / ".args"))
    line = " ".join(args)
    for wanted in ("--network none", "--read-only --read-only-tmpfs=false",
                   "--cap-drop ALL", "--security-opt no-new-privileges",
                   "--user 20000:20000", "--rootfs /opt/sandbox/rootfs",
                   "--tmpfs /work:rw,exec,size=256m,mode=1777",
                   "--tmpfs /tmp:rw,exec,size=128m,mode=1777",
                   # Un fil par cœur ALLOUÉ, pas par cœur de l'hôte ; ni
                   # module Go ni crate à chercher.
                   "--env OPENBLAS_NUM_THREADS=1", "--env GOMAXPROCS=1",
                   "--env GOPROXY=off", "--env CARGO_NET_OFFLINE=true",
                   "--memory 512m --memory-swap 512m", "--cpus 1.0",
                   "--pids-limit 128", "--ulimit nproc=128:128",
                   "--init", "--timeout 14400", "--label llm-proxy.sandbox=1"):
        assert wanted in line, wanted
    assert "--volume" not in line and "-v" not in args
    assert args[-2:] == ["sleep", "infinity"]


def test_erreur_du_programme_delai_sortie_bornee(service):
    client, _ = service(max_output=2000)
    run(client, "open('avant.txt', 'w').write('x')")
    # Un programme en erreur est un RÉSULTAT : 200, son code, sa trace.
    doc = run(client, "import sys; print('a'); sys.exit(3)")
    assert (doc["exit_code"], doc["timed_out"], doc["output"]) == (3, False, "a\n")
    doc = run(client, "raise ValueError('non')")
    assert doc["exit_code"] == 1 and "ValueError: non" in doc["output"]
    assert run(client, "exit 125", "sh")["exit_code"] == 125   # le sien, pas podman
    # Délai DEMANDÉ (1 s), sous celui de l'exécuteur ; ce qui était déjà
    # sorti est rendu, et le bac survit.
    started = time.monotonic()
    doc = run(client, "import time; print('début', flush=True); time.sleep(60)",
              timeout=1)
    assert (doc["exit_code"], doc["timed_out"]) == (None, True)
    assert doc["output"] == "début\n" and time.monotonic() - started < 8
    doc = run(client, "ls", "sh")
    assert doc["fresh"] is False and "avant.txt" in doc["output"]
    # Sortie trop longue : le début ET la fin, le milieu compté.
    doc = run(client, "print('DEBUT' + 'x' * 100_000 + 'FIN')")
    assert doc["truncated"] is True and len(doc["output"]) < 2200
    assert doc["output"].startswith("DEBUT") and doc["output"].endswith("FIN\n")
    assert "bytes of output omitted" in doc["output"]


def test_fichiers_plafonnes_et_noms_hostiles(service):
    client, _ = service(max_files=3, max_file_bytes=1000, max_total_bytes=1500)
    doc = run(client, (
        "import os\n"
        "open('a.txt', 'w').write('a' * 900)\n"
        "open('b.txt', 'w').write('b' * 900)\n"         # total dépassé
        "open('c.txt', 'w').write('c' * 10)\n"
        "open('d.txt', 'w').write('d' * 10)\n"          # un de trop
        "open('gros.bin', 'w').write('0' * 5000)\n"
        "open('-rf', 'w').write('option ?')\n"          # lu comme un nom
        "os.symlink('/etc/passwd', 'lien')\n"           # pas un fichier
        "os.mkfifo('tube')\n"))
    assert names(doc) == ["-rf", "a.txt", "c.txt"]
    assert {(s["name"], s["reason"]) for s in doc["skipped"]} == {
        ("b.txt", "total too large"), ("d.txt", "too many files"),
        ("gros.bin", "too large")}
    assert base64.b64decode(doc["files"][0]["data"]) == b"option ?"


def test_fichiers_d_entree_deposes(service):
    """`inputs` : déposés dans /work avant le programme, sous des noms et
    des bornes jugés ICI ; jamais à travers un lien laissé par un appel
    précédent ; pas repris à la récolte, sauf modifiés. Le `tar` qui
    déplie est celui de la machine du test (GNU tar attendu)."""
    client, _ = service(max_inputs=5, max_input_bytes=1000,
                        max_input_total_bytes=1500)
    ailleurs, absent = service.root / "ailleurs.txt", service.root / "absent"
    ailleurs.write_text("intact")
    # Un programme précédent du même bac a piégé les noms à venir.
    run(client, (
        "import os\n"
        f"os.symlink({str(ailleurs)!r}, 'data.csv')\n"
        f"os.symlink({str(absent)!r}, 'notes.txt')\n"
        "os.makedirs('dossier.csv'); open('dossier.csv/x', 'w').write('x')\n"))

    def entree(name, data):
        return {"name": name, "data": base64.b64encode(data).decode()}
    doc = run(client, (
        "import os\n"
        "print(open('data.csv').read(), os.path.islink('data.csv'),\n"
        "      os.path.islink('notes.txt'), os.stat('data.csv').st_uid == os.getuid())\n"
        "open('notes.txt', 'a').write(' et la suite')\n"), inputs=[
            entree("data.csv", b"a,b\n1,2\n"), entree("notes.txt", b"notes"),
            entree("dossier.csv", b"0123456789"), entree("a.bin", b"a" * 900),
            entree("lourd.bin", b"b" * 900), entree("d.txt", b"d"),
            entree("e.txt", b"e"), entree("gros.bin", b"0" * 1001),
            entree("data.csv", b"autre"), entree("../evade", b"x"),
            entree("sous/fichier", b"x"), entree(".cache", b"x"),
            entree("-rf", b"x"), entree("", b"x"), entree("a b", b"x")])
    assert doc["output"] == "a,b\n1,2\n False False True\n", doc
    assert doc["inputs"] == [{"name": "data.csv", "size": 8},
                             {"name": "notes.txt", "size": 5},
                             {"name": "a.bin", "size": 900},
                             {"name": "d.txt", "size": 1}]
    assert [(r["name"], r["reason"]) for r in doc["rejected"]] == [
        ("lourd.bin", "total too large"), ("e.txt", "too many files"),
        ("gros.bin", "too large"), ("data.csv", "duplicate name"),
        ("../evade", "invalid name"), ("sous/fichier", "invalid name"),
        (".cache", "invalid name"), ("-rf", "invalid name"),
        ("", "invalid name"), ("a b", "invalid name"),
        ("dossier.csv", "could not be written")]
    # Les liens ont été REMPLACÉS, pas suivis ; rien n'est sorti de /work.
    assert ailleurs.read_text() == "intact" and not absent.exists()
    assert sorted(p.name for p in service.root.iterdir() if p.is_file()) \
        == ["ailleurs.txt"]
    # Seul le fichier d'entrée que le programme a MODIFIÉ est un fichier
    # produit ; les autres restent dans le bac, sans repartir.
    assert names(doc) == ["notes.txt"]
    assert base64.b64decode(doc["files"][0]["data"]) == b"notes et la suite"
    doc = run(client, "cat data.csv; ls", "sh")
    assert doc["output"] == "a,b\n1,2\na.bin\nd.txt\ndata.csv\ndossier.csv\nnotes.txt\n"
    # Sans `inputs` (un proxy plus ancien) : rien de déposé, et les deux
    # champs sont là, vides.
    assert (doc["files"], doc["inputs"], doc["rejected"]) == ([], [], [])
    # Un fichier déposé de nouveau REMPLACE le premier, sans compter comme
    # produit.
    doc = run(client, "cat data.csv", "sh", inputs=[entree("data.csv", b"neuf")])
    assert (doc["output"], doc["files"]) == ("neuf", [])


def test_cloisonnement_par_client_et_bac_jetable(service):
    client, box = service()
    run(client, "open('secret.txt', 'w').write('A')")
    # Même identifiant de session, AUTRE client : un autre bac, vide.
    doc = run(client, "ls -A; echo fin", "sh", who="clientB")
    assert doc["fresh"] is True and doc["output"] == ".sbx_stamp\nfin\n"
    assert len(box) == 2
    # Sans session : un bac par appel, détruit aussitôt.
    doc = run(client, "open('x.txt', 'w').write('x'); print(1)", session="")
    assert doc["fresh"] is True and names(doc) == ["x.txt"]
    doc = run(client, "ls; echo fin", "sh", session="")
    assert doc["output"] == "fin\n"
    assert len(box) == 2 and len(os.listdir(service.root)) == 2
    assert client.get("/v1/status", headers=AUTH).json()["sessions"] == 2


def test_quotas_expiration_destruction_orphelins(service):
    os.makedirs(service.root / "sbx-orphelin" / "work")
    client, box = service(max_sessions_per_client=2, max_sessions=3, idle=3600)
    assert os.listdir(service.root) == []       # détruit au démarrage
    for conv in ("c1", "c2"):
        run(client, "open('f.txt', 'w').write('x')", session=conv)
    run(client, "print(1)", session="c1")       # c1 sert : c2 est le plus ancien
    # Quota du client atteint : le bac le moins récemment utilisé cède sa
    # place, et son propriétaire retrouve un bac NEUF.
    run(client, "print(1)", session="c3")
    assert len(box) == 2
    assert run(client, "ls", "sh", session="c1")["fresh"] is False
    doc = run(client, "ls; echo fin", "sh", session="c2")
    assert doc["fresh"] is True and doc["output"] == "fin\n"
    # Quota commun : d'autres clients, puis le client sans nom d'un proxy
    # ouvert, qui n'a PAS de quota à lui.
    for n in range(5):
        run(client, "print(1)", who="", session=f"o{n}")
    assert len(box) == 3

    # Destruction demandée ; inconnue ou d'un autre client : rien.
    def destroy(who, session):
        return client.post("/v1/destroy", headers=AUTH, json={
            "client": who, "session": session}).json()["destroyed"]
    assert destroy("clientB", "o4") is False and destroy("", "o4") is True
    assert len(box) == 2 and len(os.listdir(service.root)) == 2

    # Expiration : par inactivité (le serveur appelle sweep() toutes les
    # 30 s ; ici, à la main).
    import asyncio
    box.limits = sandbox.Limits(idle=0.05)
    time.sleep(0.1)
    assert asyncio.run(box.sweep()) == 2
    assert len(box) == 0 and os.listdir(service.root) == []


def test_tous_les_bacs_occupes(service):
    """Un bac qui exécute ne cède pas sa place : 429 `busy`."""
    import threading
    client, _ = service(max_sessions=1)
    slow = threading.Thread(target=run, args=(
        client, "import time; time.sleep(1.5)"), kwargs={"session": "lent"})
    slow.start()
    time.sleep(0.6)
    r = client.post("/v1/execute", headers=AUTH, json={
        "client": "clientA", "session": "autre", "language": "sh",
        "code": "echo 1"})
    slow.join()
    assert (r.status_code, r.json()["error"]["code"]) == (429, "busy")
    assert run(client, "echo 1", "sh", session="autre")["output"] == "1\n"


def test_bac_disparu_recree(service):
    """Le conteneur a disparu derrière l'exécuteur (durée de vie tenue
    par podman) : un bac neuf, le programme tourne, le modèle le saura."""
    client, box = service()
    run(client, "open('f.txt', 'w').write('x')")
    (name,) = os.listdir(service.root)
    shutil.rmtree(service.root / name)
    doc = run(client, "ls; echo fin", "sh")
    assert (doc["exit_code"], doc["fresh"], doc["reset"]) == (0, True, False)
    assert doc["output"] == "fin\n" and len(box) == 1
    assert os.listdir(service.root) != [name]

    # Détruit PENDANT qu'un second appel de la session attend son tour :
    # celui-ci repart d'un bac neuf, pas d'un conteneur qui n'existe plus.
    import asyncio

    async def deux_appels():
        lent = asyncio.create_task(box.execute("c", "s", "sh", "sleep 0.6"))
        await asyncio.sleep(0.3)
        suivant = asyncio.create_task(box.execute("c", "s", "sh", "echo ok"))
        await asyncio.sleep(0.1)
        assert await box.destroy("c", "s") is True
        await lent
        return await suivant
    out = asyncio.run(deux_appels())
    assert (out.exit_code, out.fresh, out.output) == (0, True, "ok\n")


@pytest.mark.parametrize("language, compiler, code", [
    ("c", "gcc", '#include <stdio.h>\n#include <math.h>\n'
                 'int main(void) { FILE *f = fopen("r.txt", "w"); '
                 'fprintf(f, "%.0f", sqrt(1764)); fclose(f); puts("ok"); '
                 'return 3; }'),
    ("cpp", "g++", '#include <iostream>\n#include <fstream>\n'
                   'int main() { std::ofstream("r.txt") << 6 * 7; '
                   'std::cout << "ok" << std::endl; return 3; }'),
    ("go", "go", 'package main\nimport ("fmt"; "os")\n'
                 'func main() { os.WriteFile("r.txt", []byte("42"), 0644); '
                 'fmt.Println("ok"); os.Exit(3) }'),
    ("rust", "rustc", 'fn main() { std::fs::write("r.txt", "42").unwrap(); '
                      'println!("ok"); std::process::exit(3); }'),
])
def test_langage_compile(service, monkeypatch, tmp_path, language, compiler,
                         code):
    """Un langage compilé : le source arrive par l'entrée standard, est
    compilé HORS du dossier de travail (ni le source ni le binaire ne
    sont des fichiers produits), puis exécuté dedans. Une erreur de
    compilation est un résultat comme un autre. Sauté sans le compilateur
    — la doublure compile sur la machine du test."""
    if shutil.which(compiler) is None:
        pytest.skip(f"{compiler} absent")
    build = tmp_path / "tmp"
    build.mkdir()
    monkeypatch.setenv("TMPDIR", str(build))
    client, _ = service(timeout=60)
    doc = run(client, code, language)
    assert (doc["exit_code"], doc["output"]) == (3, "ok\n"), doc
    assert names(doc) == ["r.txt"]
    assert base64.b64decode(doc["files"][0]["data"]) == b"42"
    doc = run(client, "ceci n'est pas un programme", language)
    assert doc["exit_code"] not in (0, None) and doc["output"].strip()
    assert doc["files"] == []


def test_sonde_des_cgroups(service, monkeypatch):
    """Ce que podman refuse ici n'est plus demandé, et l'état le dit — de
    même ce qu'il ACCEPTE sans le tenir (pas de cgroup délégué : le bac
    lit la même valeur avec et sans le drapeau)."""
    monkeypatch.setenv("FAKE_PODMAN_IGNORE", "--memory")
    client, box = service()
    status = client.get("/v1/status", headers=AUTH).json()
    assert status["cgroup"] == {"memory": False, "cpu": True, "pids": True}
    monkeypatch.delenv("FAKE_PODMAN_IGNORE")
    monkeypatch.setenv("FAKE_PODMAN_DENY", "--memory,--cpus")
    client, box = service()
    status = client.get("/v1/status", headers=AUTH).json()
    assert status["cgroup"] == {"memory": False, "cpu": False, "pids": True}
    assert status["limits"]["memory"] == "512m" and "python" in status["languages"]
    assert run(client, "print(1)")["output"] == "1\n"
    (name,) = os.listdir(service.root)
    args = json.load(open(service.root / name / ".args"))
    assert "--memory" not in args and "--cpus" not in args
    assert "--pids-limit" in args and "--ulimit" in args

    # podman ne démarre RIEN : le service répond, et dit qu'il est en panne.
    monkeypatch.setenv("FAKE_PODMAN_DOWN", "1")
    client, _ = service()
    assert client.get("/healthz").status_code == 503
    status = client.get("/v1/status", headers=AUTH).json()
    assert status["status"] == "error" and "Operation not permitted" in status["error"]
    r = client.post("/v1/execute", headers=AUTH, json={
        "client": "", "session": "", "language": "sh", "code": "echo 1"})
    assert (r.status_code, r.json()["error"]["code"]) == (503, "not_ready")


def test_requetes_invalides(service):
    client, box = service()
    ok = {"client": "c", "session": "s", "language": "python", "code": "1"}
    for body in ([], "x", {**ok, "language": "cobol"}, {**ok, "language": None},
                 {**ok, "code": ""}, {**ok, "code": 3}, {**ok, "client": 3},
                 {**ok, "session": "s" * 129}, {**ok, "timeout": "long"},
                 {**ok, "timeout": True}, {**ok, "inputs": "a.csv"},
                 {**ok, "inputs": 3}, {**ok, "inputs": ["a.csv"]},
                 {**ok, "inputs": [{"name": "a.csv"}]},
                 {**ok, "inputs": [{"name": 3, "data": "eA=="}]},
                 {**ok, "inputs": [{"name": "a.csv", "data": "%%%"}]},
                 {**ok, "inputs": [{"name": "a", "data": ""}] * 65}):
        r = client.post("/v1/execute", headers=AUTH, json=body)
        assert r.status_code == 400, body
        assert r.json()["error"]["code"] == "invalid_request"
    r = client.post("/v1/execute", headers=AUTH, content=b"{pas du json")
    assert r.status_code == 400
    # Un corps au-delà de ce que le code et les entrées permettent : pas lu.
    box.limits = sandbox.Limits(max_input_total_bytes=3000)
    r = client.post("/v1/execute", headers=AUTH, json={
        **ok, "inputs": [{"name": "a.bin", "data": "A" * 2_010_000}]})
    assert (r.status_code, r.json()["error"]["code"]) == (413, "too_large")
    assert len(box) == 0 and os.listdir(service.root) == []


def test_bornes_par_l_environnement():
    lim = sandbox.Limits.from_env({"SANDBOX_MEMORY": "1g", "SANDBOX_PIDS": "64",
                                   "SANDBOX_CPUS": "0.5", "SANDBOX_IDLE": ""})
    assert (lim.memory, lim.pids, lim.cpus, lim.idle) == ("1g", 64, 0.5, 10800.0)
    with pytest.raises(SystemExit, match="SANDBOX_PIDS"):
        sandbox.Limits.from_env({"SANDBOX_PIDS": "beaucoup"})
