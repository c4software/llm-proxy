"""Les dépôts (storage/repos.py, tools/repository.py, ui/repo_routes.py) : les
sources par compte et partagées, l'archive de chaque forge (par
net.download), le fetch git, les commandes tree, read et grep, le cache.
Sans réseau : DNS et forge sont faux (fakes.fake_dns, un MockTransport) ;
le fetch git part d'un dépôt local."""

import asyncio
import io
import json
import os
import subprocess
import tarfile

import httpx2
import pytest

from fakes import PUBLIC, fake_dns, go, net_guard

from llm_proxy.access import auth
from llm_proxy.storage import conversations, repos
from llm_proxy.tools import repository
from llm_proxy.tools import pattern as patterns
from llm_proxy.tools.contract import Call, ToolError

HOSTS = ("g.lan", "evil.example", "gitlab.pmw.lan", "api.github.com", "codeload.github.com",
         "gitea.lan")


@pytest.fixture(autouse=True)
def base(monkeypatch, tmp_path):
    monkeypatch.setattr(conversations, "DB_PATH", str(tmp_path / "conversations.db"))
    monkeypatch.setattr(repository, "ENABLED", True)
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

def test_sources_par_compte_et_partagees(monkeypatch):
    s = repos.create("a", {"name": "PMW", "forge": "gitlab", "url": "https://gitlab.pmw.lan/",
                           "token": "glpat-secret", "projects": "playmoweb/*, kid-ai/*"})
    assert s["url"] == "https://gitlab.pmw.lan" and "token" not in s
    assert s["projects"] == ["playmoweb/*", "kid-ai/*"]
    assert "glpat" not in json.dumps(repos.list_own("a")) and repos.list_own("b") == []
    repos.update("a", s["id"], {"name": "PMW", "forge": "gitlab", "url": "https://gitlab.pmw.lan"})
    assert repos.available("a")[0]["token"] == "glpat-secret"     # jeton gardé
    # Le jeton ne suit pas, sans être ressaisi, une source qui change d'hôte.
    with pytest.raises(repos.RepoError, match="change d'adresse"):
        repos.update("a", s["id"], {"name": "PMW", "forge": "gitlab", "url": "https://autre.example"})
    with pytest.raises(repos.RepoError, match="jeton"):
        repos.create("a", {"name": "X", "forge": "gitlab", "url": "https://g.example"})
    with pytest.raises(repos.RepoError, match="déjà"):            # sans casse
        repos.create("a", {"name": "pmw", "forge": "gitlab", "url": "https://g.example", "token": "t"})
    with pytest.raises(repos.RepoError, match="identifiants"):
        repos.create("a", {"name": "U", "forge": "git", "url": "https://u:jeton@g.example"})
    with pytest.raises(repos.RepoError, match="source invalide"):
        repos.delete("a", "../x")
    gh = repos.create("a", {"name": "GH", "forge": "github", "token": "ghp"})
    assert gh["url"] == "https://github.com"
    monkeypatch.setattr(repos, "SHARED", repos._shared_from({
        "pmw": {"forge": "gitlab", "url": "https://gitlab.org.lan", "token": "org"},
        "off": {"forge": "github", "token": "x", "enabled": False}}))
    assert [x["name"] for x in repos.available("b")] == ["pmw"]
    assert [x["name"] for x in repos.available("a")] == ["PMW", "GH"]   # « pmw » masquée
    assert "token" not in repos.shared()[0]
    assert repos.permits(s | {"projects": ["playmoweb/*"]}, "PlayMoWeb/api")
    assert not repos.permits(s | {"projects": ["playmoweb/*"]}, "kid-ai/backend")


# ── les forges ──────────────────────────────────────────────────────────

