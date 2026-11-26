"""La configuration de test : l'exemple du dépôt, jamais data/config.toml
(qui porte les réglages d'un déploiement réel et n'est pas versionné).
Posé AVANT tout import du paquet — config.py lit CONFIG_PATH à l'import.
Aucun import du paquet en tête de ce fichier : la fixture importe le sien
à l'usage, et fakes.py importe ce module en premier."""

import copy
import json
import os
import shutil
import types

import pytest

# La racine du DÉPÔT (proxy/tests/ → deux crans plus haut).
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
os.environ.setdefault(
    "CONFIG_PATH", os.path.join(ROOT, "data", "config.example.toml"))


@pytest.fixture(autouse=True)
def secret_client(monkeypatch, tmp_path):
    """Le secret des condensés de clés (identity.owner) dans un dossier
    temporaire : un test ne crée jamais data/client.secret."""
    from llm_proxy.core import identity
    monkeypatch.setattr(identity, "SECRET_PATH", str(tmp_path / "client.secret"))
    monkeypatch.setattr(identity, "_secret", None)


@pytest.fixture(autouse=True)
def config_copy(monkeypatch, tmp_path):
    """La configuration sur une COPIE de config.example.toml sous tmp_path,
    et en mémoire une copie de la configuration chargée : ce que la page
    enregistre (prompts, extensions, Configuration → Proxy) ne touche
    jamais data/, et chaque test repart de l'état neuf."""
    from llm_proxy.core import config
    path = tmp_path / "configuration" / "config.toml"
    path.parent.mkdir()
    shutil.copy(config.CONFIG_PATH, path)
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))
    monkeypatch.setattr(config, "CONFIG", copy.deepcopy(config.CONFIG))
    return path


