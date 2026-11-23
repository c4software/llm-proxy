"""Les comptes OIDC et leurs clés : connexion de la page par un FAUX
fournisseur OIDC (découverte, JWKS, jeton signé RS256 par authlib.jose —
le client, lui, est celui d'Authlib, tel quel), rôles (premier compte
admin, suivants en attente), clés uuid4 créées, utilisées sur /v1 et
révoquées, et ce que la page refuse. Aucun réseau : le fournisseur répond
par httpx2.MockTransport."""

import asyncio
import json
import time
import uuid

import pytest
from joserfc.jwk import RSAKey
from fastapi.testclient import TestClient

from fakes import ISSUER, admin_and_user, bearer, connect, everywhere
from llm_proxy.access import accounts, oidc
from llm_proxy import app as A
from llm_proxy.access import auth
from llm_proxy.core import identity

def new_key(browser, label="pi") -> dict:
    r = browser.post("/ui/keys", json={"label": label})
    assert r.status_code == 200, r.text
    return r.json()


def test_first_account_is_admin_next_ones_wait(idp):
    alice = connect(idp)
    me = alice.get("/ui/session").json()
    assert me["login"] == "oidc"
    assert me["account"]["role"] == "admin"
    assert me["account"]["email"] == "alice@example.org"

    bob = connect(idp, "bob", "bob@example.org")
    me = bob.get("/ui/session").json()
    assert me["account"]["role"] == "pending"
    assert me["chat"]["enabled"] is False
    # En attente : ni clés, ni rien d'autre que sa session.
    assert bob.post("/ui/keys", json={}).status_code == 403
    assert bob.get("/ui/usage", params={"start_time": 0}).status_code == 403

    # L'admin l'accepte.
    users = alice.get("/ui/users").json()["data"]
    bob_id = next(u["id"] for u in users if u["email"] == "bob@example.org")
    r = alice.patch(f"/ui/users/{bob_id}", json={"role": "user"})
    assert r.status_code == 200 and r.json()["role"] == "user"
    assert bob.post("/ui/keys", json={}).status_code == 200
    # Un user n'administre rien.
    assert bob.get("/ui/users").status_code == 403
    assert bob.get("/ui/keys", params={"all": 1}).status_code == 403
    assert bob.patch(f"/ui/users/{bob_id}", json={"role": "admin"}).status_code == 403


def test_reconnecting_finds_the_same_account(idp):
    first = connect(idp).get("/ui/session").json()["account"]
    again = connect(idp).get("/ui/session").json()["account"]
    assert again["id"] == first["id"] and again["role"] == "admin"


def test_key_lifecycle_on_the_api(idp):
    alice = connect(idp)
    created = new_key(alice, "  claude   code ")
    key = created["key"]
    assert str(uuid.UUID(key)) == key            # un uuid4 brut
    assert created["label"] == "claude code"
    assert created["hint"] == key[:8]
    # Montrée une fois : la liste n'a que l'indice et le condensé.
    listed = alice.get("/ui/keys").json()["data"]
    assert [k["id"] for k in listed] == [created["id"]]
    assert key not in json.dumps(listed)

    assert idp.client.get("/v1/tools").status_code == 401
    assert idp.client.get("/v1/tools", headers=bearer(key)).status_code == 200
    assert idp.client.get("/v1/tools",
                          headers={"x-api-key": key}).status_code == 200
    assert alice.get("/ui/keys").json()["data"][0]["last_used"]

    r = alice.delete(f"/ui/keys/{created['id']}")
    assert r.status_code == 200 and r.json()["revoked"]
    assert idp.client.get("/v1/tools", headers=bearer(key)).status_code == 401


def test_a_key_does_not_open_the_page(idp):
    key = new_key(connect(idp))["key"]
    page = TestClient(A.app)
    assert page.get("/ui/session", headers=bearer(key)).status_code == 401
    assert page.get("/ui/session", params={"key": key}).status_code == 401
    assert page.post("/ui/login", json={"key": key}).status_code == 404
    # La page elle-même et l'entrée restent ouvertes.
    assert page.get("/ui").status_code == 200
    assert page.get("/ui/auth").json() == {"mode": "oidc",
                                           "label": oidc.LABEL}


