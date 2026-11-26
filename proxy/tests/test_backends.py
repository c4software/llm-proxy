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
from llm_proxy.core import config
from llm_proxy.storage import backends as user_backends, conversations
from llm_proxy.ui import dashboard
from llm_proxy.upstream import backends, relaying

# Le vrai envoi au backend, avant que la fixture `proxy` ne le remplace :
# ici, c'est l'adresse et les en-têtes reçus par le backend qui comptent.
REAL_SEND = relaying.send_upstream
MODELS = ("permis-1", "autre")
# Le forfait mensuel d'un compte Mistral (GET /v1/billing/budget), tel
# que l'API le rend.
BUDGET = {"vibe_budget": {"usage_percentage": 0.0048, "initial_budget": 255.0, "currency": "EUR",
                          "reset_at": "2026-12-01T00:00:00Z", "payg_enabled": False},
          "api_budget": {"usage_percentage": 0.0, "initial_budget": 25.5, "currency": "EUR",
                         "reset_at": "2026-12-01T00:00:00Z", "payg_enabled": True},
          "usage_percentage": 0.0, "initial_budget": 25.5, "currency": "EUR"}
# Ce qu'un backend comme Mistral annonce de ses limites, à une requête du
# modèle « autre » : 1 000 requêtes la minute, dont 40 prises.
ANNOUNCED = {"x-ratelimit-limit-req-minute": "1000", "x-ratelimit-remaining-req-minute": "960",
             "x-ratelimit-limit-tokens-minute": "500000",
             "x-ratelimit-remaining-tokens-minute": "499000"}


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

    idp.limits_asked = []
    idp.gets = []

    def handler(request):
        if request.method == "GET" or request.url.path == "/tokenize":
            idp.gets.append((request.url.host, request.url.path, dict(request.headers)))
        if request.url.path == "/v1/limits":
            idp.limits_asked.append(request.url.host)
            return httpx2.Response(500)
        if request.url.path == "/v1/billing/budget":
            host = request.url.host
            return (httpx2.Response(200, json=BUDGET) if host == "perso.exemple.fr"
                    else httpx2.Response(200, text="<html>accueil</html>") if host == "html.exemple.fr"
                    else httpx2.Response(200, text="[" * 100_000) if host == "profond.exemple.fr"
                    else httpx2.Response(500) if host == "panne.exemple.fr"
                    else httpx2.Response(404))
        if request.method == "GET" and request.url.host == "panne.exemple.fr":
            return httpx2.Response(500)
        if request.method == "GET":
            # « p1 » : un alias de « permis-1 ».
            return httpx2.Response(200, json={"data": [
                {"id": m, **({"aliases": ["p1"]} if m == "permis-1" else {})} for m in MODELS]})
        if request.url.path == "/tokenize":
            return httpx2.Response(200, json={"tokens": [1] * 42})
        idp.got.append((request.url.host, dict(request.headers), json.loads(request.content)))
        if idp.got[-1][2].get("model") == "trop":
            return httpx2.Response(429, stream=Body(b'{"error": {"message": "rate limited"}}'),
                                   headers={"content-type": "application/json"})
        return httpx2.Response(200, stream=Body(chat_doc(
            {"role": "assistant", "content": "ok"}, "stop", 3, 1)),
            headers={"content-type": "application/json",
                     **(ANNOUNCED if idp.got[-1][2].get("model") == "autre" else {})})

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
        "état ni vrai ni faux": (alice, {**MINE, "name": "x", "enabled": "non"}),
        "tool_choice inconnu": (alice, {**MINE, "name": "x", "tool_choice": "toujours"}),
        "quotas ni vrai ni faux": (alice, {**MINE, "name": "x", "quotas": 1}),
        "gabarit ni vrai ni faux": (alice, {**MINE, "name": "x", "chat_template": "oui"}),
        "niveau de réflexion inconnu": (alice, {**MINE, "name": "x",
                                                 "effort_map": {"medium": "fort"}}),
        "table de niveaux mal formée": (alice, {**MINE, "name": "x", "effort_map": ["low"]}),
        "niveau de modèle inconnu": (alice, {**MINE, "name": "x",
                                             "model_efforts": {"qwen-*": ["fort"]}}),
        "modèle sans niveaux": (alice, {**MINE, "name": "x", "model_efforts": {"qwen-*": []}}),
        "niveaux des modèles mal formés": (alice, {**MINE, "name": "x",
                                                   "model_efforts": ["low"]}),
        "motif de modèle en double": (alice, {**MINE, "name": "x", "model_efforts": {
            "Qwen*": ["low"], "qwen*": ["high"]}}),
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


