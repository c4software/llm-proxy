"""Les dépôts (storage/repos.py, tools/repository.py, repository_bac.py,
ui/repo_routes.py) : les sources par compte et partagées, l'archive de
chaque forge (par net.download), le fetch git, les commandes tree, read et
grep lues dans le bac, le cache. Sans réseau : DNS et forge sont faux
(fakes.fake_dns, un MockTransport) ; le fetch git part d'un dépôt local ; le
faux exécuteur exécute vraiment le script du bac, dans un dossier."""

import asyncio
import base64
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile

import httpx2
import pytest

from fakes import PUBLIC, fake_dns, go, net_guard

from llm_proxy.access import auth
from llm_proxy.core.settings import INCOGNITO
from llm_proxy.storage import conversations, repos
from llm_proxy.tools import code_execution, repository
from llm_proxy.tools.contract import Call, ToolError

HOSTS = ("g.lan", "evil.example", "gitlab.exemple.fr", "api.github.com", "codeload.github.com",
         "gitea.lan")


@pytest.fixture(autouse=True)
def base(monkeypatch, tmp_path):
    monkeypatch.setattr(conversations, "DB_PATH", str(tmp_path / "conversations.db"))
    monkeypatch.setattr(repository, "ENABLED", True)
    monkeypatch.setattr(code_execution, "URL", "http://executor.invalid")
    monkeypatch.setattr(code_execution, "TOKEN", "t")
    monkeypatch.setattr(repos, "SHARED", [])
    net_guard(monkeypatch)
    fake_dns(monkeypatch, {h: [PUBLIC] for h in HOSTS})
    repository.CACHE.clear()


FILES = {"README.md": b"# Backend\n", "requirements.txt": b"fastapi==0.1\nmcp==1.2.0\n",
         "src/app.py": b"import mcp\nprint('hello')\n", "src/logo.png": b"\x89PNG\0\0data"}


def targz(top="proj-main-abc", files=None, extra=()) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in (files or FILES).items():
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for info in extra:
            tar.addfile(info, io.BytesIO(b"x" * info.size) if info.size else None)
    return buf.getvalue()


def forge(seen, routes):
    """Une forge : `routes` {(hôte, chemin brut ?requête) ou chemin brut :
    (statut, corps, en-têtes)}."""
    def handler(request):
        seen.append(request)
        host, raw = request.headers["host"], request.url.raw_path.decode()
        for key in ((host, raw), raw, raw.split("?")[0]):
            if key in routes:
                status, body, headers = routes[key]
                return httpx2.Response(status, content=body, headers=headers)
        return httpx2.Response(404)
    return httpx2.MockTransport(handler)


# ── le magasin ──────────────────────────────────────────────────────────

GITLAB = {"name": "ACME", "forge": "gitlab", "url": "https://gitlab.exemple.fr"}


def test_jeton_garde_jamais_rendu_et_source_cloisonnee():
    s = repos.create("a", {**GITLAB, "url": "https://gitlab.exemple.fr/", "token": "glpat-secret",
                           "projects": "equipe/*, outils/*"})
    assert s["url"] == "https://gitlab.exemple.fr" and "token" not in s
    assert s["projects"] == ["equipe/*", "outils/*"]
    assert "glpat" not in json.dumps(repos.list_own("a"))
    assert repos.list_own("b") == [] and repos.available("b") == []
    # Modifiée sans ressaisir le jeton : il reste — sauf si la source change
    # d'hôte, où il ne suit pas.
    repos.update("a", s["id"], GITLAB)
    assert repos.available("a")[0]["token"] == "glpat-secret"
    with pytest.raises(repos.RepoError, match="change d'adresse"):
        repos.update("a", s["id"], {**GITLAB, "url": "https://autre.example"})
    assert repos.available("a")[0]["url"] == "https://gitlab.exemple.fr"


