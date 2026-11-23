"""Les backends des comptes (storage/backends.py, ui/backend_routes.py,
upstream/backends.py) et les modèles permis d'un compte (colonne `models`
de access/accounts.py) : ce qu'un compte déclare, qui le voit, avec quelle
clé la requête part, ce que l'admin borne. Sans réseau : chaque backend,
celui du TOML comme ceux des comptes, parle à un MockTransport qui note ce
qu'il reçoit et sert un catalogue."""

import json

import httpx2
import pytest

from fakes import admin_and_user, bearer, chat_doc, everywhere

from llm_proxy.access import oidc
from llm_proxy.storage import backends as user_backends, conversations
from llm_proxy.ui import dashboard
from llm_proxy.upstream import backends, relaying

# Le vrai envoi au backend, avant que la fixture `proxy` ne le remplace :
# ici, c'est l'adresse et les en-têtes reçus par le backend qui comptent.
REAL_SEND = relaying.send_upstream
MODELS = ("permis-1", "autre")


class Body(httpx2.AsyncByteStream):
    """Un corps que le relais lit en flux (aiter_raw), comme sur le réseau
    (FakeUpstream remplace l'envoi ; ici, c'est le transport)."""

    def __init__(self, data: bytes):
        self.data = data

    async def __aiter__(self):
        yield self.data


@pytest.fixture
def env(idp, monkeypatch, tmp_path):
    """`idp` (comptes OIDC), les backends des comptes actifs, et tous les
    backends derrière un transport qui note (hôte, en-têtes, corps) dans
    `env.got` et sert MODELS à GET /v1/models."""
    monkeypatch.setattr(conversations, "DB_PATH", str(tmp_path / "conversations.db"))
    monkeypatch.setattr(user_backends, "ENABLED", True)
    monkeypatch.setattr(backends, "ACCOUNTS", {})
    monkeypatch.setattr(relaying, "send_upstream", REAL_SEND)
    everywhere(monkeypatch, dashboard, "UI_CHAT", True)
    idp.got = []

    def handler(request):
        if request.method == "GET":
            return httpx2.Response(200, json={"data": [{"id": m} for m in MODELS]})
        if request.url.path == "/tokenize":
            return httpx2.Response(200, json={"tokens": [1] * 42})
        idp.got.append((request.url.host, dict(request.headers), json.loads(request.content)))
        return httpx2.Response(200, stream=Body(chat_doc(
            {"role": "assistant", "content": "ok"}, "stop", 3, 1)),
            headers={"content-type": "application/json"})

    def client(b):
        return httpx2.AsyncClient(base_url=b.url, transport=httpx2.MockTransport(handler))
    monkeypatch.setattr(backends, "open_client", client)
    essai = backends.Backend("essai", {"url": "http://backend.invalid"})
    essai.client = client(essai)
    monkeypatch.setitem(backends.BACKENDS, "essai", essai)
    return idp


def ask(browser, model, **headers):
    return browser.post("/ui/chat/completions", headers=headers, json={
        "model": model, "messages": [{"role": "user", "content": "bonjour"}]})


MINE = {"name": "perso", "api": "openai", "url": "https://perso.exemple.fr",
        "api_key": "sk-perso", "shared": False}


# ── le magasin et la page ───────────────────────────────────────────────

def test_cle_jamais_rendue_et_backend_cloisonne(env):
    alice, bob, _ = admin_and_user(env)
    r = alice.post("/ui/backends", json=MINE)
    assert r.status_code == 200, r.text
    bid = r.json()["backend"]["id"]
    alice.post("/ui/backends", json={**MINE, "name": "deux", "shared": True})
    for page in (alice.get("/ui/backends"), alice.get("/ui/backends?all=1"),
                 bob.get("/ui/backends")):
        assert "sk-perso" not in page.text, page.text
    assert [b["name"] for b in alice.get("/ui/backends").json()["backends"]] == ["perso", "deux"]

    # Bob ne voit pas celui d'Alice, ne le modifie pas, ne le supprime pas ;
    # il voit le partagé et qui le partage.
    seen = bob.get("/ui/backends").json()
    assert seen["backends"] == []
    assert [(b["name"], b["by"]) for b in seen["shared"]] == [("deux", "alice@example.org")]
    assert bob.put(f"/ui/backends/{bid}", json={**MINE, "url": "https://bob.exemple.fr"}).status_code == 404
    assert bob.delete(f"/ui/backends/{bid}").json()["deleted"] is False
    assert alice.get("/ui/backends").json()["backends"][0]["url"] == MINE["url"]