def test_un_backend_desactive_reste_mais_ne_sert_plus(env):
    """Désactivé : ni routé ni montré aux autres comptes, ni aux outils ;
    son créateur le garde, clé comprise, et le réactive tel quel."""
    alice, bob, _ = admin_and_user(env)
    bid = alice.post("/ui/backends", json={**MINE, "shared": True}).json()["backend"]["id"]
    off = {k: v for k, v in MINE.items() if k != "api_key"} | {"shared": True}
    r = alice.put(f"/ui/backends/{bid}", json={**off, "enabled": False})
    assert r.status_code == 200 and r.json()["backend"]["enabled"] is False, r.text
    for who in (alice, bob):
        assert ask(who, "perso/m").status_code == 400
    assert backends.route_backend({"model": "perso/m"}, limited=False) is None
    assert bob.get("/ui/backends").json()["shared"] == []
    for who in (alice, bob):
        assert not any(m["id"].startswith("perso/") for m in who.get("/ui/models").json()["data"])
    # Une modification qui ne dit rien de l'état le garde.
    alice.put(f"/ui/backends/{bid}", json={**off, "url": MINE["url"] + "/v2"})
    assert ask(bob, "perso/m").status_code == 400
    assert [b["enabled"] for b in alice.get("/ui/backends").json()["backends"]] == [False]
    # Son nom reste pris.
    assert bob.post("/ui/backends", json={**MINE, "shared": True}).status_code == 400

    alice.put(f"/ui/backends/{bid}", json={**off, "enabled": True})
    assert ask(bob, "perso/m").status_code == 200
    assert env.got[-1][1]["authorization"] == "Bearer sk-perso"


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
    assert backends.route_backend({"model": "commun/m"}, limited=False).name == "commun"
    for model in ("perso/m", "debob/m"):
        assert backends.route_backend({"model": model}, limited=False) is None, model

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