def test_source_refusee():
    """Sans jeton pour une forge qui en veut un, du nom d'une autre (sans
    casse), ou avec des identifiants dans l'URL."""
    repos.create("a", {**GITLAB, "token": "t"})
    for body, refus in [
            ({"name": "X", "forge": "gitlab", "url": "https://g.example"}, "jeton"),
            ({"name": "acme", "forge": "gitlab", "url": "https://g.example", "token": "t"}, "déjà"),
            ({"name": "U", "forge": "git", "url": "https://u:jeton@g.example"}, "identifiants")]:
        with pytest.raises(repos.RepoError, match=refus):
            repos.create("a", body)
    assert len(repos.list_own("a")) == 1
    with pytest.raises(repos.RepoError, match="source invalide"):
        repos.delete("a", "../x")


def test_sources_partagees_masquees_par_celles_du_compte(monkeypatch):
    """[repos.sources.<nom>] : offertes à tous, sans leur jeton ; une source
    du compte de même nom l'emporte ; `projects` borne les projets lus."""
    repos.create("a", {**GITLAB, "token": "t"})
    assert repos.create("a", {"name": "GH", "forge": "github", "token": "ghp"})["url"] \
        == "https://github.com"
    monkeypatch.setattr(repos, "SHARED", repos._shared_from({
        "acme": {"forge": "gitlab", "url": "https://gitlab.org.lan", "token": "org"},
        "off": {"forge": "github", "token": "x", "enabled": False}}))
    assert [x["name"] for x in repos.available("b")] == ["acme"]
    assert [x["name"] for x in repos.available("a")] == ["ACME", "GH"]
    assert "token" not in repos.shared()[0]
    limited = {"projects": ["equipe/*"]}
    assert repos.permits(limited, "Equipe/api") and not repos.permits(limited, "outils/backend")


# ── les forges ──────────────────────────────────────────────────────────

def test_archive_de_chaque_forge():
    seen = []
    data = targz()
    t = forge(seen, {
        "/api/v4/projects/outils%2Fbackend/repository/archive.tar.gz?sha=main": (200, data, {}),
        "/repos/o/r/tarball": (302, b"", {"location": "https://codeload.github.com/o/r/tar.gz/x"}),
        "/o/r/tar.gz/x": (200, data, {}),
        "/api/v1/repos/o/r": (200, json.dumps({"default_branch": "dev"}).encode(), {}),
        "/api/v1/repos/o/r/archive/dev.tar.gz": (200, data, {}),
    })
    gl = {"forge": "gitlab", "url": "https://gitlab.exemple.fr", "token": "glpat"}
    assert go(repository.fetch(gl, "outils/backend", "main", t)) == data
    assert seen[-1].headers["private-token"] == "glpat"
    assert seen[-1].headers["accept-encoding"] == "identity"     # borne comptée telle quelle
    gh = {"forge": "github", "url": "https://github.com", "token": "ghp"}
    assert go(repository.fetch(gh, "o/r", "", t)) == data
    assert seen[-2].headers["host"] == "api.github.com"
    assert seen[-2].headers["authorization"] == "Bearer ghp"
    assert "authorization" not in seen[-1].headers           # pas chez codeload
    gt = {"forge": "gitea", "url": "https://gitea.lan", "token": "tok"}
    assert go(repository.fetch(gt, "o/r", "", t)) == data
    assert seen[-1].headers["authorization"] == "token tok"


def test_jeton_pas_suivi_vers_un_autre_hote():
    seen = []
    t = forge(seen, {
        "/api/v4/projects/a%2Fb/repository/archive.tar.gz":
            (302, b"", {"location": "https://g.lan/archives/1.tar.gz"}),
        ("g.lan", "/archives/1.tar.gz"): (302, b"", {"location": "https://evil.example/x.tar.gz"}),
        ("evil.example", "/x.tar.gz"): (200, targz(), {}),
    })
    go(repository.fetch({"forge": "gitlab", "url": "https://g.lan", "token": "glpat"}, "a/b", "", t))
    assert [r.headers.get("private-token") for r in seen] == ["glpat", "glpat", None]


