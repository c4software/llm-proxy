"""Les dépôts (storage/repos.py, tools/forge.py, tools/repository.py,
repository_bac.py, tools/pull_request.py, ui/repo_routes.py) : les sources
par compte et partagées, l'archive de chaque forge (par net.download), le
fetch git, les commandes tree, read et grep lues dans le bac, le cache, les
pull requests de chaque forge. Sans réseau : DNS et forge sont faux
(fakes.fake_dns, un MockTransport) ; le fetch git part d'un dépôt local ; le
faux exécuteur exécute vraiment le script du bac, dans un dossier."""

import asyncio
import base64
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import types

import httpx2
import pytest

from fakes import PUBLIC, fake_dns, go, net_guard

from llm_proxy.access import auth
from llm_proxy.core.settings import FORGE_SCOPE, INCOGNITO
from llm_proxy.storage import conversations, repos
from llm_proxy.core import window
from llm_proxy.tools import code_execution, forge as forges, pull_request, repository
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
    monkeypatch.setattr(pull_request, "ENABLED", True)
    pull_request.HOUR.clear()


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
    """Une forge : `routes` {(méthode, chemin brut), (hôte, chemin brut
    ?requête) ou chemin brut : (statut, corps, en-têtes)}, ou une liste de
    réponses données tour à tour (la dernière reste)."""
    def handler(request):
        seen.append(request)
        host, raw = request.headers["host"], request.url.raw_path.decode()
        for key in ((request.method, raw), (host, raw), raw, raw.split("?")[0]):
            if key in routes:
                got = routes[key]
                if isinstance(got, list):
                    got = got.pop(0) if len(got) > 1 else got[0]
                status, body, headers = got
                return httpx2.Response(status, content=body, headers=headers)
        return httpx2.Response(404)
    return httpx2.MockTransport(handler)


def doc(value, status=200):
    return (status, json.dumps(value).encode(), {})


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


def test_ecriture_reservee_aux_sources_du_compte(monkeypatch):
    """`write` et `blocked_paths` sont gardés (le jeton jamais rendu) ;
    seule une source du compte cochée permet les pull requests : ni la
    forge git, ni une source partagée, ni la source d'un autre compte."""
    s = repos.create("a", {**GITLAB, "token": "glpat-secret", "write": True,
                           "blocked_paths": "docs/*, *.lock"})
    assert (s["write"], s["blocked_paths"]) == (True, ["docs/*", "*.lock"])
    assert "glpat" not in json.dumps(repos.list_own("a"))
    assert repos.writable(repos.available("a")[0])
    with pytest.raises(repos.RepoError, match="forge à API"):
        repos.create("a", {"name": "G", "forge": "git", "url": "https://g.lan", "write": True})
    with pytest.raises(SystemExit, match="lecture"):
        repos._shared_from({"org": {"forge": "gitlab", "url": "https://g.lan", "token": "t",
                                    "write": True}})
    monkeypatch.setattr(repos, "SHARED", repos._shared_from({
        "org": {"forge": "gitlab", "url": "https://g.lan", "token": "t"}}))
    for owner in ("a", "b"):
        assert not repos.writable(next(x for x in repos.available(owner) if x["shared"])), owner
    with pytest.raises(ToolError) as exc:
        go(forges.source("b", "", "equipe/api", write=True))
    assert exc.value.code == "unavailable"
    repos.update("a", s["id"], {**GITLAB, "write": False})
    assert not repos.writable(repos.available("a")[0])


def test_base_d_avant_l_ecriture(tmp_path, monkeypatch):
    """Une table repo_sources d'avant `write` et `blocked_paths` les reçoit :
    ses sources restent en lecture."""
    path = str(tmp_path / "vieille.db")
    with sqlite3.connect(path) as c:
        c.execute("""CREATE TABLE repo_sources (
            owner TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL,
            forge TEXT NOT NULL, url TEXT NOT NULL, username TEXT NOT NULL,
            token TEXT NOT NULL, projects TEXT NOT NULL, created REAL NOT NULL,
            last_used REAL, last_error TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (owner, id))""")
        c.execute("INSERT INTO repo_sources (owner, id, name, forge, url, username, token, "
                  "projects, created) VALUES ('a', 's1', 'S', 'gitlab', 'https://g.lan', '', "
                  "'t', '[]', 1)")
    monkeypatch.setattr(conversations, "DB_PATH", path)
    (s,) = repos.list_own("a")
    assert (s["write"], s["blocked_paths"]) == (False, [])
    assert not repos.writable(repos.available("a")[0])


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