def test_users_only_see_and_manage_their_own_keys(idp):
    """Un user ne voit, ne révoque et ne règle (droits d'outils) que ses
    clés ; l'admin, celles de tous."""
    alice, bob, bob_id = admin_and_user(idp)
    a = new_key(alice)
    b = new_key(bob)
    assert [k["id"] for k in bob.get("/ui/keys").json()["data"]] == [b["id"]]
    assert bob.patch(f"/ui/keys/{a['id']}", json={"tools": []}).status_code == 404
    assert bob.delete(f"/ui/keys/{a['id']}").status_code == 404
    assert idp.client.get("/v1/tools", headers=bearer(a["key"])).status_code == 200
    assert bob.patch(f"/ui/keys/{b['id']}", json={"tools": []}).status_code == 200
    every = alice.get("/ui/keys", params={"all": 1}).json()
    assert {k["id"] for k in every["data"]} == {a["id"], b["id"]}
    assert alice.patch(f"/ui/keys/{b['id']}", json={"tools": None}).status_code == 200
    assert alice.delete(f"/ui/keys/{b['id']}").status_code == 200
    assert idp.client.get("/v1/tools", headers=bearer(b["key"])).status_code == 401


def test_blocking_an_account_kills_its_keys_and_session(idp):
    alice, bob, bob_id = admin_and_user(idp)
    key = new_key(bob)["key"]
    assert idp.client.get("/v1/tools", headers=bearer(key)).status_code == 200
    alice.patch(f"/ui/users/{bob_id}", json={"role": "blocked"})
    assert idp.client.get("/v1/tools", headers=bearer(key)).status_code == 401
    assert bob.get("/ui/memory").status_code == 403
    assert bob.get("/ui/session").json()["account"]["role"] == "blocked"


def test_the_last_admin_keeps_the_role(idp):
    alice = connect(idp)
    me = alice.get("/ui/session").json()["account"]["id"]
    r = alice.patch(f"/ui/users/{me}", json={"role": "user"})
    assert r.status_code == 409
    assert alice.patch(f"/ui/users/{me}", json={"role": "chef"}).status_code == 400


def test_keys_of_an_account_share_its_client(idp):
    alice = connect(idp)
    uid = alice.get("/ui/session").json()["account"]["id"]
    k1, k2 = new_key(alice)["key"], new_key(alice)["key"]
    one = asyncio.run(auth.key_client(k1))
    two = asyncio.run(auth.key_client(k2))
    # Statistiques par clé, client (mémoire) par compte — celui de la page.
    assert one[0] != two[0]
    assert one[1] == two[1] == auth.account_owner(uid)
    assert asyncio.run(auth.key_client("pas-une-cle")) is None


def test_toml_keys_still_work_next_to_accounts(idp, monkeypatch):
    monkeypatch.setattr(auth, "PROXY_API_KEYS", ["sk-toml"])
    assert idp.client.get("/v1/tools", headers=bearer("sk-toml")).status_code == 200
    every = connect(idp).get("/ui/keys", params={"all": 1}).json()
    assert every["toml"] == [{"api_key_id": identity.owner("sk-toml"),
                              "hint": identity.owner("sk-toml")[:8],
                              "user_id": None, "user": None}]