def test_archive_refusee_ou_trop_grosse(monkeypatch):
    gl = {"forge": "gitlab", "url": "https://g.lan", "token": "x"}
    for status, code in ((401, "not_allowed"), (404, "not_accessible"),
                         (429, "too_many_requests")):
        t = forge([], {"/api/v4/projects/a%2Fb/repository/archive.tar.gz": (status, b"", {})})
        with pytest.raises(ToolError) as exc:
            go(repository.fetch(gl, "a/b", "", t))
        assert exc.value.code == code
    monkeypatch.setattr(repository, "MAX_BYTES", 10)
    t = forge([], {"/api/v4/projects/a%2Fb/repository/archive.tar.gz": (200, b"x" * 100, {})})
    with pytest.raises(ToolError, match="larger"):
        go(repository.fetch(gl, "a/b", "", t))


def test_gitea_branche_par_defaut_invalide():
    t = forge([], {"/api/v1/repos/o/r": (200, json.dumps({"default_branch": "../../admin"}).encode(), {})})
    with pytest.raises(ToolError, match="default branch"):
        go(repository.fetch({"forge": "gitea", "url": "https://gitea.lan", "token": "t"}, "o/r", "", t))


def git_repo(tmp_path, project: str, *commits: dict) -> tuple[dict, list[str]]:
    """Un dépôt git local, un commit par dictionnaire {fichier: octets} →
    (la source « git » qui le sert, les commits)."""
    src = tmp_path / "srv" / f"{project}.git"
    src.mkdir(parents=True)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    def git(*a):
        return subprocess.run(["git", "-C", str(src), *a], check=True, env=env,
                              capture_output=True, text=True).stdout.strip()
    git("init", "-q", "-b", "main")
    git("config", "uploadpack.allowAnySHA1InWant", "true")
    shas = []
    for files in commits:
        for name, data in files.items():
            (src / name).write_bytes(data)
        git("add", ".")
        git("commit", "-qm", "x")
        shas.append(git("rev-parse", "HEAD"))
    return {"forge": "git", "url": f"file://{tmp_path}/srv", "token": ""}, shas


def test_fetch_git_une_branche_ou_un_commit(tmp_path):
    """Forge « git » : `git fetch --depth 1` puis `git archive`."""
    source, (first, _) = git_repo(tmp_path, "grp/proj", {"app.py": b"print(1)\n"},
                                  {"app.py": b"print(2)\n"})

    def files(data):
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            return {m.name: tar.extractfile(m).read() for m in tar if m.isfile()}
    assert files(go(repository.fetch(source, "grp/proj", ""))) == {"src/app.py": b"print(2)\n"}
    assert files(go(repository.fetch(source, "grp/proj", first))) == {"src/app.py": b"print(1)\n"}
    with pytest.raises(ToolError, match="git failed"):
        go(repository.fetch(source, "grp/absent", ""))


def test_fetch_git_borne_meme_avec_peu_de_gros_fichiers(tmp_path, monkeypatch):
    """Sous 100 objets, git écrirait des objets libres un par un : forcé en
    pack (fetch.unpackLimit), la borne tient et se dit."""
    source, _ = git_repo(tmp_path, "grp/big",
                         {f"f{i}.bin": os.urandom(400_000) for i in range(5)})
    monkeypatch.setattr(repository, "MAX_BYTES", 1_000_000)
    with pytest.raises(ToolError, match="larger than 1000000"):
        go(repository.fetch(source, "grp/big", ""))


# ── le bac : les commandes, lues par le vrai script ─────────────────────