@pytest.fixture(autouse=True)
def page_build(monkeypatch, tmp_path):
    """La page CONSTRUITE (web/dist, `npm run build`) : une doublure sous
    tmp_path — sa page, un script et un style à empreinte, le manifeste et
    les icônes de public/ — et vendor/ à côté : les tests du proxy
    n'exigent pas de build. Rend le dossier de la doublure."""
    from llm_proxy.ui import static_files
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        '<!doctype html><link rel="stylesheet" href="/ui/static/assets/index-abc123.css">'
        '<script type="module" src="/ui/static/assets/index-abc123.js"></script><div id="app"></div>',
        encoding="utf-8")
    (dist / "assets" / "index-abc123.js").write_text("export {};\n", encoding="utf-8")
    (dist / "assets" / "index-abc123.css").write_text("body{}\n", encoding="utf-8")
    public = os.path.join(static_files.WEB_DIR, "static", "public")
    for name in os.listdir(public):
        shutil.copy(os.path.join(public, name), dist / name)
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    (vendor / "versions.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(static_files, "DIST_DIR", str(dist.resolve()))
    monkeypatch.setattr(static_files, "VENDOR_DIR", str(vendor.resolve()))
    return dist


@pytest.fixture(autouse=True)
def conversations_db(monkeypatch, tmp_path):
    """La base des magasins par compte (historique, webhooks, dépôts,
    backends) dans un dossier temporaire : un changement de rôle relit les
    backends des comptes, et un test ne crée jamais data/conversations.db."""
    from llm_proxy.storage import conversations
    monkeypatch.setattr(conversations, "DB_PATH", str(tmp_path / "conversations.db"))


@pytest.fixture(autouse=True)
def fresh_direct_calls():
    """Chaque test repart sans appel direct d'outil compté
    (tool_routes.direct_wait)."""
    from llm_proxy.exposure import tool_routes
    tool_routes._direct.clear()
    yield
    tool_routes._direct.clear()


@pytest.fixture(autouse=True)
def fresh_catalog():
    """Chaque test relit les catalogues des backends (catalog.fetch_models
    les garde CATALOG_TTL)."""
    from llm_proxy.upstream import catalog
    catalog.forget()
    yield
    catalog.forget()


@pytest.fixture
def proxy(monkeypatch):
    """L'application entière, par le client de test de Starlette (`client`),
    les deux surfaces traduites actives ; seul l'envoi au backend
    (relaying.send_upstream) est remplacé : aucun réseau. Un backend «essai»
    à gabarit (comme llama.cpp : `chat_template`) sans quota, les outils
    factices (`hosted`, dont `hosted.runs` dit ce qui a été exécuté), et de
    quoi lire ce qui se passe :
      `replies` : ce que le backend répondra, tour après tour — un
                  FakeUpstream, ou (statut, type, message) s'il est
                  injoignable ;
      `sent`    : les corps partis au backend ;
      `lines`   : les lignes de stats — (clé, backend, modèle, endpoint,
                  statut, durée, prompt, completion, exact, flux, cache)."""
    from fastapi.testclient import TestClient

    from fakes import hosted_tools
    from llm_proxy.surfaces import anthropic_api, responses_api
    from llm_proxy import app, tools
    from llm_proxy.upstream import backends
    from llm_proxy.storage import stats
    from llm_proxy.access import auth
    from llm_proxy.upstream import relaying
    from llm_proxy.upstream.backends import Backend

    env = types.SimpleNamespace(replies=[], sent=[], lines=[],
                                hosted=hosted_tools())

    async def send_upstream(call, request, path, body):
        env.sent.append(json.loads(body))
        reply = env.replies.pop(0)
        return call.error(*reply) if isinstance(reply, tuple) else reply

    monkeypatch.setitem(backends.BACKENDS, "essai",
                        Backend("essai", {"url": "http://backend.invalid",
                                          "chat_template": True}))
    monkeypatch.setattr(auth, "PROXY_API_KEYS", [])
    from fakes import everywhere
    everywhere(monkeypatch, anthropic_api, "ENABLED", True)
    everywhere(monkeypatch, responses_api, "ENABLED", True)
    monkeypatch.setattr(tools, "Hosted", lambda **kw: env.hosted)
    monkeypatch.setattr(relaying, "send_upstream", send_upstream)
    everywhere(monkeypatch, stats, "record", lambda *a: env.lines.append(a))
    env.client = TestClient(app.app)
    return env


@pytest.fixture
def idp(proxy, monkeypatch, tmp_path):
    """`proxy`, les comptes OIDC actifs sur une base temporaire, et le faux
    fournisseur (`idp`) derrière le client d'Authlib."""
    import httpx2
    from fakes import FakeIdP, ISSUER
    from llm_proxy.access import accounts, oidc
    monkeypatch.setattr(accounts, "DB_PATH", str(tmp_path / "accounts.db"))
    monkeypatch.setattr(accounts, "_cache", {})
    monkeypatch.setattr(accounts, "_touched", {})
    monkeypatch.setattr(oidc, "ISSUER", ISSUER)
    monkeypatch.setattr(oidc, "CLIENT_ID", "llm-proxy")
    monkeypatch.setattr(oidc, "CLIENT_SECRET", "secret")
    monkeypatch.setattr(oidc, "ENABLED", True)
    fake = FakeIdP()
    oidc.register(transport=httpx2.MockTransport(fake.handle))
    proxy.idp = fake
    return proxy


@pytest.fixture
def stats_db(tmp_path, monkeypatch):
    """La base de statistiques sur un fichier temporaire, son écrivain
    lancé (stats.init), arrêté à la fin."""
    from fakes import everywhere
    from llm_proxy.storage import stats
    everywhere(monkeypatch, stats, "DB_PATH", str(tmp_path / "stats.db"))
    stats.init()
    yield
    stats.close()


@pytest.fixture
def ui(proxy, monkeypatch):
    """`proxy`, l'auth active (deux clés), le chat de la page ouvert, les
    outils hébergés de chat/completions actifs ; `clients` note le client
    de chaque ligne de statistiques (stats.CLIENT au moment d'écrire)."""
    from fakes import KEY, everywhere, hosted_chat
    from llm_proxy.access import auth
    from llm_proxy.storage import stats
    from llm_proxy.ui import dashboard
    monkeypatch.setattr(auth, "PROXY_API_KEYS", [KEY, "sk-bob"])
    everywhere(monkeypatch, dashboard, "UI_CHAT", True)
    hosted_chat(monkeypatch)
    proxy.clients = []
    everywhere(monkeypatch, stats, "record",
                        lambda *a: proxy.clients.append(stats.CLIENT.get()))
    everywhere(monkeypatch, stats, "record_tool", lambda *a: None)
    return proxy


@pytest.fixture
def conf(ui, tmp_path, monkeypatch):
    """`ui` connecté, sur une configuration écrite à la main (CONFIG_TOML),
    sous tmp_path."""
    from fakes import CONFIG_TOML, login
    from llm_proxy.core import config
    path = tmp_path / "config.toml"
    path.write_text(CONFIG_TOML)
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))
    login(ui)
    return ui