def test_a_toml_key_bound_to_an_account_follows_it(idp, monkeypatch):
    monkeypatch.setattr(auth, "PROXY_API_KEYS", ["sk-toml"])
    digest = identity.owner("sk-toml")
    alice, bob, bob_id = admin_and_user(idp)
    # Seule, la clé est son propre client.
    assert asyncio.run(auth.key_client("sk-toml")) == (digest, digest, None, None, None)

    # Un user ne rattache rien ; l'admin, si.
    assert bob.put(f"/ui/keys/toml/{digest}",
                   json={"user_id": bob_id}).status_code == 403
    r = alice.put(f"/ui/keys/toml/{digest}", json={"user_id": bob_id})
    assert r.status_code == 200 and r.json()["user"] == "bob@example.org"
    # Elle écrit désormais dans la mémoire du compte ; ses stats aussi.
    assert asyncio.run(auth.key_client("sk-toml")) == (
        digest, auth.account_owner(bob_id), bob_id, None, None)
    assert digest in accounts.digests(bob_id)
    # Bob la voit parmi ses clés ; l'admin, dans toutes.
    mine = bob.get("/ui/keys").json()["toml"]
    assert [k["api_key_id"] for k in mine] == [digest]
    assert alice.get("/ui/keys").json()["toml"] == []

    # Elle suit l'état du compte.
    alice.patch(f"/ui/users/{bob_id}", json={"role": "blocked"})
    assert idp.client.get("/v1/tools",
                          headers=bearer("sk-toml")).status_code == 401
    alice.patch(f"/ui/users/{bob_id}", json={"role": "user"})
    assert idp.client.get("/v1/tools",
                          headers=bearer("sk-toml")).status_code == 200

    # Détachée : de nouveau son propre client.
    assert alice.put(f"/ui/keys/toml/{digest}",
                     json={"user_id": None}).json()["user_id"] is None
    assert asyncio.run(auth.key_client("sk-toml")) == (digest, digest, None, None, None)


def test_binding_refuses_what_is_not_a_toml_key(idp, monkeypatch):
    monkeypatch.setattr(auth, "PROXY_API_KEYS", ["sk-toml"])
    alice = connect(idp)
    digest = identity.owner("sk-toml")
    assert alice.put("/ui/keys/toml/" + "0" * 32,
                     json={"user_id": 1}).status_code == 404
    assert alice.put(f"/ui/keys/toml/{digest}",
                     json={"user_id": 999}).status_code == 404
    assert alice.put(f"/ui/keys/toml/{digest}",
                     json={"user_id": "1"}).status_code == 400


def test_a_forged_callback_is_refused(idp):
    browser = TestClient(A.app)
    browser.get("/ui/auth/login", follow_redirects=False)
    r = browser.get(oidc.CALLBACK_PATH + "?code=x&state=forged",
                    follow_redirects=False)
    assert r.status_code == 303 and "auth_error" in r.headers["location"]
    assert browser.get("/ui/session").status_code == 401


def test_logout_ends_the_session(idp):
    alice = connect(idp)
    assert alice.post("/ui/logout", json={}).status_code == 200
    assert alice.get("/ui/session").status_code == 401


def test_without_oidc_nothing_changes(proxy):
    assert proxy.client.get("/ui/auth").json() == {"mode": None, "label": None}
    assert proxy.client.get("/ui/keys").status_code == 404
    assert proxy.client.get("/ui/auth/login").status_code == 404
    assert proxy.client.get("/ui/admin/backup").status_code == 404
    assert proxy.client.get("/healthz").json()["ui_login"] is None


def test_a_token_signed_by_another_key_is_refused(idp):
    idp.idp.published = RSAKey.generate_key(2048, parameters={"kid": "k1"})
    browser = TestClient(A.app)
    r = browser.get("/ui/auth/login", follow_redirects=False)
    back = idp.idp.authorize(r.headers["location"], "mallory", "m@example.org")
    r = browser.get(back, follow_redirects=False)
    assert r.status_code == 303 and "auth_error" in r.headers["location"]
    assert browser.get("/ui/session").status_code == 401