class Bac:
    """Un faux exécuteur : un dossier par (client, session), où le code
    envoyé tourne VRAIMENT (le Python du test), ses fichiers d'entrée posés
    d'abord — comme dans un bac."""

    def __init__(self, root):
        self.root, self.calls, self.destroyed = root, [], []

    def handler(self, request):
        body = json.loads(request.content)
        if request.url.path == "/v1/destroy":
            self.destroyed.append(body)
            work = self.root / f"{body['client'] or 'open'}-{body['session']}"
            shutil.rmtree(work, ignore_errors=True)
            return httpx2.Response(200, json={"destroyed": True})
        self.calls.append(body)
        # Session «» : un bac d'un seul appel, détruit après lui.
        work = self.root / (f"{body['client'] or 'open'}-{body['session']}" if body["session"]
                            else f"once-{len(self.calls)}")
        work.mkdir(parents=True, exist_ok=True)
        for f in body.get("inputs") or []:
            (work / f["name"]).write_bytes(base64.b64decode(f["data"]))
        p = subprocess.run([sys.executable, "-c", body["code"]], cwd=work,
                           capture_output=True, text=True, timeout=60)
        if not body["session"]:
            shutil.rmtree(work, ignore_errors=True)
        return httpx2.Response(200, json={"exit_code": p.returncode, "output": p.stdout + p.stderr,
                                          "timed_out": False, "rejected": [], "files": []})

    @property
    def transport(self):
        return httpx2.MockTransport(self.handler)

    def wipe(self):
        """Un bac neuf : l'exécuteur a purgé ou redémarré."""
        shutil.rmtree(self.root, ignore_errors=True)


MORE = {**FILES, "min.js": b"x" * 100_000 + b"CIBLE" + b"x" * 100_000,
        "f.py": b"un\x0cdeux\nici\n", "slow.txt": b"x" * 5000, "big.bin": b"\0" * 10}


@pytest.fixture
def repo(tmp_path):
    """Une source GitLab du compte « a », sa forge, le faux exécuteur, et
    `run(args)` : l'outil appelé."""
    repos.create("a", {"name": "ACME", "forge": "gitlab", "url": "https://g.lan", "token": "glpat",
                       "projects": ["outils/*"]})
    env = type("R", (), {})()
    env.forge_seen, env.bac = [], Bac(tmp_path / "bac")
    env.archive = targz(files=MORE)
    t = forge(env.forge_seen, {
        "/api/v4/projects/outils%2Fbackend/repository/archive.tar.gz": (200, env.archive, {})})

    async def call(args, client="a", forge=t):
        got = await repository.TOOL.run({"project": "outils/backend", **args}, Call(client=client),
                                        transport=forge, sandbox=env.bac.transport)
        return got.text
    env.call = call
    env.run = lambda *a, **k: go(call(*a, **k))
    return env


def test_le_depot_est_lu_dans_le_bac(repo):
    out = repo.run({"command": "grep", "pattern": "mcp=="})
    assert out == "[ACME:outils/backend]\nrequirements.txt:2: mcp==1.2.0"
    first, second = repo.bac.calls
    assert "inputs" not in first and second["inputs"][0]["name"].endswith(".tar.gz")
    assert (first["session"], first["language"], first["client"]) == ("repository", "python", "a")
    assert "glpat" not in json.dumps(repo.bac.calls)         # le jeton n'entre pas dans le bac
    # Déjà dans le bac : une seule exécution, sans archive.
    repo.run({"command": "read", "path": "src/app.py"})
    assert len(repo.bac.calls) == 3 and "inputs" not in repo.bac.calls[-1]
    # Un bac neuf : l'archive repart du cache, sans nouveau téléchargement.
    repo.bac.wipe()
    assert "src/app.py" in repo.run({"command": "tree"})
    assert len(repo.forge_seen) == 1 and "inputs" in repo.bac.calls[-1]
    assert repos.list_own("a")[0]["last_used"]


def test_tree_read_grep(repo):
    t = repo.run({"command": "tree", "depth": 1})
    assert "src/  (2 files)" in t and "requirements.txt  (" in t
    r = repo.run({"command": "read", "path": "requirements.txt", "offset": 2, "limit": 1})
    assert "    2\tmcp==1.2.0" in r and "fastapi" not in r and "(2 lines)" in r
    assert "continue" not in r
    long = repo.run({"command": "read", "path": "min.js"})
    assert "characters]" in long
    assert "CIBLE" in repo.run({"command": "grep", "pattern": "CIBLE", "include": "*.js"})
    assert repo.run({"command": "grep", "pattern": "ici"}).endswith("f.py:2: ici")
    assert "    2\tici" in repo.run({"command": "read", "path": "f.py"})
    assert repo.run({"command": "grep", "pattern": "^$", "include": "f.py"}).endswith("No match.")
    for args, msg in (({"command": "read", "path": "requirements.txt", "offset": 9}, "past"),
                      ({"command": "read", "path": "app.py"}, "did you mean: src/app.py"),
                      ({"command": "read", "path": "src/logo.png"}, "binary"),
                      ({"command": "grep", "pattern": "("}, "invalid regular expression")):
        with pytest.raises(ToolError, match=msg):
            repo.run(args)


