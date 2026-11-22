"""L'extension d'exemple `pull_request` (examples/extensions/pull_request.py) :
une PR en brouillon au jeton de service, sur les seuls projets permis,
sans chemin de CI, bornée par compte et pour tout le proxy, attribuée,
sa branche supprimée après un échec ; le jeton jamais rendu. La forge est
une doublure (httpx2.MockTransport derrière le vrai net.post, DNS
remplacé)."""

import asyncio
import importlib.util
import json
import logging
from pathlib import Path

import httpx2
import pytest

from fakes import fake_dns
from llm_proxy import extension
from llm_proxy.core import config

FILE = Path(__file__).resolve().parents[2] / "examples" / "extensions" / "pull_request" / "pull_request.py"
TOKEN = "glpat-SECRET-de-service"
CLIENT = "a1b2c3d4e5f6a7b8c9d0"
HEAD, TREE, NEW = "a" * 40, "b" * 40, "c" * 40
# Les modes des fichiers de la base (GitHub : l'arbre de la base).
MODES = {"README.md": "100644", "run.sh": "100755", "lien": "120000"}


@pytest.fixture
def pr(monkeypatch):
    """Le module de l'extension, neuf (ses bornes à zéro), réglé sur une
    forge `forge.exemple.fr`."""
    spec = importlib.util.spec_from_file_location("llm_proxy_ext.pull_request_test", FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fake_dns(monkeypatch, {"forge.exemple.fr": ["93.184.216.34"]})

    def configure(forge="gitlab", **more):
        monkeypatch.setitem(config.CONFIG, "extensions", {"pull_request": {
            "forge": forge, "url": "https://forge.exemple.fr", "token": TOKEN,
            "projects": ["equipe/*"], **more}})
    configure()
    module.configure = configure
    return module


class Forge:
    """Une forge en mémoire : `existing`, les fichiers de la base ;
    `fail`, (méthode, fin du chemin) → statut. Note chaque requête."""

    def __init__(self, existing=("README.md", "run.sh"), fail=None):
        self.existing, self.fail, self.seen = set(existing), dict(fail or {}), []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.raw_path.decode().split("?")[0]
        body = json.loads(request.content) if request.content else None
        self.seen.append((request.method, path, body, dict(request.headers)))
        for (method, end), status in self.fail.items():
            if request.method == method and path.endswith(end):
                return httpx2.Response(status, json={"message": "refused by the forge"})
        if request.method == "DELETE":
            return httpx2.Response(204)
        if "/branches/" in path or "/git/ref/heads/llm-proxy" in path:
            return httpx2.Response(404, json={"message": "not found"})
        if "/repository/files/" in path:
            name = path.rsplit("/", 1)[1].replace("%2F", "/")
            return httpx2.Response(200 if name in self.existing else 404)
        if "/contents/" in path and request.method == "GET":
            name = path.split("/contents/", 1)[1]
            return (httpx2.Response(200, json={"type": "file", "sha": "s-" + name})
                    if name in self.existing else httpx2.Response(404, json={}))
        if path.endswith("/git/ref/heads/main"):
            return httpx2.Response(200, json={"object": {"sha": HEAD}})
        if path.endswith("/git/commits/" + HEAD):
            return httpx2.Response(200, json={"tree": {"sha": TREE}})
        if path.endswith("/git/trees/" + TREE):
            return httpx2.Response(200, json={"truncated": False, "tree": [
                {"path": p, "mode": m} for p, m in MODES.items()]})
        if request.method == "POST":
            if path.endswith(("/merge_requests", "/pulls")):
                key = "web_url" if path.endswith("/merge_requests") else "html_url"
                return httpx2.Response(201, json={key: "https://forge.exemple.fr/pr/1"})
            return httpx2.Response(201, json={"sha": NEW})
        return httpx2.Response(200, json={"default_branch": "main"})

    def transport(self):
        return httpx2.MockTransport(self.handler)

    def posted(self, end: str):
        return next(body for method, path, body, _ in self.seen
                    if method == "POST" and path.endswith(end))


ARGS = {"project": "equipe/app", "title": "Corrige la doc",
        "description": "Pourquoi.", "files": [
            {"path": "README.md", "content": "# App\n"},
            {"path": "docs/nouveau.md", "content": "Nouveau.\n"}]}


def run(pr, forge: Forge, args=ARGS, client=CLIENT):
    return asyncio.run(pr.TOOL.run(args, extension.Call(client=client),
                                   transport=forge.transport()))


# ── la proposition ──────────────────────────────────────────────────────

def test_une_pr_gitlab_est_ouverte_en_brouillon_et_attribuee(pr, caplog):
    forge = Forge()
    with caplog.at_level(logging.INFO):
        result = run(pr, forge)
    commit = forge.posted("/repository/commits")
    assert commit["start_branch"] == "main" and commit["branch"].startswith("llm-proxy/corrige-la-doc-")
    assert [(a["action"], a["file_path"]) for a in commit["actions"]] == [
        ("update", "README.md"), ("create", "docs/nouveau.md")]
    mr = forge.posted("/merge_requests")
    assert mr["title"] == "Draft: Corrige la doc" and mr["target_branch"] == "main"
    assert f"account `{CLIENT[:12]}`" in mr["description"]
    assert all(h.get("private-token") == TOKEN for *_, h in forge.seen)
    assert result.meta["url"] == "https://forge.exemple.fr/pr/1"
    assert TOKEN not in result.text + json.dumps(dict(result.meta))
    line = next(r.getMessage() for r in caplog.records if "pull request ouverte" in r.getMessage())
    assert "equipe/app" in line and CLIENT[:12] in line and TOKEN not in line


@pytest.mark.parametrize("kind", ["github", "gitea"])
def test_github_et_gitea_ouvrent_aussi_un_brouillon(pr, kind):
    pr.configure(kind)
    forge = Forge()
    result = run(pr, forge)
    pull = forge.posted("/pulls")
    assert pull["base"] == "main" and pull["head"].startswith("llm-proxy/")
    if kind == "github":
        assert pull["draft"] is True
        assert forge.posted("/git/trees")["base_tree"] == TREE
    else:
        assert pull["title"].startswith("WIP: ")
        ops = {f["path"]: f["operation"] for f in forge.posted("/contents")["files"]}
        assert ops == {"README.md": "update", "docs/nouveau.md": "create"}
    assert result.meta["url"] == "https://forge.exemple.fr/pr/1"


def test_github_garde_le_mode_d_un_script_et_refuse_un_lien(pr):
    """Un script réécrit garde son bit x ; un lien de la base n'est pas un
    fichier à réécrire : refusé avant toute écriture."""
    pr.configure("github")
    forge = Forge()
    run(pr, forge, {**ARGS, "files": [{"path": "run.sh", "content": "#!/bin/sh\n"}]})
    assert forge.posted("/git/trees")["tree"][0]["mode"] == "100755"
    forge = Forge()
    with pytest.raises(extension.ToolError) as err:
        run(pr, forge, {**ARGS, "files": [{"path": "lien", "content": "x"}]})
    assert err.value.code == "invalid_input"
    assert not [m for m, *_ in forge.seen if m == "POST"]


def test_refus_avant_tout_appel_a_la_forge(pr):
    """Projet hors de `projects` ou mal formé, chemin de CI, motif de
    `blocked_paths` : refusés sans rien demander à la forge."""
    pr.configure(blocked_paths=["deploy/*"])
    cases = {
        "projet hors liste": ({"project": "autre/app"}, "not_allowed"),
        "projet avec ..": ({"project": "equipe/../autre"}, "invalid_input"),
        "chemin de CI": ({"files": [{"path": ".gitlab-ci.yml", "content": "x"}]}, "not_allowed"),
        "CI, autre casse": ({"files": [{"path": ".GitHub/Workflows/a.yml", "content": "x"}]},
                            "not_allowed"),
        "motif de l'admin": ({"files": [{"path": "deploy/prod.sh", "content": "x"}]},
                             "not_allowed"),
        "chemin qui remonte": ({"files": [{"path": "../x", "content": "x"}]}, "invalid_input"),
    }
    for case, (change, code) in cases.items():
        forge = Forge()
        with pytest.raises(extension.ToolError) as err:
            run(pr, forge, {**ARGS, **change})
        assert (err.value.code, forge.seen) == (code, []), case


def test_bornes_par_compte_et_pour_tout_le_proxy(pr, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(pr.time, "monotonic", lambda: now[0])
    for _ in range(pr.PER_ACCOUNT):
        run(pr, Forge())
    with pytest.raises(extension.ToolError) as err:
        run(pr, Forge())
    assert err.value.code == "too_many_requests"
    for i in range(pr.PER_HOUR - pr.PER_ACCOUNT):    # d'autres comptes, jusqu'au plafond
        run(pr, Forge(), client=f"compte-{i}")
    with pytest.raises(extension.ToolError) as err:
        run(pr, Forge(), client="compte-neuf")
    assert err.value.code == "too_many_requests"
    now[0] += 3600
    run(pr, Forge())                                 # l'heure glissante est passée


def test_echec_de_la_pr_supprime_la_branche_sans_rendre_le_jeton(pr):
    forge = Forge(fail={("POST", "/merge_requests"): 403})
    with pytest.raises(extension.ToolError) as err:
        run(pr, forge)
    branch = forge.posted("/repository/commits")["branch"]
    assert ("DELETE", "/api/v4/projects/equipe%2Fapp/repository/branches/"
            + branch.replace("/", "%2F")) in [(m, p) for m, p, *_ in forge.seen]
    assert err.value.code == "not_allowed" and TOKEN not in err.value.message


def test_un_reglage_manquant_ou_mal_type_met_l_outil_hors_service(pr, monkeypatch):
    """Plutôt qu'un refus perdu : `blocked_paths` en chaîne serait lu
    lettre par lettre."""
    assert pr.TOOL.enabled
    good = {"forge": "gitlab", "url": "https://forge.exemple.fr", "token": TOKEN,
            "projects": ["equipe/*"]}
    cases = {"sans jeton": {"token": None}, "sans projets": {"projects": None},
             "sans adresse": {"url": None}, "forge inconnue": {"forge": "svn"},
             "projets en chaîne": {"projects": "equipe/*"},
             "blocked_paths en chaîne": {"blocked_paths": "deploy/prod.sh"}}
    for case, change in cases.items():
        table = {k: v for k, v in {**good, **change}.items() if v is not None}
        monkeypatch.setitem(config.CONFIG, "extensions", {"pull_request": table})
        assert not pr.TOOL.enabled, case
    assert pr.TOOL.offered == {extension.Offer.WEB}
