"""La configuration de test : l'exemple du dépôt, jamais data/config.toml
(qui porte les réglages d'un déploiement réel et n'est pas versionné).
Posé AVANT tout import du paquet — config.py lit CONFIG_PATH à l'import.
Aucun import du paquet en tête de ce fichier : la fixture importe le sien
à l'usage, et tests/fakes.py importe ce module en premier."""

import json
import os
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault(
    "CONFIG_PATH", os.path.join(ROOT, "data", "config.example.toml"))


@pytest.fixture
def proxy(monkeypatch):
    """L'application entière, par le client de test de Starlette (`client`),
    les deux surfaces traduites actives ; seul l'envoi au backend
    (app.send_upstream) est remplacé : aucun réseau. Un backend «essai»
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
    from llm_proxy import app
    from llm_proxy.backends import Backend

    env = types.SimpleNamespace(replies=[], sent=[], lines=[],
                                hosted=hosted_tools())

    async def send_upstream(call, request, path, body):
        env.sent.append(json.loads(body))
        reply = env.replies.pop(0)
        return call.error(*reply) if isinstance(reply, tuple) else reply

    monkeypatch.setitem(app.BACKENDS, "essai",
                        Backend("essai", {"url": "http://backend.invalid"}))
    monkeypatch.setattr(app, "PROXY_API_KEYS", [])
    monkeypatch.setattr(app.anthropic_api, "ENABLED", True)
    monkeypatch.setattr(app.responses_api, "ENABLED", True)
    monkeypatch.setattr(app.tools, "Hosted", lambda: env.hosted)
    monkeypatch.setattr(app, "send_upstream", send_upstream)
    monkeypatch.setattr(app.stats, "record", lambda *a: env.lines.append(a))
    env.client = TestClient(app.app)
    return env
