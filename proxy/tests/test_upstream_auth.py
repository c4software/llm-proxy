"""Un 401 du backend (sa clé manque ou est refusée) n'est jamais relayé tel
quel : le client — la page web d'abord — le prendrait pour SA clé refusée
et se déconnecterait. Le proxy rend un 502 qui nomme le backend."""

import httpx2
from fastapi.testclient import TestClient

from fakes import fake_backend


def test_upstream_401_becomes_502(monkeypatch):
    from llm_proxy import app
    from llm_proxy.storage import stats

    monkeypatch.setattr(stats, "record", lambda *a: None)
    fake_backend(monkeypatch, "cle", {"url": "http://cle.invalid"},
                 lambda req: httpx2.Response(401, json={
                     "error": "Header of type `authorization` was missing"}))
    client = TestClient(app.app)
    for stream in (False, True):
        r = client.post("/v1/chat/completions", json={
            "model": "cle/m", "stream": stream,
            "messages": [{"role": "user", "content": "bonjour"}]})
        assert r.status_code == 502, r.text
        err = r.json()["error"]
        assert err["type"] == "upstream_auth_error"
        assert "cle" in err["message"]