def test_grep_piege_borne_dans_le_bac(repo):
    """Un motif à retour arrière catastrophique : le fichier est dit trop
    lent et sauté, les autres sont cherchés — dans le bac, pas le proxy."""
    out = repo.run({"command": "grep", "pattern": r"(x+x+)+y", "include": "*.txt"})
    assert "1 files where the pattern was too slow" in out


def test_archive_piegee_et_bornes(repo, monkeypatch, tmp_path):
    """Liens et chemins en .. ne sont pas écrits ; trop d'entrées : refusé."""
    link = tarfile.TarInfo("proj/lien")
    link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
    up = tarfile.TarInfo("proj/../../evil")
    up.size = 1
    data = targz(extra=(link, up))
    t = forge([], {"/api/v4/projects/outils%2Fbackend/repository/archive.tar.gz": (200, data, {})})
    out = repo.run({"command": "tree"}, forge=t)
    assert "lien" not in out and "evil" not in out
    assert not list(tmp_path.rglob("evil"))
    monkeypatch.setattr(repository, "MAX_ENTRIES", 3)
    repo.bac.wipe()
    with pytest.raises(ToolError, match="entries"):
        repo.run({"command": "tree"}, forge=t)


def test_choix_de_la_source(repo):
    with pytest.raises(ToolError, match="no repository source"):
        repo.run({"command": "tree"}, client="b")
    for args, msg in (({"project": "autre/x"}, "does not allow"),
                      ({"source": "nope"}, "available"),
                      ({"project": "../etc"}, "project"),
                      ({"ref": "--upload-pack=x"}, "ref")):
        with pytest.raises(ToolError, match=msg):
            repo.run({"command": "tree", **args})


def test_un_seul_telechargement_pour_deux_commandes(repo):
    """Deux commandes simultanées sur le même projet attendent le même
    téléchargement."""
    async def both():
        return await asyncio.gather(repo.call({"command": "tree"}),
                                    repo.call({"command": "read", "path": "README.md"}))
    tree, read = go(both())
    assert "README.md" in tree and "# Backend" in read
    assert len(repo.forge_seen) == 1


def test_erreur_gardee_sur_la_source(repo):
    with pytest.raises(ToolError):
        repo.run({"command": "tree", "project": "outils/absent"})
    assert "not found" in repos.list_own("a")[0]["last_error"]


def test_oubli_vide_le_cache_et_detruit_le_bac(repo):
    """Le bac des dépôts demande l'inactivité du cache ; forget vide le
    cache du compte et détruit son bac : la commande suivante retélécharge."""
    repo.run({"command": "tree"})
    assert all(c["idle"] == repository.CACHE.ttl for c in repo.bac.calls)
    assert go(repository.forget("a", repo.bac.transport)) is True
    assert repo.bac.destroyed == [{"client": "a", "session": "repository"}]
    assert len(repository.CACHE) == 0
    repo.run({"command": "tree"})
    assert len(repo.forge_seen) == 2 and "inputs" in repo.bac.calls[-1]


def test_oubli_pendant_un_telechargement(repo, monkeypatch):
    """Un forget pendant qu'une commande télécharge : l'archive ne retourne
    ni dans le cache, ni dans le bac."""
    real = repository.fetch

    async def fetch(*a, **k):
        await repository.forget("a", repo.bac.transport)
        return await real(*a, **k)
    monkeypatch.setattr(repository, "fetch", fetch)
    with pytest.raises(ToolError, match="erased"):
        repo.run({"command": "tree"})
    assert len(repository.CACHE) == 0
    assert all("inputs" not in c for c in repo.bac.calls)