def test_gitea_branche_par_defaut_suit_un_projet_renomme():
    """Sans ref, la branche par défaut d'un projet Gitea renommé (301 sur la
    même forge) se lit encore, le jeton gardé sur la forge."""
    seen = []
    data = targz()
    t = forge(seen, {
        "/api/v1/repos/o/r": (301, b"", {"location": "https://gitea.lan/api/v1/repos/o/r2"}),
        "/api/v1/repos/o/r2": doc({"default_branch": "dev"}),
        "/api/v1/repos/o/r/archive/dev.tar.gz": (200, data, {})})
    gt = {"forge": "gitea", "url": "https://gitea.lan", "token": "tok"}
    assert go(repository.fetch(gt, "o/r", "", t)) == data
    assert [r.headers["authorization"] for r in seen] == ["token tok"] * 3


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


# ── les pull requests ───────────────────────────────────────────────────

CHANGE = {"project": "equipe/api", "title": "Corrige le build",
          "description": "Le test cassait.",
          "files": [{"path": "README.md", "content": "# API\n"},
                    {"path": "src/new.py", "content": "print('é')\n"},
                    {"path": "old.txt", "delete": True}]}


def writer(forge="gitlab", url="https://gitlab.exemple.fr", **more) -> dict:
    return repos.create("a", {"name": forge, "forge": forge, "url": url, "token": "tok",
                              "write": True, **more})


def propose(t, client="a", **args):
    got = go(pull_request.TOOL.run({**CHANGE, **args}, Call(client=client), transport=t))
    return got


def bodies(seen, method, path) -> list:
    return [json.loads(r.content) for r in seen
            if r.method == method and r.url.raw_path.decode().split("?")[0].endswith(path)]


def gitlab_routes() -> dict:
    root = "/api/v4/projects/equipe%2Fapi"
    return {
        ("GET", root): doc({"default_branch": "main"}),
        ("HEAD", root + "/repository/files/README.md?ref=main"): (200, b"", {}),
        ("HEAD", root + "/repository/files/old.txt?ref=main"): (200, b"", {}),
        ("POST", root + "/repository/commits"): doc({"id": "c2"}, 201),
        ("POST", root + "/merge_requests"):
            doc({"web_url": "https://gitlab.exemple.fr/equipe/api/-/merge_requests/3"}, 201)}


def test_une_pr_gitlab_est_un_commit_sur_une_branche_neuve_et_un_brouillon():
    """Un commit (create, update, delete selon ce que la base a) sur une
    branche neuve du préfixe, puis une merge request en brouillon, le pied
    du proxy ajouté ; le jeton à chaque requête, vers la forge seule, et
    jamais dans ce qui est rendu."""
    writer()
    seen = []
    got = propose(forge(seen, gitlab_routes()))
    assert got.meta["url"] == "https://gitlab.exemple.fr/equipe/api/-/merge_requests/3"
    (commit,) = bodies(seen, "POST", "/repository/commits")
    assert commit["start_branch"] == "main" and commit["branch"].startswith("llm-proxy/corrige-le-build-")
    assert commit["actions"] == [
        {"action": "update", "file_path": "README.md", "content": "# API\n"},
        {"action": "create", "file_path": "src/new.py", "content": "print('é')\n"},
        {"action": "delete", "file_path": "old.txt"}]
    (mr,) = bodies(seen, "POST", "/merge_requests")
    assert (mr["source_branch"], mr["target_branch"]) == (commit["branch"], "main")
    assert mr["title"] == "Draft: Corrige le build"
    assert mr["description"] == "Le test cassait." + pull_request.FOOTER
    assert {r.headers["host"] for r in seen} == {"gitlab.exemple.fr"}
    assert all(r.headers["private-token"] == "tok" for r in seen)
    assert repos.list_own("a")[0]["last_error"] == ""
    shown = got.text + json.dumps(dict(got.meta)) + json.dumps(
        pull_request.TOOL.summary(CHANGE, got)) + json.dumps(repos.list_own("a"))
    assert "tok" not in shown


