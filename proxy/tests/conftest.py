"""La configuration de test : l'exemple du dépôt, jamais data/config.toml
(qui porte les réglages d'un déploiement réel et n'est pas versionné).
Posé AVANT tout import du paquet — config.py lit CONFIG_PATH à l'import.
Aucun import du paquet en tête de ce fichier : la fixture importe le sien
à l'usage, et fakes.py importe ce module en premier."""

import json
import os
import types

import pytest

# La racine du DÉPÔT (proxy/tests/ → deux crans plus haut) : data/ et
# docs/ y sont restés.
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
os.environ.setdefault(
    "CONFIG_PATH", os.path.join(ROOT, "data", "config.example.toml"))


@pytest.fixture(autouse=True)
def secret_client(monkeypatch, tmp_path):
    """Le secret des condensés de clés (tools.owner) dans un dossier
    temporaire : un test ne crée jamais data/client.secret."""
    from llm_proxy import tools
    monkeypatch.setattr(tools, "SECRET_PATH", str(tmp_path / "client.secret"))
    monkeypatch.setattr(tools, "_secret", None)


@pytest.fixture(autouse=True)
def fresh_catalog():
    """Chaque test relit les catalogues des backends (catalog.fetch_models
    les garde CATALOG_TTL)."""
    from llm_proxy import catalog
    catalog.forget()
    yield
    catalog.forget()


@pytest.fixture
def proxy(monkeypatch):
    """L'application entière, par le client de test de Starlette (`client`),
    les deux surfaces traduites actives ; seul l'envoi au backend
    (relaying.send_upstream) est remplacé : aucun réseau. Un backend «essai»
    sans quota, les outils factices (`hosted`, dont `hosted.runs` dit ce
    qui a été exécuté), et de quoi lire ce qui se passe :
      `replies` : ce que le backend répondra, tour après tour — un
                  FakeUpstream, ou (statut, type, message) s'il est
                  injoignable ;
      `sent`    : les corps partis au backend ;
      `lines`   : les lignes de stats — (clé, backend, modèle, endpoint,
                  statut, durée, prompt, completion, exact, flux, cache)."""
    from fastapi.testclient import TestClient

    from fakes import hosted_tools
    from llm_proxy import anthropic_api, app, backends, responses_api, stats, tools
    from llm_proxy import auth, relaying
    from llm_proxy.backends import Backend

    env = types.SimpleNamespace(replies=[], sent=[], lines=[],
                                hosted=hosted_tools())

    async def send_upstream(call, request, path, body):
        env.sent.append(json.loads(body))
        reply = env.replies.pop(0)
        return call.error(*reply) if isinstance(reply, tuple) else reply

    monkeypatch.setitem(backends.BACKENDS, "essai",
                        Backend("essai", {"url": "http://backend.invalid"}))
    monkeypatch.setattr(auth, "PROXY_API_KEYS", [])
    monkeypatch.setattr(anthropic_api, "ENABLED", True)
    monkeypatch.setattr(responses_api, "ENABLED", True)
    monkeypatch.setattr(tools, "Hosted", lambda: env.hosted)
    monkeypatch.setattr(relaying, "send_upstream", send_upstream)
    monkeypatch.setattr(stats, "record", lambda *a: env.lines.append(a))
    env.client = TestClient(app.app)
    return env