def test_oubli_dit_si_l_executeur_ne_repond_pas():
    down = httpx2.MockTransport(lambda request: httpx2.Response(503))
    assert go(repository.forget("a", down)) is False


def test_incognito_un_bac_d_un_seul_appel(repo):
    """Incognito : chaque commande dépose l'archive dans un bac d'un seul
    appel (session «»), et le cache ne garde rien."""
    token = INCOGNITO.set(True)
    try:
        assert "mcp==1.2.0" in repo.run({"command": "grep", "pattern": "mcp=="})
        repo.run({"command": "tree"})
    finally:
        INCOGNITO.reset(token)
    assert [c["session"] for c in repo.bac.calls] == ["", ""]
    assert all(c.get("inputs") for c in repo.bac.calls)
    assert len(repository.CACHE) == 0 and len(repo.forge_seen) == 2
    assert not list((repo.bac.root).glob("once-*"))           # rien ne reste


def test_cache_glissant_purge_et_oubli(monkeypatch):
    from llm_proxy.tools.webcache import Cache
    import time as t
    now = [1000.0]
    monkeypatch.setattr(t, "monotonic", lambda: now[0])
    c = Cache(60, 10, 10_000, sliding=True)
    c.put(("a", 1), b"x", 1)
    c.put(("b", 1), b"y", 1)
    now[0] += 50
    assert c.get(("a", 1)) == b"x"               # relu : sa durée repart
    now[0] += 30
    assert c.purge() == 1 and c.get(("b", 1)) is None and c.get(("a", 1)) == b"x"
    assert c.forget(lambda k: k[0] != "a") == 1 and len(c) == 0


# ── la page ─────────────────────────────────────────────────────────────

def test_routes_de_la_page(proxy, monkeypatch):
    monkeypatch.setattr(auth, "PROXY_API_KEYS", ["sk-a", "sk-b"])
    c = proxy.client
    c.post("/ui/login", json={"key": "sk-a"})
    r = c.post("/ui/repos", json={"name": "ACME", "forge": "gitlab",
                                  "url": "https://gitlab.exemple.fr", "token": "glpat-zzz"})
    assert r.status_code == 200, r.text
    sid = r.json()["source"]["id"]
    j = c.get("/ui/repos").json()
    assert [s["id"] for s in j["sources"]] == [sid] and "glpat" not in json.dumps(j)
    assert c.get("/ui/session").json()["repos"] is True
    c.cookies.clear()
    c.post("/ui/login", json={"key": "sk-b"})
    assert c.get("/ui/repos").json()["sources"] == []
    assert c.delete(f"/ui/repos/{sid}").json()["deleted"] is False
    monkeypatch.setattr(repository, "ENABLED", False)
    assert c.get("/ui/repos").status_code == 404


def test_modifier_supprimer_oublier_efface(proxy, monkeypatch):
    """Modifier ou supprimer une source, ou « Oublier », efface ce que le
    compte a téléchargé."""
    monkeypatch.setattr(auth, "PROXY_API_KEYS", ["sk-a"])
    owner = auth.toml_digests().pop()
    forgotten = []

    async def forget(o, transport=None):
        forgotten.append(o)
        return True
    monkeypatch.setattr(repository, "forget", forget)
    c = proxy.client
    c.post("/ui/login", json={"key": "sk-a"})
    sid = c.post("/ui/repos", json={"name": "S", "forge": "gitlab", "url": "https://g.lan",
                                    "token": "t"}).json()["source"]["id"]
    assert forgotten == []                       # créer n'efface rien
    c.put(f"/ui/repos/{sid}", json={"name": "S2", "forge": "gitlab", "url": "https://g.lan"})
    c.delete(f"/ui/repos/{sid}")
    assert c.post("/ui/repos/forget", json={}).json() == {"forgotten": True}
    assert forgotten == [owner, owner, owner]
