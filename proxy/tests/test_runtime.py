"""La partie Exécution de la page (ui/runtime_routes.py) et le client de
l'exécuteur qu'elle emploie (tools/code_execution.py) : chacun voit et
détruit ses bacs ; un admin voit l'exécuteur, les bacs de tous et les
caches du proxy, et les vide. L'exécuteur est un faux à la frontière HTTP
(httpx2.MockTransport), aucun réseau."""

import json
import types

import httpx2
import pytest

from fakes import admin_and_user, connect, go
from llm_proxy.access import auth
from llm_proxy.tools import code_execution, webcache


class Executor:
    """Un faux exécuteur : ses bacs, et ce qu'on lui demande."""

    def __init__(self):
        self.bacs = [{"client": "", "session": "conv1"}, {"client": "autre", "session": "repository"}]
        self.calls = []

    def handler(self, request):
        body = json.loads(request.content) if request.content else {}
        self.calls.append((request.url.path, body, dict(request.url.params)))
        assert request.headers["authorization"] == "Bearer t"
        path, client = request.url.path, request.url.params.get("client")
        if path == "/v1/status":
            return httpx2.Response(200, json={"status": "ok", "sessions": len(self.bacs)})
        if path == "/v1/sessions":
            return httpx2.Response(200, json={"data": [
                {**b, "age": 1, "idle_for": 1, "idle": 1800, "expires_in": 1799, "running": False}
                for b in self.bacs if client is None or b["client"] == client]})
        if path == "/v1/destroy":
            gone = [b for b in self.bacs if (b["client"], b["session"]) == (body["client"], body["session"])]
            self.bacs = [b for b in self.bacs if b not in gone]
            return httpx2.Response(200, json={"destroyed": bool(gone)})
        if path == "/v1/destroy_all":
            gone = [b for b in self.bacs if "client" not in body or b["client"] == body["client"]]
            self.bacs = [b for b in self.bacs if b not in gone]
            return httpx2.Response(200, json={"destroyed": len(gone)})
        return httpx2.Response(404)


@pytest.fixture
def executor(monkeypatch):
    """Le faux exécuteur, branché sous le client : toute requête vers lui
    passe par son transport."""
    fake = Executor()
    monkeypatch.setattr(code_execution, "URL", "http://executor.invalid")
    monkeypatch.setattr(code_execution, "TOKEN", "t")
    # Un httpx2 propre à code_execution : son client passe par le faux
    # exécuteur ; le reste du proxy (le fournisseur OIDC des tests) garde
    # le vrai.
    local = types.SimpleNamespace(**vars(httpx2))
    local.AsyncClient = lambda *a, **k: httpx2.AsyncClient(
        *a, **{**k, "transport": httpx2.MockTransport(fake.handler)})
    monkeypatch.setattr(code_execution, "httpx2", local)
    return fake


# ── le client de l'exécuteur ────────────────────────────────────────────

def test_client_de_l_executeur(executor):
    assert go(code_execution.status())["status"] == "ok"
    assert [b["session"] for b in go(code_execution.sessions("autre"))] == ["repository"]
    assert executor.calls[-1][2] == {"client": "autre"}
    assert go(code_execution.destroy("", "conv1")) is True
    assert go(code_execution.destroy_all("autre")) == 1
    assert executor.calls[-1][1] == {"client": "autre"}
    assert go(code_execution.destroy_all()) == 0 and executor.calls[-1][1] == {}


def test_executeur_injoignable_ou_ancien(executor, monkeypatch):
    """Une route absente (exécuteur d'avant) ou injoignable : None, jamais
    d'exception."""
    monkeypatch.setattr(executor, "handler", lambda request: httpx2.Response(404))
    assert go(code_execution.sessions()) is None and go(code_execution.destroy_all()) is None


# ── la page ─────────────────────────────────────────────────────────────

def test_proxy_ouvert_la_page_est_admin(proxy, executor):
    """Sans comptes OIDC, la page connectée est admin, comme pour la
    configuration du proxy."""
    c = proxy.client
    j = c.get("/ui/runtime").json()
    assert [b["session"] for b in j["own"]["bacs"]] == ["conv1"]
    assert [b["account"] for b in j["admin"]["bacs"]] == ["proxy ouvert", "#autre"]
    assert c.post("/ui/runtime/bacs/destroy", json={"session": "repository",
                                                    "client": "autre"}).json() == {"destroyed": True}
    webcache.CACHE.put("k", "v", 1)
    assert c.post("/ui/runtime/caches/web/clear", json={}).json() == {"name": "web", "cleared": 1}
    assert c.post("/ui/runtime/caches/inconnu/clear", json={}).status_code == 404


def test_un_compte_ne_touche_qu_a_ses_bacs(idp, executor):
    """OIDC : un user voit et détruit SES bacs ; ni ceux d'un autre, ni tous,
    ni les caches. L'admin, si."""
    alice, bob, bob_id = admin_and_user(idp)
    bob_owner = auth.account_owner(bob_id)
    executor.bacs.append({"client": bob_owner, "session": "conv9"})
    j = bob.get("/ui/runtime").json()
    assert "admin" not in j and [b["session"] for b in j["own"]["bacs"]] == ["conv9"]
    assert bob.post("/ui/runtime/bacs/destroy", json={"session": "conv1", "client": ""}).status_code == 403
    assert bob.post("/ui/runtime/bacs/destroy_all", json={"everyone": True}).status_code == 403
    assert bob.post("/ui/runtime/caches/web/clear", json={}).status_code == 403
    assert bob.post("/ui/runtime/bacs/destroy_all", json={}).json() == {"destroyed": 1}
    assert executor.calls[-1][1] == {"client": bob_owner}
    admin = alice.get("/ui/runtime").json()["admin"]
    assert {b["account"] for b in admin["bacs"]} >= {"#autre"}
    assert alice.post("/ui/runtime/bacs/destroy_all", json={"everyone": True}).json()["destroyed"] == 2


def test_executeur_injoignable_dans_la_page(proxy, executor, monkeypatch):
    monkeypatch.setattr(executor, "handler", lambda request: httpx2.Response(503))
    assert proxy.client.get("/ui/runtime").json()["own"]["bacs"] is None
    assert proxy.client.post("/ui/runtime/bacs/destroy_all", json={}).status_code == 502