def test_une_pr_github_garde_la_base_et_se_passe_du_brouillon_si_l_offre_l_interdit():
    """L'arbre (contenus en ligne, sha null pour une suppression) sur le
    commit de la base, la ref, puis la PR en brouillon ; une offre qui
    refuse les brouillons : une PR ordinaire, dite brouillon par son titre."""
    writer("github", "https://github.com")
    seen = []
    t = forge(seen, {
        ("GET", "/repos/equipe/api"): doc({"default_branch": "main"}),
        ("GET", "/repos/equipe/api/contents/README.md?ref=main"): doc({"type": "file", "sha": "s1"}),
        ("GET", "/repos/equipe/api/contents/old.txt?ref=main"): doc({"type": "file", "sha": "s3"}),
        ("GET", "/repos/equipe/api/git/ref/heads/main"): doc({"object": {"sha": "c1"}}),
        ("GET", "/repos/equipe/api/git/commits/c1"): doc({"tree": {"sha": "t1"}}),
        ("POST", "/repos/equipe/api/git/trees"): doc({"sha": "t2"}, 201),
        ("POST", "/repos/equipe/api/git/commits"): doc({"sha": "c2"}, 201),
        ("POST", "/repos/equipe/api/git/refs"): doc({}, 201),
        ("POST", "/repos/equipe/api/pulls"): [
            doc({"message": "Validation Failed", "errors": [
                {"message": "Draft pull requests are not supported in this repository."}]}, 422),
            doc({"html_url": "https://github.com/equipe/api/pull/9"}, 201)]})
    assert propose(t).meta["url"] == "https://github.com/equipe/api/pull/9"
    (tree,) = bodies(seen, "POST", "/git/trees")
    assert tree["base_tree"] == "t1"
    assert [(e["path"], e.get("content"), "sha" in e) for e in tree["tree"]] == [
        ("README.md", "# API\n", False), ("src/new.py", "print('é')\n", False),
        ("old.txt", None, True)]
    (commit,) = bodies(seen, "POST", "/git/commits")
    assert (commit["tree"], commit["parents"]) == ("t2", ["c1"])
    (ref,) = bodies(seen, "POST", "/git/refs")
    assert ref["sha"] == "c2" and ref["ref"].startswith("refs/heads/llm-proxy/")
    first, second = bodies(seen, "POST", "/pulls")
    assert first["draft"] is True and first["title"] == "Corrige le build"
    assert "draft" not in second and second["title"] == "[Draft] Corrige le build"
    assert {r.headers["host"] for r in seen} == {"api.github.com"}
    assert all(r.headers["authorization"] == "Bearer tok" for r in seen)


def test_une_pr_gitea_ecrit_tous_les_fichiers_en_un_commit():
    """Le sha de chaque fichier existant, un seul POST contents vers une
    branche neuve, puis la PR « WIP: »."""
    writer("gitea", "https://gitea.lan")
    seen = []
    root = "/api/v1/repos/equipe/api"
    t = forge(seen, {
        ("GET", root): doc({"default_branch": "main"}),
        ("GET", root + "/contents/README.md?ref=main"): doc({"type": "file", "sha": "s1"}),
        ("GET", root + "/contents/old.txt?ref=main"): doc({"type": "file", "sha": "s3"}),
        ("POST", root + "/contents"): doc({}, 201),
        ("POST", root + "/pulls"): doc({"html_url": "https://gitea.lan/equipe/api/pulls/2"}, 201)})
    assert propose(t).meta["url"] == "https://gitea.lan/equipe/api/pulls/2"
    (contents,) = bodies(seen, "POST", "/contents")
    assert contents["branch"] == "main" and contents["new_branch"].startswith("llm-proxy/")
    assert [(f["operation"], f["path"], f.get("sha")) for f in contents["files"]] == [
        ("update", "README.md", "s1"), ("create", "src/new.py", None), ("delete", "old.txt", "s3")]
    assert base64.b64decode(contents["files"][1]["content"]).decode() == "print('é')\n"
    (pr,) = bodies(seen, "POST", "/pulls")
    assert pr["title"] == "WIP: Corrige le build" and pr["head"] == contents["new_branch"]
    assert all(r.headers["authorization"] == "token tok" for r in seen)