def test_l_admin_voit_et_retire_les_backends_de_tous(env):
    alice, bob, _ = admin_and_user(env)
    bid = bob.post("/ui/backends", json=MINE).json()["backend"]["id"]
    # Bob n'est pas admin.
    assert "all" not in bob.get("/ui/backends?all=1").json()
    assert bob.delete(f"/ui/backends/{bid}?all=1").status_code == 403
    every = alice.get("/ui/backends?all=1").json()["all"]
    assert [(b["name"], b["by"]) for b in every] == [("perso", "bob@example.org")]
    assert alice.delete(f"/ui/backends/{bid}?all=1").json()["deleted"] is True
    assert bob.get("/ui/backends").json()["backends"] == []
    assert ask(bob, "perso/m").status_code == 400


def test_noms_et_bornes_refuses(env, monkeypatch):
    alice, bob, _ = admin_and_user(env)
    assert alice.post("/ui/backends", json={**MINE, "shared": True}).status_code == 200
    cases = {
        "nom d'un backend du proxy": (alice, {**MINE, "name": "essai"}),
        "nom pris par un de ses backends": (alice, {**MINE, "name": "PERSO"}),
        "nom d'un autre backend partagé": (bob, {**MINE, "shared": True}),
        "préfixe illisible": (alice, {**MINE, "name": "a/b"}),
        "identifiants dans l'adresse": (alice, {**MINE, "name": "x", "url": "https://u:p@h.fr"}),
        "protocole inconnu": (alice, {**MINE, "name": "x", "api": "grpc"}),
        "images ni vrai ni faux": (alice, {**MINE, "name": "x", "images": "oui"}),
        "tokenisation hors de l'adresse": (alice, {**MINE, "name": "x", "tokenize_path": "/../x"}),
        "tokenisation sans chemin": (alice, {**MINE, "name": "x", "tokenize_path": "tokenize"}),
        "tokenisation vers un hôte": (alice, {**MINE, "name": "x", "tokenize_path": "//h/x"}),
    }
    for case, (who, body) in cases.items():
        assert who.post("/ui/backends", json=body).status_code == 400, case
    # Pour lui seul, Bob peut reprendre le nom : le sien masque celui d'Alice.
    assert bob.post("/ui/backends", json=MINE).status_code == 200
    monkeypatch.setattr(user_backends, "MAX_PER_USER", 1)
    assert bob.post("/ui/backends", json={**MINE, "name": "deux"}).status_code == 413


def test_la_cle_suit_l_adresse(env):
    """Sans clé ressaisie, la clé reste ; elle ne part pas vers une autre
    origine."""
    alice, _, _ = admin_and_user(env)
    bid = alice.post("/ui/backends", json=MINE).json()["backend"]["id"]
    body = {k: v for k, v in MINE.items() if k != "api_key"}
    assert alice.put(f"/ui/backends/{bid}", json={**body, "url": MINE["url"] + "/v2"}).status_code == 200
    ask(alice, "perso/m")
    assert env.got[-1][1]["authorization"] == "Bearer sk-perso"
    assert alice.put(f"/ui/backends/{bid}", json={**body, "url": "https://autre.exemple.fr"}).status_code == 200
    ask(alice, "perso/m")
    assert env.got[-1][0] == "autre.exemple.fr"
    assert "authorization" not in env.got[-1][1]


def test_inactifs_ni_page_ni_routage(env, monkeypatch):
    alice, bob, bob_id = admin_and_user(env)
    alice.post("/ui/backends", json=MINE)
    assert ask(alice, "perso/m").status_code == 200
    monkeypatch.setattr(user_backends, "ENABLED", False)
    assert alice.get("/ui/backends").status_code == 404
    assert alice.get("/ui/session").json()["backends"] is False
    # Relu (au démarrage, ou après un changement de rôle) : plus routé.
    alice.patch(f"/ui/users/{bob_id}", json={"role": "user"})
    assert ask(alice, "perso/m").status_code == 400


# ── le routage ──────────────────────────────────────────────────────────