def test_the_admin_sets_an_account_share(idp, monkeypatch):
    """La part d'un compte dans le partage équitable : par défaut, puis
    illimitée (0), puis un nombre — relue pendant l'attente."""
    import time

    from fakes import FakeUpstream, chat_doc
    from llm_proxy.surfaces import chat_api
    from llm_proxy.access import fairshare
    fairshare.reset()
    everywhere(monkeypatch, chat_api, "ENABLED", False)
    monkeypatch.setattr(fairshare, "DEFAULT_SHARE", 100)
    monkeypatch.setattr(fairshare, "WINDOW", 0.4)
    monkeypatch.setattr(fairshare, "CHECK_EVERY", 0.05)
    alice = connect(idp)
    uid = alice.get("/ui/session").json()["account"]["id"]
    key = new_key(alice)["key"]

    def ask():
        idp.replies.append(FakeUpstream(chat_doc(
            {"role": "assistant", "content": "ok"}, "stop", 100, 50)))
        started = time.monotonic()
        r = idp.client.post("/v1/chat/completions", headers=bearer(key),
                            json={"model": "essai/qwen", "messages": [
                                {"role": "user", "content": "q"}]})
        assert r.status_code == 200
        return time.monotonic() - started

    ask()
    assert ask() >= 0.3                                # part par défaut
    r = alice.patch(f"/ui/users/{uid}", json={"share": 0})
    assert r.status_code == 200 and r.json()["share"] == 0
    ask()
    assert ask() < 0.3                                 # illimitée
    assert alice.get("/ui/session").json()["fair_share"]["share"] is None
    assert alice.patch(f"/ui/users/{uid}", json={"share": 10_000}).json()[
        "share"] == 10_000
    assert alice.patch(f"/ui/users/{uid}", json={"share": None}).json()[
        "share"] is None
    for bad in (-1, "beaucoup", True):
        assert alice.patch(f"/ui/users/{uid}",
                           json={"share": bad}).status_code == 400
    assert alice.patch(f"/ui/users/{uid}", json={}).status_code == 400
    fairshare.reset()


def test_an_admin_downloads_a_backup(idp, tmp_path, monkeypatch):
    """Configuration → Comptes → Sauvegarde : l'archive de data/, faite à
    la demande — pour un admin seulement ; sans comptes, rien."""
    import io
    import tarfile

    from llm_proxy.core import config
    from llm_proxy.storage import memories, stats
    from llm_proxy.core import identity
    (tmp_path / "config.toml").write_text("[proxy]\n")
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.toml"))
    monkeypatch.setattr(memories, "DB_PATH", str(tmp_path / "memory.db"))
    everywhere(monkeypatch, stats, "DB_PATH", str(tmp_path / "absente.db"))
    identity.owner("x")                                  # crée client.secret
    alice = connect(idp)
    bob = connect(idp, "bob", "bob@example.org")
    r = alice.get("/ui/admin/backup")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/gzip"
    assert "llm-proxy-" in r.headers["content-disposition"]
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tar:
        # stats.db absente : sautée ; le reste y est.
        assert sorted(tar.getnames()) == ["accounts.db", "client.secret",
                                         "config.toml"]
    assert bob.get("/ui/admin/backup").status_code == 403


def test_only_an_admin_sees_the_settings(idp, tmp_path, monkeypatch):
    """Configuration → Paramètres, avec les comptes : admin seulement."""
    from llm_proxy.core import config
    (tmp_path / "config.toml").write_text("[proxy]\n")
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.toml"))
    alice = connect(idp)
    bob = connect(idp, "bob", "bob@example.org")
    assert alice.get("/ui/config").status_code == 200
    assert bob.get("/ui/config").status_code == 403
    assert bob.post("/ui/config/check", json={"toml": ""}).status_code == 403


# ── Droits d'une clé sur les outils hébergés ────────────────────────────

def listed(env, key) -> set[str]:
    r = env.client.get("/v1/tools", headers=bearer(key))
    assert r.status_code == 200, r.text
    return {t["name"] for t in r.json()["data"]}


def test_a_key_is_limited_to_the_tools_it_is_granted(idp):
    alice = connect(idp)
    every = listed(idp, new_key(alice)["key"])        # sans droits : tout
    assert len(every) >= 2, every
    # La page propose d'accorder chacun de ces outils.
    assert every <= {d["name"] for d in alice.get("/ui/keys/tools").json()["data"]}
    one, other = sorted(every)[:2]
    created = alice.post("/ui/keys", json={"label": "ci", "tools": [one, one]})
    assert created.status_code == 200, created.text
    assert created.json()["tools"] == [one]
    key = created.json()["key"]
    assert listed(idp, key) == {one}
    # Appelé directement, l'outil refusé dit pourquoi.
    r = idp.client.post(f"/v1/tools/{other}", json={}, headers=bearer(key))
    assert r.status_code == 403 and r.json()["error"]["type"] == "tool_not_permitted"
    # Aucun outil.
    none = alice.post("/ui/keys", json={"tools": []}).json()
    assert listed(idp, none["key"]) == set()
    # La liste des clés montre les droits.
    rights = {k["id"]: k["tools"] for k in alice.get("/ui/keys").json()["data"]}
    assert rights[created.json()["id"]] == [one] and rights[none["id"]] == []