def test_une_pr_refusee_ne_touche_pas_la_forge(monkeypatch):
    """Chemins de la CI ou protégés par la source, chemins hors du projet,
    trop de fichiers ou d'octets, source sans écriture, partagée ou d'un
    autre compte : refusé, et rien ne part vers la forge."""
    writer(blocked_paths=["docs/*"])
    repos.create("a", {"name": "lecture", "forge": "gitlab", "url": "https://gitlab.exemple.fr",
                       "token": "t"})
    monkeypatch.setattr(repos, "SHARED", repos._shared_from({
        "org": {"forge": "gitlab", "url": "https://gitlab.exemple.fr", "token": "t"}}))
    monkeypatch.setattr(pull_request, "MAX_FILES", 3)
    monkeypatch.setattr(pull_request, "MAX_BYTES", 100)
    one = lambda path, content="x": [{"path": path, "content": content}]
    seen = []
    t = forge(seen, gitlab_routes())
    for args, code in (
            ({"files": one(".gitlab-ci.yml")}, "not_allowed"),
            ({"files": one(".GitHub/Workflows/ci.yml")}, "not_allowed"),
            ({"files": one("docs/index.md")}, "not_allowed"),
            ({"files": one("../etc/passwd")}, "invalid_input"),
            ({"files": one("src/.git/config")}, "invalid_input"),
            ({"files": one("a") + one("A")}, "invalid_input"),
            ({"files": [{"path": f"f{i}"} for i in range(4)]}, "invalid_input"),
            ({"files": one("a", "é" * 60)}, "invalid_input"),
            ({"files": one("a"), "source": "lecture"}, "invalid_input"),
            ({"files": one("a"), "source": "org"}, "invalid_input"),
            ({"files": one("a"), "title": ""}, "invalid_input")):
        with pytest.raises(ToolError) as exc:
            propose(t, **args)
        assert exc.value.code == code, args
    with pytest.raises(ToolError) as exc:
        propose(t, client="b", files=one("a"))
    assert exc.value.code == "unavailable"
    assert seen == []


def test_une_pr_github_ne_remplace_qu_un_fichier_existant_ou_absent():
    """Un lien ou un sous-module n'est pas écrasé par un fichier, et un
    fichier absent ne se supprime pas : refusé avant toute écriture."""
    writer("github", "https://github.com")
    seen = []
    t = forge(seen, {
        ("GET", "/repos/equipe/api"): doc({"default_branch": "main"}),
        ("GET", "/repos/equipe/api/contents/lien?ref=main"): doc({"type": "symlink", "sha": "x"})})
    for files in ([{"path": "lien", "content": "x"}], [{"path": "absent", "delete": True}]):
        with pytest.raises(ToolError) as exc:
            propose(t, files=files)
        assert exc.value.code == "invalid_input", files
    assert [r.method for r in seen if r.method != "GET"] == []


def test_echec_apres_la_branche_la_supprime():
    """La merge request refusée (jeton sans ce droit) : la branche créée
    est supprimée, et la même modification redemandée passe ensuite."""
    writer()
    seen = []
    routes = gitlab_routes()
    routes[("POST", "/api/v4/projects/equipe%2Fapi/merge_requests")] = [
        doc({"message": "403 Forbidden"}, 403), routes[("POST", "/api/v4/projects/equipe%2Fapi"
                                                         "/merge_requests")]]
    t = forge(seen, routes)
    with pytest.raises(ToolError) as exc:
        propose(t)
    assert exc.value.code == "not_allowed"
    (commit,) = bodies(seen, "POST", "/repository/commits")
    (deleted,) = [r for r in seen if r.method == "DELETE"]
    assert deleted.url.raw_path.decode().endswith(
        "/repository/branches/" + commit["branch"].replace("/", "%2F"))
    assert propose(t).meta["url"].endswith("/merge_requests/3")