def test_un_backend_a_soi_ne_sert_que_son_compte(env):
    alice, bob, _ = admin_and_user(env)
    alice.post("/ui/backends", json=MINE)
    r = ask(alice, "perso/qwen3")
    assert r.status_code == 200, r.text
    host, headers, body = env.got[-1]
    assert (host, headers["authorization"], body["model"]) == \
        ("perso.exemple.fr", "Bearer sk-perso", "qwen3")
    r = ask(bob, "perso/qwen3")
    assert r.status_code == 400 and r.json()["error"]["type"] == "unknown_backend_prefix"
    assert len(env.got) == 1


def test_un_backend_partage_sert_tous_avec_la_cle_du_createur(env):
    alice, bob, _ = admin_and_user(env)
    alice.post("/ui/backends", json={**MINE, "shared": True})
    # Par une clé de Bob aussi, pas seulement par sa page.
    key = bob.post("/ui/keys", json={"label": "cli"}).json()["key"]
    r = env.client.post("/v1/chat/completions", headers=bearer(key), json={
        "model": "perso/m", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 200, r.text
    assert env.got[-1][1]["authorization"] == "Bearer sk-perso"

    # Un backend de Bob du même nom masque le partagé, pour lui seul.
    bob.post("/ui/backends", json={**MINE, "url": "https://bob.exemple.fr", "api_key": "sk-bob"})
    ask(bob, "perso/m")
    assert (env.got[-1][0], env.got[-1][1]["authorization"]) == ("bob.exemple.fr", "Bearer sk-bob")
    ask(alice, "perso/m")
    assert env.got[-1][0] == "perso.exemple.fr"


def test_un_backend_partage_sert_les_outils_et_le_partage_equitable(env):
    """Comme un backend du TOML : un outil hébergé peut le viser (partagé
    par un admin seulement : le nom d'un partagé est au premier qui le
    prend), ses requêtes comptent dans le partage équitable, ses réglages
    valent et suivent une modification. Un backend à soi ne sert que son
    compte, ni outil ni partage."""
    alice, bob, _ = admin_and_user(env)
    bid = alice.post("/ui/backends", json={**MINE, "name": "commun", "shared": True,
                                           "images": True}).json()["backend"]["id"]
    bob.post("/ui/backends", json={**MINE, "name": "debob", "shared": True})
    alice.post("/ui/backends", json={**MINE, "name": "claude", "api": "anthropic",
                                     "shared": True})
    alice.post("/ui/backends", json={**MINE, "images": True})
    assert backends.route_backend({"model": "commun/m"}, limited=False)[0].name == "commun"
    for model in ("perso/m", "debob/m"):
        assert backends.route_backend({"model": model}, limited=False)[0] is None, model

    health = alice.get("/ui/health").json()["backends"]
    assert "perso" not in health
    assert {n: (health[n]["images"], health[n]["fair_share"]) for n in ("commun", "claude")} \
        == {"commun": (True, True), "claude": (True, True)}
    # Le chemin de tokenisation, posé après coup, sert.
    alice.put(f"/ui/backends/{bid}", json={**MINE, "name": "commun", "shared": True,
                                          "images": True, "tokenize_path": "/tokenize"})
    key = bob.post("/ui/keys", json={"label": "cli"}).json()["key"]
    r = env.client.post("/v1/messages/count_tokens", headers=bearer(key), json={
        "model": "commun/m", "messages": [{"role": "user", "content": "bonjour"}]})
    assert r.status_code == 200 and r.json() == {"input_tokens": 42}, r.text


def test_un_createur_bloque_perd_ses_backends_partages(env):
    alice, bob, bob_id = admin_and_user(env)
    bob.post("/ui/backends", json={**MINE, "shared": True})
    assert ask(alice, "perso/m").status_code == 200
    alice.patch(f"/ui/users/{bob_id}", json={"role": "blocked"})
    assert ask(alice, "perso/m").status_code == 400
    alice.patch(f"/ui/users/{bob_id}", json={"role": "user"})
    assert ask(alice, "perso/m").status_code == 200


def test_un_nom_du_toml_l_emporte(env, monkeypatch):
    alice, _, _ = admin_and_user(env)
    alice.post("/ui/backends", json=MINE)
    toml = backends.Backend("perso", {"url": "http://toml.invalid"})
    toml.client = backends.open_client(toml)
    monkeypatch.setitem(backends.BACKENDS, "perso", toml)
    ask(alice, "perso/m")
    assert env.got[-1][0] == "toml.invalid"


def test_les_identifiants_du_client_ne_partent_pas_vers_un_backend_de_compte(env, monkeypatch):
    """Sans clé, un backend du TOML reçoit l'Authorization du client (proxy
    ouvert) ; celui d'un compte jamais, ni ses cookies : son adresse est
    celle qu'a choisie un compte, peut-être un autre."""
    monkeypatch.setattr(oidc, "ENABLED", False)
    env.client.post("/ui/backends", json={**MINE, "api_key": "", "shared": True})
    for model, sent in (("essai/m", True), ("perso/m", False)):
        r = env.client.post("/v1/chat/completions",
                            headers={"Authorization": "Bearer sk-client", "Cookie": "s=1"},
                            json={"model": model, "messages": [{"role": "user", "content": "x"}]})
        assert r.status_code == 200, (model, r.text)
        headers = env.got[-1][1]
        assert ("authorization" in headers) is sent, model
        if not sent:
            assert "cookie" not in headers, model


# ── les modèles permis d'un compte ──────────────────────────────────────

def test_l_admin_borne_les_modeles_d_un_compte(env):
    alice, bob, bob_id = admin_and_user(env)
    alice.post("/ui/backends", json={**MINE, "name": "alice", "shared": True})
    bob.post("/ui/backends", json={**MINE, "name": "bob"})
    # Un user ne se règle pas lui-même.
    assert bob.patch(f"/ui/users/{bob_id}", json={"models": None}).status_code == 403
    r = alice.patch(f"/ui/users/{bob_id}", json={"models": ["essai/permis-*"]})
    assert r.status_code == 200 and r.json()["models"] == ["essai/permis-*"]

    key = bob.post("/ui/keys", json={"label": "cli"}).json()["key"]
    surfaces = {
        "page": lambda m: ask(bob, m),
        "chat": lambda m: env.client.post("/v1/chat/completions", headers=bearer(key), json={
            "model": m, "messages": [{"role": "user", "content": "x"}]}),
        "responses": lambda m: env.client.post("/v1/responses", headers=bearer(key), json={
            "model": m, "input": "x"}),
        "messages": lambda m: env.client.post("/v1/messages", headers=bearer(key), json={
            "model": m, "max_tokens": 10, "messages": [{"role": "user", "content": "x"}]}),
        "relais": lambda m: env.client.post("/v1/embeddings", headers=bearer(key), json={
            "model": m, "input": "x"}),
    }
    for surface, send in surfaces.items():
        for model, status in (("essai/permis-1", 200), ("ESSAI/Permis-2", 200),
                              ("essai/autre", 403), ("alice/m", 403),
                              # Ses propres backends ne sont pas bornés.
                              ("bob/m", 200)):
            r = send(model)
            assert r.status_code == status, (surface, model, r.text)
    r = surfaces["chat"]("essai/autre")
    assert r.json()["error"]["type"] == "model_not_allowed"
    # Sans modèle, le backend de repli servirait un modèle non permis.
    r = env.client.post("/v1/embeddings", headers=bearer(key), json={"input": "x"})
    assert r.status_code == 403, r.text

    # Alice n'est pas bornée ; aucun motif = aucun modèle.
    assert ask(alice, "essai/autre").status_code == 200
    alice.patch(f"/ui/users/{bob_id}", json={"models": []})
    assert ask(bob, "essai/permis-1").status_code == 403
    alice.patch(f"/ui/users/{bob_id}", json={"models": None})
    assert ask(bob, "essai/autre").status_code == 200
    for bad in ("essai/*", ["a b"], [f"m{i}" for i in range(201)]):
        assert alice.patch(f"/ui/users/{bob_id}", json={"models": bad}).status_code == 400, bad


def test_le_catalogue_ne_montre_que_les_modeles_permis(env):
    alice, bob, bob_id = admin_and_user(env)
    bob.post("/ui/backends", json={**MINE, "name": "bob"})
    alice.patch(f"/ui/users/{bob_id}", json={"models": ["essai/permis-*"]})
    ids = lambda who: {m["id"] for m in who.get("/ui/models").json()["data"]
                       if m["id"].startswith(("essai/", "bob/"))}
    assert ids(bob) == {"essai/permis-1", "bob/permis-1", "bob/autre"}
    assert ids(alice) == {"essai/permis-1", "essai/autre"}