def test_the_rights_of_a_key_change_afterwards(idp):
    alice = connect(idp)
    created = new_key(alice)
    every = listed(idp, created["key"])
    one = sorted(every)[0]
    r = alice.patch(f"/ui/keys/{created['id']}", json={"tools": [one]})
    assert r.status_code == 200 and r.json()["tools"] == [one], r.text
    assert listed(idp, created["key"]) == {one}
    r = alice.patch(f"/ui/keys/{created['id']}",
                    json={"tools": None, "label": " portable "})
    assert r.json()["tools"] is None and r.json()["label"] == "portable"
    assert listed(idp, created["key"]) == every
    # Mal formé, ou rien à changer.
    for body in ({"tools": "web_search"}, {"tools": [3]}, {"tools": ["a b"]}, {}):
        assert alice.patch(f"/ui/keys/{created['id']}", json=body).status_code == 400
    # Révoquée, elle ne change plus.
    alice.delete(f"/ui/keys/{created['id']}")
    assert alice.patch(f"/ui/keys/{created['id']}",
                       json={"tools": []}).status_code == 409


def test_mcp_rights_cover_a_whole_server():
    from llm_proxy import tools
    tool = next(t for t in tools.REGISTRY if t.name == "web_search")
    assert tools.granted(tool, None) and tools.granted(tool, frozenset({"web_search"}))
    assert not tools.granted(tool, frozenset({"mcp", "web_fetch"}))

    class Fake:
        name, kinds = "docs_search", ("docs_search", "mcp:docs", "mcp")
    assert tools.granted(Fake, frozenset({"mcp:docs"}))
    assert tools.granted(Fake, frozenset({"mcp"}))
    assert not tools.granted(Fake, frozenset({"mcp:autre"}))


def test_rights_survive_a_database_from_before(idp, tmp_path, monkeypatch):
    """Une base d'avant (table keys sans `tools`, users sans `models`) : les
    colonnes sont ajoutées, ses clés gardent tous les outils et son compte
    tous les modèles."""
    import sqlite3
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "issuer TEXT NOT NULL, sub TEXT NOT NULL, email TEXT NOT NULL "
                 "DEFAULT '', name TEXT NOT NULL DEFAULT '', role TEXT NOT NULL, "
                 "created REAL NOT NULL, last_login REAL, UNIQUE (issuer, sub))")
    conn.execute("CREATE TABLE keys (id TEXT PRIMARY KEY, user_id INTEGER NOT "
                 "NULL REFERENCES users(id), digest TEXT NOT NULL UNIQUE, hint "
                 "TEXT NOT NULL, label TEXT NOT NULL DEFAULT '', created REAL "
                 "NOT NULL, last_used REAL, revoked REAL)")
    conn.execute("INSERT INTO users VALUES (1, 'i', 's', '', '', 'user', 0, 0)")
    conn.execute("INSERT INTO keys VALUES ('k', 1, 'd', 'h', '', 0, NULL, NULL)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(accounts, "DB_PATH", str(path))
    assert accounts.resolve("d") == {"id": "k", "user_id": 1, "tools": None,
                                     "models": None}


def test_the_surfaces_only_see_the_granted_tools():
    """tools.Hosted() — ce que construisent les trois surfaces, /v1/tools et
    /mcp — ne garde que les outils permis par les droits de la requête."""
    import contextvars
    from llm_proxy import tools

    def build(rights):
        tools.GRANTED.set(rights)
        return set(tools.Hosted().by_name)

    every = contextvars.copy_context().run(build, None)
    assert every, "aucun outil actif dans la configuration des tests"
    one = sorted(every)[0]
    assert contextvars.copy_context().run(build, frozenset({one})) == {one}
    assert contextvars.copy_context().run(build, frozenset()) == set()
    assert tools.GRANTED.get() is None          # rien n'a fui hors du contexte