def test_un_evenement_de_forge_ne_fait_ouvrir_que_son_projet():
    """Dans le tour d'un événement de forge (FORGE_SCOPE), lire ou proposer
    ne vise que le projet de l'événement, sur sa forge : une issue publique
    ne fait pas recopier un projet privé ailleurs. Sans projet connu, rien."""
    writer()
    repos.create("a", {"name": "autre", "forge": "github", "url": "https://github.com",
                       "token": "t", "write": True})
    for scope, project, source, allowed in (
            (("gitlab.exemple.fr", "equipe/api"), "equipe/api", "gitlab", True),
            (("GitLab.exemple.fr", "Equipe/API"), "equipe/api", "gitlab", True),
            (("gitlab.exemple.fr", "equipe/api"), "equipe/prive", "gitlab", False),
            (("github.com", "equipe/api"), "equipe/api", "gitlab", False),
            (("", ""), "equipe/api", "gitlab", False)):
        token = FORGE_SCOPE.set(scope)
        try:
            for write in (False, True):
                case = (scope, project, write)
                if allowed:
                    assert go(forges.source("a", source, project, write))["name"] == source, case
                else:
                    with pytest.raises(ToolError) as exc:
                        go(forges.source("a", source, project, write))
                    assert exc.value.code == "not_allowed", case
        finally:
            FORGE_SCOPE.reset(token)
    assert go(forges.source("a", "gitlab", "equipe/prive", True))["name"] == "gitlab"


def test_la_meme_modification_retombe_sur_sa_branche():
    """La même modification (même titre, mêmes fichiers) donne la même branche :
    déjà là, elle est refusée sans rien écrire ; une autre en donne une
    autre."""
    writer()
    seen = []
    propose(forge(seen, gitlab_routes()))
    (commit,) = bodies(seen, "POST", "/repository/commits")
    branch = commit["branch"]
    routes = gitlab_routes()
    routes[("GET", "/api/v4/projects/equipe%2Fapi/repository/branches/"
            + branch.replace("/", "%2F"))] = doc({"name": branch})
    seen.clear()
    with pytest.raises(ToolError, match="already exists"):
        propose(forge(seen, routes))
    assert [r.method for r in seen if r.method == "POST"] == []
    propose(forge(seen, routes), files=[{"path": "README.md", "content": "autre\n"}])
    (commit,) = bodies(seen, "POST", "/repository/commits")
    assert commit["branch"] != branch


def test_les_pr_d_un_compte_sont_bornees_par_heure(monkeypatch):
    """[repos].pr_per_hour par compte, sur une heure glissante (horloge
    décalée) ; un autre compte a sa propre borne."""
    writer()
    repos.create("b", {"name": "B", "forge": "gitlab", "url": "https://gitlab.exemple.fr",
                       "token": "tok", "write": True})
    monkeypatch.setattr(pull_request, "PER_HOUR", 1)
    now = [1000.0]
    monkeypatch.setattr(window, "time", types.SimpleNamespace(monotonic=lambda: now[0]))
    propose(forge([], gitlab_routes()))
    with pytest.raises(ToolError) as exc:
        propose(forge([], gitlab_routes()))
    assert exc.value.code == "too_many_requests"
    propose(forge([], gitlab_routes()), client="b")
    now[0] += 3601
    propose(forge([], gitlab_routes()))


def test_une_ecriture_n_est_jamais_redirigee():
    """Une requête d'écriture n'est pas redirigée, et une lecture redirigée
    ne porte pas le jeton hors de la source : l'erreur est notée sur la
    source, sans le jeton."""
    writer()
    seen = []
    routes = gitlab_routes()
    del routes[("GET", "/api/v4/projects/equipe%2Fapi")]
    routes[("gitlab.exemple.fr", "/api/v4/projects/equipe%2Fapi")] = (
        301, b"", {"location": "https://evil.example/api/v4/projects/equipe%2Fapi"})
    routes[("evil.example", "/api/v4/projects/equipe%2Fapi")] = doc({"default_branch": "main"})
    routes[("POST", "/api/v4/projects/equipe%2Fapi/repository/commits")] = (
        307, b"", {"location": "https://evil.example/steal"})
    with pytest.raises(ToolError) as exc:
        propose(forge(seen, routes))
    assert exc.value.code == "not_accessible" and "tok" not in exc.value.message
    assert [r.headers.get("private-token") for r in seen if r.headers["host"] == "evil.example"] \
        == [None]
    assert not any(r.url.path == "/steal" for r in seen)
    error = repos.list_own("a")[0]["last_error"]
    assert "redirected" in error and "tok" not in error


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
    assert j["pull_requests"] is True and ".gitlab-ci.yml" in j["blocked_paths"]
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