def test_archive_de_chaque_forge():
    seen = []
    data = targz()
    t = forge(seen, {
        "/api/v4/projects/kid-ai%2Fbackend/repository/archive.tar.gz?sha=main": (200, data, {}),
        "/repos/o/r/tarball": (302, b"", {"location": "https://codeload.github.com/o/r/tar.gz/x"}),
        "/o/r/tar.gz/x": (200, data, {}),
        "/api/v1/repos/o/r": (200, json.dumps({"default_branch": "dev"}).encode(), {}),
        "/api/v1/repos/o/r/archive/dev.tar.gz": (200, data, {}),
    })
    gl = {"forge": "gitlab", "url": "https://gitlab.pmw.lan", "token": "glpat"}
    files = go(repository.fetch(gl, "kid-ai/backend", "main", t))
    assert files["requirements.txt"] == FILES["requirements.txt"]
    assert seen[-1].headers["private-token"] == "glpat"
    assert seen[-1].headers["accept-encoding"] == "identity"     # borne comptée telle quelle
    gh = {"forge": "github", "url": "https://github.com", "token": "ghp"}
    assert set(go(repository.fetch(gh, "o/r", "", t))) == set(FILES)
    assert seen[-2].headers["host"] == "api.github.com"
    assert seen[-2].headers["authorization"] == "Bearer ghp"
    assert "authorization" not in seen[-1].headers           # pas chez codeload
    gt = {"forge": "gitea", "url": "https://gitea.lan", "token": "tok"}
    assert set(go(repository.fetch(gt, "o/r", "", t))) == set(FILES)
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


def test_archive_refusee_trop_grosse_ou_piegee(monkeypatch):
    gl = {"forge": "gitlab", "url": "https://g.lan", "token": "x"}
    for status, code in ((401, "not_allowed"), (404, "not_accessible"),
                         (429, "too_many_requests")):
        t = forge([], {"/api/v4/projects/a%2Fb/repository/archive.tar.gz": (status, b"", {})})
        with pytest.raises(ToolError) as exc:
            go(repository.fetch(gl, "a/b", "", t))
        assert exc.value.code == code
    link = tarfile.TarInfo("proj/lien")
    link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
    up = tarfile.TarInfo("proj/../../evil")
    up.size = 1
    assert set(repository._unpack(targz(extra=(link, up)))) == set(FILES)
    monkeypatch.setattr(repository, "MAX_ENTRIES", 3)
    with pytest.raises(ToolError, match="entries"):
        repository._unpack(targz())
    monkeypatch.setattr(repository, "MAX_BYTES", 10)
    t = forge([], {"/api/v4/projects/a%2Fb/repository/archive.tar.gz": (200, b"x" * 100, {})})
    with pytest.raises(ToolError, match="larger"):
        go(repository.fetch(gl, "a/b", "", t))


def test_gitea_branche_par_defaut_invalide():
    t = forge([], {"/api/v1/repos/o/r": (200, json.dumps({"default_branch": "../../admin"}).encode(), {})})
    with pytest.raises(ToolError, match="default branch"):
        go(repository.fetch({"forge": "gitea", "url": "https://gitea.lan", "token": "t"}, "o/r", "", t))