def test_tool_choice_injecte_sur_un_backend_de_compte(env):
    alice, _, _ = admin_and_user(env)
    alice.post("/ui/backends", json={**MINE, "tool_choice": "auto"})
    tool = {"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}
    key = alice.post("/ui/keys", json={"label": "cli"}).json()["key"]
    for sent, expected in (({}, "auto"), ({"tool_choice": "none"}, "none")):
        r = env.client.post("/v1/chat/completions", headers=bearer(key), json={
            "model": "perso/m", "tools": [tool], **sent,
            "messages": [{"role": "user", "content": "x"}]})
        assert r.status_code == 200, r.text
        assert env.got[-1][2]["tool_choice"] == expected, sent


def test_les_champs_du_gabarit_ne_partent_qu_a_un_backend_a_gabarit(env):
    """`chat_template_kwargs`, `store` et le `reasoning_content` /
    `reasoning` de l'historique (pi, omp) font refuser la requête par une
    API stricte (Mistral, 422) : retirés, sauf vers un backend
    `chat_template` (llama.cpp, vLLM). Le niveau de réflexion qu'ils
    portaient passe en `reasoning_effort`, sans écraser celui du client ;
    « sans réflexion » n'en donne aucun. `effort_map` ramène ensuite tout
    `reasoning_effort` aux niveaux du backend, à gabarit ou non."""
    alice, _, _ = admin_and_user(env)
    bid = alice.post("/ui/backends", json=MINE).json()["backend"]["id"]
    key = alice.post("/ui/keys", json={"label": "cli"}).json()["key"]

    history = [{"role": "user", "content": "x"},
               {"role": "assistant", "content": "y", "reasoning_content": "",
                "reasoning": "z"},
               {"role": "user", "content": "x"}]

    def sent(**extra):
        r = env.client.post("/v1/chat/completions", headers=bearer(key), json={
            "model": "perso/m", "store": False, "messages": history, **extra})
        assert r.status_code == 200, r.text
        body = env.got[-1][2]
        assert body["messages"][1]["content"] == "y"
        out = {k: body[k] for k in ("store", "chat_template_kwargs", "reasoning_effort")
               if k in body}
        out.update({k: v for k, v in body["messages"][1].items() if k.startswith("reasoning")})
        return out

    def check(cases):
        for kwargs, own, expected in cases:
            assert sent(chat_template_kwargs=kwargs, **own) == expected, (kwargs, own)
    check((
        ({"thinking": True, "reasoning_effort": "high"}, {}, {"reasoning_effort": "high"}),
        ({"enable_thinking": True, "reasoning_effort": "low"}, {"reasoning_effort": "medium"},
         {"reasoning_effort": "medium"}),
        ({"enable_thinking": False}, {}, {}),
        ({"reasoning_effort": "xhigh"}, {}, {"reasoning_effort": "xhigh"}),
    ))
    # Les niveaux de DeepSeek V4 Flash : low, high, max ; « none » retiré.
    deepseek = {"minimal": "low", "medium": "high", "xhigh": "high", "none": ""}
    alice.put(f"/ui/backends/{bid}", json={**MINE, "effort_map": deepseek})
    assert alice.get("/ui/backends").json()["backends"][0]["effort_map"] == deepseek
    check((
        ({"reasoning_effort": "medium"}, {}, {"reasoning_effort": "high"}),
        ({"reasoning_effort": "minimal"}, {}, {"reasoning_effort": "low"}),
        ({}, {"reasoning_effort": "xhigh"}, {"reasoning_effort": "high"}),
        ({}, {"reasoning_effort": "none"}, {}),
        ({}, {"reasoning_effort": "max"}, {"reasoning_effort": "max"}),
    ))
    alice.put(f"/ui/backends/{bid}", json={**MINE, "chat_template": True,
                                          "effort_map": deepseek})
    assert alice.get("/ui/backends").json()["backends"][0]["chat_template"] is True
    kwargs = {"enable_thinking": False}
    assert sent(chat_template_kwargs=kwargs, reasoning_effort="medium") == {
        "store": False, "chat_template_kwargs": kwargs, "reasoning_effort": "high",
        "reasoning_content": "", "reasoning": "z"}
    # À gabarit, le niveau reste dans les kwargs : la table s'y applique.
    assert sent(chat_template_kwargs={"reasoning_effort": "xhigh"})["chat_template_kwargs"] \
        == {"reasoning_effort": "high"}


def test_les_niveaux_proposes_par_les_modeles_d_un_backend_de_compte(env):
    """`model_efforts` : les niveaux que proposent les modèles d'un backend
    de compte, par motif, gardés tels quels et appliqués au backend routé
    (le premier motif qui vise le modèle l'emporte, à la création comme à
    la modification)."""
    alice, _, _ = admin_and_user(env)
    table = {"deepseek-*": ["none", "low", "high", "max"], "*": ["low", "medium", "high"]}
    bid = alice.post("/ui/backends", json={**MINE, "model_efforts": table}).json()["backend"]["id"]
    assert alice.get("/ui/backends").json()["backends"][0]["model_efforts"] == table
    b = backends.of_account(bid, None)
    assert b.efforts_of("DeepSeek-V4-Flash") == ["none", "low", "high", "max"]
    assert b.efforts_of("qwen3") == ["low", "medium", "high"]
    alice.put(f"/ui/backends/{bid}", json={**MINE, "model_efforts": {"qwen*": ["none", "xhigh"]}})
    b = backends.of_account(bid, None)
    assert (b.efforts_of("qwen3"), b.efforts_of("deepseek-v4")) == (["none", "xhigh"], None)


def test_seul_un_admin_pose_les_quotas_d_un_backend(env):
    """Le limiteur règle l'attente de tous sur ce backend : un compte
    ordinaire ne le pose pas, mais garde ou ôte celui qu'il a ; l'admin le
    pose aussi sur le backend d'un autre compte (?all=1), sa clé gardée."""
    alice, bob, bob_id = admin_and_user(env)
    alice_id = alice.get("/ui/session").json()["account"]["id"]
    assert bob.post("/ui/backends", json={**MINE, "quotas": True}).status_code == 403
    body = {k: v for k, v in MINE.items() if k != "api_key"} | {"shared": True}
    bid = bob.post("/ui/backends", json={**MINE, "shared": True}).json()["backend"]["id"]
    assert bob.put(f"/ui/backends/{bid}?all=1", json={**body, "quotas": True}).status_code == 403
    r = alice.put(f"/ui/backends/{bid}?all=1", json={**body, "quotas": True})
    assert r.status_code == 200 and r.json()["backend"]["quotas"] is True, r.text
    assert alice.get("/ui/health").json()["backends"]["perso"]["quotas"] is True
    ask(alice, "perso/m")
    assert env.got[-1][1]["authorization"] == "Bearer sk-perso"

    # L'accord vaut pour une adresse : vers une autre origine, Bob ne garde
    # pas les quotas.
    away = {**body, "url": "https://ailleurs.exemple.fr", "quotas": True}
    assert bob.put(f"/ui/backends/{bid}", json=away).status_code == 403
    # Bob garde les quotas, les ôte, ne les repose plus ; de même pour
    # Alice redevenue simple compte.
    for quotas, status in ((True, 200), (False, 200), (True, 403)):
        r = bob.put(f"/ui/backends/{bid}", json={**body, "quotas": quotas})
        assert r.status_code == status, (quotas, r.text)
    # Les règles `limits` : de même, posées par l'admin, gardées ou ôtées.
    rule = [{"models": ["m*"], "rpm": 5}]
    assert bob.put(f"/ui/backends/{bid}", json={**body, "limits": rule}).status_code == 403
    r = alice.put(f"/ui/backends/{bid}?all=1", json={**body, "quotas": True, "limits": rule})
    assert r.status_code == 200 and r.json()["backend"]["limits"][0]["rpm"] == 5, r.text
    assert alice.get("/ui/health").json()["backends"]["perso"]["limits"][0]["models"] == ["m*"]
    for limits, status in ((rule, 200), ([{"models": ["*"], "rpm": 99}], 403), ([], 200)):
        r = bob.put(f"/ui/backends/{bid}", json={**body, "quotas": True, "limits": limits})
        assert r.status_code == status, (limits, r.text)
    assert bob.post("/ui/backends", json={**MINE, "name": "x", "limits": rule}).status_code == 403
    assert alice.post("/ui/backends", json={**MINE, "name": "x", "limits": [{"rpm": 1}]}
                      ).status_code == 400
    assert alice.patch(f"/ui/users/{bob_id}", json={"role": "admin"}).status_code == 200
    assert bob.patch(f"/ui/users/{alice_id}", json={"role": "user"}).status_code == 200
    assert alice.put(f"/ui/backends/{bid}?all=1", json={**body, "quotas": True}).status_code == 403


def test_modifier_un_reglage_garde_les_fenetres_du_limiteur(env):
    """Changer le tool_choice, le nom ou les règles d'un backend à quotas ne
    remet pas à zéro les compteurs hors règles ; la règle neuve s'applique.
    Changer son adresse remet tout à zéro."""
    alice, _, _ = admin_and_user(env)
    body = {k: v for k, v in MINE.items() if k != "api_key"} | {"shared": True, "quotas": True}
    bid = alice.post("/ui/backends", json={**MINE, "shared": True, "quotas": True}
                     ).json()["backend"]["id"]
    limits = lambda name: alice.get("/ui/health").json()["backends"][name]["rate_limits"]
    assert ask(alice, "perso/m").status_code == 200
    used = limits("perso")
    assert used, used
    alice.put(f"/ui/backends/{bid}", json={**body, "tool_choice": "auto", "name": "autre"})
    assert limits("autre") == used
    rule = [{"models": ["n*"], "rpm": 7}]
    alice.put(f"/ui/backends/{bid}", json={**body, "name": "autre", "limits": rule})
    assert ask(alice, "autre/n1").status_code == 200
    got = limits("autre")
    assert got["rule:0"]["limits_source"] == "backend" and got["rule:0"]["minute"]["requests"] == 1
    assert {k: v for k, v in got.items() if k != "rule:0"} == used
    alice.put(f"/ui/backends/{bid}", json={**body, "name": "autre",
                                          "url": "https://ailleurs.exemple.fr"})
    assert limits("autre") == {}


def test_l_admin_voit_l_etat_des_quotas(env):
    """Configuration → Quotas : par backend à quotas, ses buckets (la
    règle, sa consommation et son plafond, marge comprise ; les limites et
    restants que l'amont annonce dans ses en-têtes, l'usage qu'il a vu hors
    du proxy compté), les 429 reçus malgré le limiteur. Réservé à
    l'admin."""
    alice, bob, _ = admin_and_user(env)
    alice.post("/ui/backends", json={**MINE, "shared": True, "quotas": True,
                                     "limits": [{"models": ["permis-*"], "rpm": 7}]})
    alice.get("/ui/models")                         # le catalogue lu
    assert ask(alice, "perso/permis-1").status_code == 200
    assert ask(alice, "perso/trop").status_code == 429
    assert bob.get("/ui/quotas").status_code == 403
    view = next(b for b in alice.get("/ui/quotas").json()["backends"] if b["name"] == "perso")
    assert (view["by"], view["upstream_429"]) == ("alice@example.org", 1)
    rule = next(x for x in view["buckets"] if x["key"] == "rule:0")
    assert (rule["source"], rule["name"], rule["minute"]["requests"],
            rule["minute"]["max_requests"]) == ("backend", "permis-*", 1, 6)
    assert rule["minute"]["frees_in"] > 0
    # « autre » : l'amont en annonce les limites.
    assert ask(alice, "perso/autre").status_code == 200
    view = next(b for b in alice.get("/ui/quotas").json()["backends"] if b["name"] == "perso")
    told = next(x for x in view["buckets"] if x["key"] == "generic:autre")
    assert (told["source"], told["minute"]["max_requests"], told["minute"]["max_tokens"],
            told["minute"]["requests"], told["remaining"]) == \
        ("en-têtes", 900, 450000, 1, {"rpm": 960, "tpm": 499000})
    assert "sk-perso" not in alice.get("/ui/quotas").text


def test_le_limiteur_d_un_backend_partage_compte_tous_les_comptes(env):
    """Partagé, un backend n'a qu'un limiteur : la consommation que montre
    Configuration → Quotas est celle de tous ses comptes, par la page comme
    par une clé."""
    alice, bob, _ = admin_and_user(env)
    alice.post("/ui/backends", json={**MINE, "shared": True, "quotas": True,
                                     "limits": [{"models": ["permis-*"], "rpm": 7}]})
    key = bob.post("/ui/keys", json={"label": "cli"}).json()["key"]
    assert ask(alice, "perso/permis-1").status_code == 200
    assert ask(bob, "perso/permis-1").status_code == 200
    r = env.client.post("/v1/chat/completions", headers=bearer(key), json={
        "model": "perso/permis-1", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 200, r.text
    view = next(b for b in alice.get("/ui/quotas").json()["backends"] if b["name"] == "perso")
    rule = next(x for x in view["buckets"] if x["key"] == "rule:0")
    assert rule["minute"]["requests"] == 3


def test_l_admin_voit_le_forfait_mensuel_d_un_compte_mistral(env, monkeypatch):
    """Configuration → Quotas : le forfait mensuel du compte d'un backend à
    quotas (Mistral : GET /v1/billing/budget, avec sa clé), lu à
    l'affichage, une fois par [quotas].limits_refresh au plus, relu par
    « Relire les limites ». Un backend qui n'en a pas (404, une page HTML,
    un corps piégé) n'est plus interrogé ; une panne passagère (500) se
    réessaie."""
    alice, _, _ = admin_and_user(env)
    hosts = ("perso", "ailleurs", "html", "profond", "panne")
    for host in hosts:
        alice.post("/ui/backends", json={**MINE, "name": host, "url": f"https://{host}.exemple.fr",
                                         "shared": True, "quotas": True})
    asked = lambda: sorted((h, hd.get("authorization")) for h, path, hd in env.gets
                           if path == "/v1/billing/budget")
    for _ in range(3):
        views = {b["name"]: b for b in alice.get("/ui/quotas").json()["backends"]}
    assert views["perso"]["budget"] == [
        {"kind": "api", "initial": 25.5, "used_percent": 0.0, "currency": "EUR",
         "reset_at": "2026-12-01T00:00:00Z", "payg": True},
        {"kind": "vibe", "initial": 255.0, "used_percent": 0.0048, "currency": "EUR",
         "reset_at": "2026-12-01T00:00:00Z", "payg": False}]
    assert views["perso"]["budget_read"]
    assert all(views[h]["budget"] == [] for h in hosts[1:])
    assert asked() == [(f"{h}.exemple.fr", "Bearer sk-perso") for h in sorted(hosts)]
    monkeypatch.setattr(backends, "REREAD_SECONDS", 0.0)
    assert alice.get("/ui/quotas?refresh=1").status_code == 200
    assert asked() == [(f"{h}.exemple.fr", "Bearer sk-perso")
                       for h in sorted(hosts + ("panne", "perso"))]


def test_le_forfait_figure_dans_l_usage_de_son_proprietaire_seul(env, stats_db):
    """L'enregistrement d'usage (ui/agent_usage.py) porte le forfait
    mensuel des backends à quotas du compte, une jauge « Monthly » par
    forfait avec sa réinitialisation, pas celui du backend d'un autre
    compte, même partagé ni pour l'admin."""
    alice, bob, _ = admin_and_user(env)
    alice.post("/ui/backends", json={**MINE, "shared": True, "quotas": True})
    bid = bob.post("/ui/backends", json={**MINE, "name": "abob"}).json()["backend"]["id"]
    r = alice.put(f"/ui/backends/{bid}?all=1", json={**MINE, "name": "abob", "quotas": True})
    assert r.status_code == 200, r.text
    limits = lambda who: who.get("/ui/usage/agent").json()["limits"]
    meter = lambda name, kind, used: {"label": f"{name} {kind}", "title": "Monthly",
                                      "percent": used, "resetsAt": "2026-12-01T00:00:00Z"}
    assert limits(alice) == [meter("perso", "API", 0.0), meter("perso", "Vibe", 0.0048 / 100)]
    assert limits(bob) == [meter("abob", "API", 0.0), meter("abob", "Vibe", 0.0048 / 100)]


def test_relire_les_limites_ne_martele_pas_l_amont(env):
    """?refresh=1 : un essai par backend toutes les 30 s au plus, réussi ou
    non ; un backend sans limites de compte (404) n'est plus redemandé."""
    alice, _, _ = admin_and_user(env)
    alice.post("/ui/backends", json={**MINE, "shared": True, "quotas": True})
    b = next(x for x in backends.ACCOUNTS.values() if x.name == "perso")
    b.quota_state.no_account = False
    b.quota_state.account_tried = 0.0
    before = len(env.limits_asked)
    for _ in range(3):
        assert alice.get("/ui/quotas?refresh=1").status_code == 200
    assert len(env.limits_asked) == before + 1


def test_sans_table_backends_aucun_backend_du_toml(monkeypatch):
    """Pas d'Albert créé d'office : un TOML sans [backends] n'en a aucun."""
    monkeypatch.setattr(config, "CONFIG", {})
    assert backends.load_backends() == {}


def test_une_requete_sans_modele_est_refusee(env):
    """Aucun backend de repli : sans `model`, rien ne part, pour un compte
    libre comme pour un compte borné."""
    alice, bob, bob_id = admin_and_user(env)
    alice.patch(f"/ui/users/{bob_id}", json={"models": ["essai/*"]})
    for who in (alice, bob):
        key = who.post("/ui/keys", json={"label": "cli"}).json()["key"]
        for method, path, body in (
                ("POST", "/v1/embeddings", {"input": "x"}),
                ("POST", "/v1/chat/completions", {"messages": [{"role": "user", "content": "x"}]}),
                ("GET", "/v1/audio/voices", None)):
            r = env.client.request(method, path, headers=bearer(key), json=body)
            assert r.status_code == 400 and r.json()["error"]["type"] == "model_required", \
                (path, r.text)
        # Un corps qui n'est pas un objet JSON non plus.
        for content in (b"pas du json", b"[]", b'"x"'):
            r = env.client.post("/v1/chat/completions", headers=bearer(key), content=content)
            assert r.status_code == 400, (content, r.text)
    assert env.got == []


def test_sans_aucun_backend_le_catalogue_est_vide(env, monkeypatch):
    alice, _, _ = admin_and_user(env)
    monkeypatch.setattr(backends, "BACKENDS", {})
    key = alice.post("/ui/keys", json={"label": "cli"}).json()["key"]
    for r in (env.client.get("/v1/models", headers=bearer(key)), alice.get("/ui/models")):
        assert r.status_code == 200 and r.json()["data"] == [], r.text


def test_les_en_tetes_propres_d_un_backend_partent_en_dernier(env, monkeypatch):
    """[backends.<nom>.headers] : ajoutés à chaque requête, ils l'emportent sur
    ceux du client ; un en-tête réglé par le proxy est refusé au démarrage."""
    alice, _, _ = admin_and_user(env)
    toml = backends.Backend("hdr", {"url": "http://hdr.invalid", "api_key": "sk-cle",
                                    "headers": {"X-Equipe": "ia", "User-Agent": "mien/1",
                                                "authorization": "Bearer autre", "X-Vide": " "}})
    toml.client = backends.open_client(toml)
    monkeypatch.setitem(backends.BACKENDS, "hdr", toml)
    assert ask(alice, "hdr/m", **{"User-Agent": "client/9"}).status_code == 200
    sent = env.got[-1][1]
    assert (sent["x-equipe"], sent["user-agent"], sent["authorization"]) == \
        ("ia", "mien/1", "Bearer autre")
    # Une valeur vide (un ${VAR} absent du .env) ne part pas.
    assert "x-vide" not in sent
    for bad in ({"Host": "x"}, {"Content-Length": "1"}, {"X-A": "a\r\nX-B: b"}, {"a b": "x"},
                {"X-A": "é"}, {"X-A": "a\x01"}):
        with pytest.raises(SystemExit):
            backends.Backend("bad", {"url": "http://x.invalid", "headers": bad})


def test_un_backend_du_toml_sans_cle_garde_l_authorization_du_client_avec_ses_en_tetes(
        env, monkeypatch):
    """Proxy ouvert, backend du TOML sans clé : son catalogue part avec
    l'Authorization du client ET ses en-têtes propres ; count_tokens
    (tokenize, par la clé du backend) aussi avec ses en-têtes."""
    monkeypatch.setattr(oidc, "ENABLED", False)
    toml = backends.Backend("hdr", {"url": "http://hdr.invalid", "tokenize_path": "/tokenize",
                                    "headers": {"X-Equipe": "ia"}})
    toml.client = backends.open_client(toml)
    monkeypatch.setitem(backends.BACKENDS, "hdr", toml)
    r = env.client.get("/v1/models", headers={"Authorization": "Bearer sk-client"})
    assert r.status_code == 200, r.text
    got = next(h for host, path, h in env.gets if host == "hdr.invalid" and path == "/v1/models")
    assert (got["authorization"], got["x-equipe"]) == ("Bearer sk-client", "ia")
    r = env.client.post("/v1/messages/count_tokens", json={
        "model": "hdr/m", "messages": [{"role": "user", "content": "x"}]})
    assert r.json() == {"input_tokens": 42}, r.text
    assert next(h for host, path, h in env.gets if path == "/tokenize")["x-equipe"] == "ia"


def test_les_en_tetes_d_un_backend_de_compte_restent_secrets(env):
    """Envoyés au backend, jamais rendus (ni au créateur, ni aux autres, ni à
    l'admin) : la page n'en voit que les noms. Une valeur vide garde
    l'ancienne, sauf vers une autre origine ; absents, ils sont retirés."""
    alice, bob, _ = admin_and_user(env)
    heads = {"X-Secret": "s3cret", "X-Autre": "a"}
    bid = bob.post("/ui/backends", json={**MINE, "shared": True, "headers": heads}
                   ).json()["backend"]["id"]
    ask(alice, "perso/m")
    assert env.got[-1][1]["x-secret"] == "s3cret"
    for page in (bob.get("/ui/backends"), alice.get("/ui/backends"),
                 alice.get("/ui/backends?all=1")):
        assert "s3cret" not in page.text, page.text
    assert bob.get("/ui/backends").json()["backends"][0]["headers"] == ["X-Autre", "X-Secret"]

    body = {k: v for k, v in MINE.items() if k != "api_key"} | {"shared": True}
    assert bob.put(f"/ui/backends/{bid}", json={**body, "headers": {"X-Secret": ""}}).status_code == 200
    ask(alice, "perso/m")
    assert env.got[-1][1]["x-secret"] == "s3cret" and "x-autre" not in env.got[-1][1]
    # L'admin qui bascule les quotas (?all=1) renvoie les noms vides : gardés.
    assert alice.put(f"/ui/backends/{bid}?all=1", json={
        **body, "quotas": True, "headers": {"X-Secret": ""}}).status_code == 200
    ask(alice, "perso/m")
    assert env.got[-1][1]["x-secret"] == "s3cret"
    away = {**body, "url": "https://ailleurs.exemple.fr", "headers": {"X-Secret": ""}}
    assert bob.put(f"/ui/backends/{bid}", json=away).status_code == 400
    # Absents d'une modification : gardés ; une table vide les retire.
    assert bob.put(f"/ui/backends/{bid}", json=body).status_code == 200
    ask(alice, "perso/m")
    assert env.got[-1][1]["x-secret"] == "s3cret"
    assert "s3cret" not in alice.get("/ui/quotas").text
    # Un autre compte ne les touche pas.
    assert alice.put(f"/ui/backends/{bid}", json={**body, "headers": {}}).status_code == 404
    assert bob.put(f"/ui/backends/{bid}", json={**body, "headers": {}}).status_code == 200
    ask(alice, "perso/m")
    assert "x-secret" not in env.got[-1][1]
    for bad in ({"Host": "x"}, {"X-A": "a\nb"}, {"X-A": ""}, {"X-A": "é"}):
        r = bob.post("/ui/backends", json={**MINE, "name": "x", "headers": bad})
        assert r.status_code == 400, (bad, r.text)


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


# ── les modèles exposés d'un backend ────────────────────────────────────

def test_un_backend_n_expose_que_ses_modeles_coches(env):
    """`models` borne ce que le backend expose, à tous, son créateur
    compris : ni listé, ni routé (404 `model_not_found`), ni pour un outil ;
    un alias suit son modèle. Absent d'une modification, il reste ; vide,
    tout est exposé (sans reconstruire le backend)."""
    alice, bob, _ = admin_and_user(env)
    bid = alice.post("/ui/backends", json={**MINE, "shared": True, "models": ["Permis-*"]}) \
        .json()["backend"]["id"]
    ids = lambda who: {m["id"] for m in who.get("/ui/models").json()["data"]
                       if m["id"].startswith("perso/")}
    for who in (alice, bob):
        assert ids(who) == {"perso/permis-1"}
        for model, status in (("perso/PERMIS-1", 200), ("perso/p1", 200), ("perso/autre", 404)):
            r = ask(who, model)
            assert r.status_code == status, (model, r.text)
        assert r.json()["error"]["type"] == "model_not_found"
    assert backends.route_backend({"model": "perso/autre"}, limited=False) is None
    assert backends.route_backend({"model": "perso/permis-1"}, limited=False) is not None
    health = alice.get("/ui/health").json()["backends"]["perso"]
    assert health["last_seen_models"] == health["chat_models"] == ["perso/permis-1"]

    base = {k: v for k, v in MINE.items() if k != "api_key"} | {"shared": True}
    alice.put(f"/ui/backends/{bid}", json=base)
    assert alice.get("/ui/backends").json()["backends"][0]["models"] == ["Permis-*"]
    assert ask(bob, "perso/autre").status_code == 404
    alice.put(f"/ui/backends/{bid}", json={**base, "models": ["autre"]})
    assert ask(bob, "perso/p1").status_code == 404
    alice.put(f"/ui/backends/{bid}", json={**base, "models": []})
    assert ids(bob) == {"perso/permis-1", "perso/autre"}


def test_le_catalogue_entier_d_un_backend_se_lit_pour_cocher(env):
    """GET /ui/backends/{id}/models : tout le catalogue, et ce que le filtre
    expose ; le sien seulement (l'admin : n'importe lequel, par ?all=1),
    actif, 502 s'il ne répond pas."""
    alice, bob, _ = admin_and_user(env)
    bid = alice.post("/ui/backends", json={**MINE, "shared": True, "models": ["permis-*"]}) \
        .json()["backend"]["id"]
    bob_bid = bob.post("/ui/backends", json={**MINE, "name": "abob"}).json()["backend"]["id"]
    assert alice.get(f"/ui/backends/{bid}/models").json() == {"models": [
        {"id": "autre", "exposed": False}, {"id": "permis-1", "exposed": True}]}
    for who, url, status in ((bob, f"/ui/backends/{bid}/models", 404),
                             (bob, f"/ui/backends/{bid}/models?all=1", 403),
                             (bob, f"/ui/backends/{bob_bid}/models", 200),
                             (alice, f"/ui/backends/{bob_bid}/models", 404),
                             (alice, f"/ui/backends/{bob_bid}/models?all=1", 200)):
        assert who.get(url).status_code == status, url
    down = bob.post("/ui/backends", json={**MINE, "name": "panne",
                                         "url": "https://panne.exemple.fr"}).json()["backend"]["id"]
    assert bob.get(f"/ui/backends/{down}/models").status_code == 502
    base = {k: v for k, v in MINE.items() if k != "api_key"} | {"shared": True}
    alice.put(f"/ui/backends/{bid}", json={**base, "enabled": False})
    assert alice.get(f"/ui/backends/{bid}/models").status_code == 404


def test_des_modeles_exposes_mal_formes_sont_refuses(env):
    alice, _, _ = admin_and_user(env)
    for bad in ("permis-*", [""], [3], ["x" * 201], [f"m{i}" for i in range(501)]):
        r = alice.post("/ui/backends", json={**MINE, "models": bad})
        assert r.status_code == 400, bad


def test_un_backend_du_toml_borne_ses_modeles(env, monkeypatch):
    """[backends.<nom>].models : la même borne pour un backend du TOML,
    motifs sans casse ; une valeur qui n'est pas une liste arrête le
    démarrage."""
    alice, _, _ = admin_and_user(env)
    monkeypatch.setattr(config, "CONFIG", {"backends": {"essai": {
        "url": "http://backend.invalid", "models": ["AUTRE"]}}})
    narrow = backends.load_backends()["essai"]
    narrow.client = backends.open_client(narrow)
    monkeypatch.setitem(backends.BACKENDS, "essai", narrow)
    assert {m["id"] for m in alice.get("/ui/models").json()["data"]
            if m["id"].startswith("essai/")} == {"essai/autre"}
    assert ask(alice, "essai/permis-1").status_code == 404
    monkeypatch.setattr(config, "CONFIG", {"backends": {"x": {"url": "http://x.invalid",
                                                              "models": "autre"}}})
    with pytest.raises(SystemExit):
        backends.load_backends()


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