def test_fetch_git(tmp_path):
    """Forge « git » : `git fetch --depth 1` puis `git archive`, une branche
    ou un commit."""
    src = tmp_path / "srv" / "grp" / "proj.git"
    src.mkdir(parents=True)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    git = lambda *a: subprocess.run(["git", "-C", str(src), *a], check=True, env=env,
                                    capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    (src / "app.py").write_text("print(1)\n")
    git("add", ".")
    git("commit", "-qm", "un")
    first = git("rev-parse", "HEAD")
    (src / "app.py").write_text("print(2)\n")
    git("commit", "-qam", "deux")
    git("config", "uploadpack.allowAnySHA1InWant", "true")
    source = {"forge": "git", "url": f"file://{tmp_path}/srv", "token": ""}
    assert go(repository.fetch(source, "grp/proj", "")) == {"app.py": b"print(2)\n"}
    assert go(repository.fetch(source, "grp/proj", first)) == {"app.py": b"print(1)\n"}
    with pytest.raises(ToolError, match="git failed"):
        go(repository.fetch(source, "grp/absent", ""))


def test_fetch_git_borne_meme_avec_peu_de_gros_fichiers(tmp_path, monkeypatch):
    """Sous 100 objets, git écrirait des objets libres un par un : forcé en
    pack (fetch.unpackLimit), la borne tient et se dit."""
    src = tmp_path / "srv" / "grp" / "big.git"
    src.mkdir(parents=True)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    for i in range(5):
        (src / f"f{i}.bin").write_bytes(os.urandom(400_000))
    subprocess.run(["git", "-C", str(src), "add", "."], check=True)
    subprocess.run(["git", "-C", str(src), "commit", "-qm", "x"], check=True, env=env)
    monkeypatch.setattr(repository, "MAX_BYTES", 1_000_000)
    with pytest.raises(ToolError, match="larger than 1000000"):
        go(repository.fetch({"forge": "git", "url": f"file://{tmp_path}/srv", "token": ""},
                            "grp/big", ""))


# ── les commandes ───────────────────────────────────────────────────────

def test_tree_read_grep():
    files = {**FILES, "big.bin": None, "min.js": b"x" * 100_000 + b"\nfin\n"}
    t = repository.tree(files, "", 1)
    assert "src/  (2 files)" in t and "requirements.txt  (" in t and "not kept" in t
    assert "src/app.py" in repository.tree(files, "src", 2)
    r = repository.read(files, "requirements.txt", 2, 1)
    assert "    2\tmcp==1.2.0" in r and "fastapi" not in r and "(2 lines)" in r
    assert "continue" not in r                                # la fin du fichier
    long = repository.read(files, "min.js", 1, 400)           # une ligne énorme : coupée, on avance
    assert "characters]" in long and "    2\tfin" in long
    with pytest.raises(ToolError, match="past"):
        repository.read(files, "requirements.txt", 9, 10)
    with pytest.raises(ToolError, match="did you mean: src/app.py"):
        repository.read(files, "app.py", 1, 10)
    with pytest.raises(ToolError, match="binary"):
        repository.read(files, "src/logo.png", 1, 10)
    mcp = patterns.compile("mcp")
    assert repository.grep(files, mcp, "", "") == ("requirements.txt:2: mcp==1.2.0\n"
                                                   "src/app.py:1: import mcp")
    assert repository.grep(files, mcp, "", "*.txt") == "requirements.txt:2: mcp==1.2.0"
    assert repository.grep(files, patterns.compile("nothing"), "src", "").startswith("No match")


def test_grep_piege_borne(monkeypatch):
    """Un motif à retour arrière catastrophique est coupé, pas le proxy : le
    fichier est dit trop lent, les autres sont cherchés ; au-delà du budget
    de toute la recherche, TimeoutError."""
    monkeypatch.setattr(patterns, "TIMEOUT", 0.2)
    files = {"a.txt": b"x" * 5000, "b.txt": b"xxy\n"}
    slow = patterns.compile(r"(x+x+)+y")
    assert repository.grep(files, slow, "", "") == ("b.txt:1: xxy\n"
                                                    "[1 files where the pattern was too slow, not searched]")
    monkeypatch.setattr(repository, "GREP_BUDGET", 0.1)
    with pytest.raises(TimeoutError):
        repository.grep(files, slow, "", "")


def test_grep_une_ligne_par_correspondance_meme_longue():
    """Un fichier sans saut de ligne (minifié) : la recherche reste rapide,
    la ligne est coupée autour de la correspondance, les numéros sont ceux de
    read."""
    files = {"min.js": b"a" * 2_000_000, "f.py": b"un\x0cdeux\nici\n"}
    out = repository.grep(files, patterns.compile("a"), "", "*.js")
    assert out.startswith("min.js:1: aaa") and len(out) < 400
    files["min.js"] = b"x" * 100_000 + b"CIBLE" + b"x" * 100_000
    assert "CIBLE" in repository.grep(files, patterns.compile("CIBLE"), "", "")
    assert repository.grep(files, patterns.compile("ici"), "", "") == "f.py:2: ici"
    assert repository.grep({"g.txt": b"un\n"}, patterns.compile("^$"), "", "").startswith("No match")
    assert "    2\tici" in repository.read(files, "f.py", 1, 10)


def test_outil_de_bout_en_bout_et_cache():
    repos.create("a", {"name": "PMW", "forge": "gitlab", "url": "https://g.lan", "token": "glpat",
                       "projects": ["kid-ai/*"]})
    seen = []
    t = forge(seen, {"/api/v4/projects/kid-ai%2Fbackend/repository/archive.tar.gz": (200, targz(), {})})
    call = Call(client="a")
    run = lambda args, c=call: go(repository.TOOL.run(args, c, transport=t))
    got = run({"command": "grep", "project": "kid-ai/backend", "pattern": "mcp=="})
    assert got.text == "[PMW:kid-ai/backend]\nrequirements.txt:2: mcp==1.2.0"
    got = run({"command": "read", "project": "kid-ai/backend", "path": "src/app.py"})
    assert "import mcp" in got.text and len(seen) == 1          # gardé : pas retéléchargé
    assert repos.list_own("a")[0]["last_used"] and not repos.list_own("a")[0]["last_error"]
    # La source change de jeton : l'ancien contenu n'est pas servi.
    sid = repos.list_own("a")[0]["id"]
    repos.update("a", sid, {"name": "PMW", "forge": "gitlab", "url": "https://g.lan",
                            "token": "glpat-2", "projects": ["kid-ai/*"]})
    run({"command": "tree", "project": "kid-ai/backend"})
    assert len(seen) == 2 and seen[-1].headers["private-token"] == "glpat-2"
    with pytest.raises(ToolError, match="invalid regular expression"):
        run({"command": "grep", "project": "kid-ai/backend", "pattern": "("})
    with pytest.raises(ToolError, match="no repository source"):
        run({"command": "tree", "project": "kid-ai/backend"}, Call(client="b"))
    with pytest.raises(ToolError, match="does not allow"):
        run({"command": "tree", "project": "autre/x"})
    with pytest.raises(ToolError, match="available"):
        run({"command": "tree", "project": "kid-ai/x", "source": "nope"})
    with pytest.raises(ToolError, match="project"):
        run({"command": "tree", "project": "../etc"})
    with pytest.raises(ToolError, match="ref"):
        run({"command": "tree", "project": "kid-ai/x", "ref": "--upload-pack=x"})


def test_un_seul_telechargement_pour_deux_commandes():
    repos.create("a", {"name": "S", "forge": "gitlab", "url": "https://g.lan", "token": "x"})
    seen = []
    t = forge(seen, {"/api/v4/projects/a%2Fb/repository/archive.tar.gz": (200, targz(), {})})

    async def both():
        return await asyncio.gather(*(repository.TOOL.run(
            {"command": c, "project": "a/b"} | ({"pattern": "mcp"} if c == "grep" else {}),
            Call(client="a"), transport=t) for c in ("tree", "grep")))
    go(both())
    assert len(seen) == 1 and not repository._loading


def test_erreur_gardee_sur_la_source():
    repos.create("a", {"name": "S", "forge": "gitlab", "url": "https://g.lan", "token": "x"})
    with pytest.raises(ToolError):
        go(repository.TOOL.run({"command": "tree", "project": "a/b"}, Call(client="a"),
                               transport=forge([], {})))
    assert "not found" in repos.list_own("a")[0]["last_error"]


# ── la page ─────────────────────────────────────────────────────────────

def test_routes_de_la_page(proxy, monkeypatch):
    monkeypatch.setattr(auth, "PROXY_API_KEYS", ["sk-a", "sk-b"])
    c = proxy.client
    c.post("/ui/login", json={"key": "sk-a"})
    r = c.post("/ui/repos", json={"name": "PMW", "forge": "gitlab",
                                  "url": "https://gitlab.pmw.lan", "token": "glpat-zzz"})
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
