"""L'outil `browser` et son lien avec l'extension (browser_link.py) : la
présentation par clé, le relais d'un appel au navigateur DU client, les
refus (clé invalide, pas d'extension, échec rendu par l'extension), le
remplacement d'une connexion, la capture en fichier, l'extension en zip."""

import base64
import io
import json
import threading
import zipfile

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from fakes import bearer
from llm_proxy import app, auth, browser_link, tools
from llm_proxy.tools import browser

KEYS = ["sk-a", "sk-b"]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth, "PROXY_API_KEYS", KEYS)
    monkeypatch.setattr(browser, "ENABLED", True)
    monkeypatch.setattr(browser_link, "LINKS", {})
    with TestClient(app.app) as c:
        yield c


def hello(ws, key="sk-a"):
    ws.send_text(json.dumps({"type": "hello", "key": key, "version": "0.1.0",
                             "agent": "Chrome/141"}))
    return json.loads(ws.receive_text())


def run_tool(client, key, args, out):
    """POST /v1/tools/browser dans un fil : il attend le navigateur."""
    def go():
        out["r"] = client.post("/v1/tools/browser", json=args, headers=bearer(key)).json()
    t = threading.Thread(target=go)
    t.start()
    return t


def test_presentation_et_relais_d_un_appel(client):
    with client.websocket_connect(browser_link.PATH) as ws:
        assert hello(ws) == {"type": "welcome", "protocol": 1}
        ws.send_text('{"type": "ping"}')
        assert json.loads(ws.receive_text()) == {"type": "pong"}
        out = {}
        t = run_tool(client, "sk-a", {"action": "open", "url": "https://exemple.fr"}, out)
        call = json.loads(ws.receive_text())
        assert call["type"] == "call" and call["op"] == "open"
        assert call["args"] == {"action": "open", "url": "https://exemple.fr"}
        # Qui appelle : de quoi faire confirmer la session par l'utilisateur.
        who = call["who"]
        assert who["endpoint"] == "/v1/tools" and who["key"].startswith("clé ")
        assert who["agent"] == "testclient" and len(who["session"]) == 24
        ws.send_text(json.dumps({"type": "result", "id": call["id"], "ok": True,
                                 "text": "[tab 7] Exemple\n[1] lien « Accueil »",
                                 "url": "https://exemple.fr/", "title": "Exemple"}))
        t.join(5)
        assert out["r"]["is_error"] is False
        assert out["r"]["result"].startswith("[tab 7] Exemple")
        assert out["r"]["meta"] == {"url": "https://exemple.fr/", "title": "Exemple"}


def test_un_client_n_atteint_que_son_navigateur(client):
    with client.websocket_connect(browser_link.PATH) as ws:
        hello(ws, "sk-a")
        r = client.post("/v1/tools/browser", json={"action": "tabs"}, headers=bearer("sk-b")).json()
        assert r["is_error"] and r["error"] == "unavailable"
        assert "no browser is connected" in r["result"]


def test_cle_refusee(client):
    with client.websocket_connect(browser_link.PATH) as ws:
        assert hello(ws, "sk-nope")["type"] == "denied"
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
        assert exc.value.code == browser_link.CLOSE_DENIED
    assert browser_link.LINKS == {}


def test_presentation_attendue_en_premier(client):
    with client.websocket_connect(browser_link.PATH) as ws:
        ws.send_text('{"type": "ping"}')
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
        assert exc.value.code == browser_link.CLOSE_BAD_HELLO


def test_echec_rendu_par_l_extension(client):
    with client.websocket_connect(browser_link.PATH) as ws:
        hello(ws)
        out = {}
        t = run_tool(client, "sk-a", {"action": "read"}, out)
        call = json.loads(ws.receive_text())
        ws.send_text(json.dumps({"type": "result", "id": call["id"], "ok": False,
                                 "code": "not_allowed",
                                 "error": "the user refused exemple.fr"}))
        t.join(5)
        assert out["r"]["error"] == "not_allowed"
        assert out["r"]["result"] == "Error: the user refused exemple.fr."
        # Un code inconnu est ramené à `failed`.
        t = run_tool(client, "sk-a", {"action": "read"}, out)
        call = json.loads(ws.receive_text())
        ws.send_text(json.dumps({"type": "result", "id": call["id"], "ok": False,
                                 "code": "bizarre", "error": "oups"}))
        t.join(5)
        assert out["r"]["error"] == "failed"


def test_action_inconnue(client):
    r = client.post("/v1/tools/browser", json={"action": "hack"}, headers=bearer("sk-a")).json()
    assert r["error"] == "invalid_input"


def test_capture_rendue_en_fichier(client, monkeypatch):
    from llm_proxy import files
    monkeypatch.setattr(files, "PUBLIC_URL", "https://proxy.exemple.fr")
    jpeg = b"\xff\xd8\xff\xe0fausse-image"
    with client.websocket_connect(browser_link.PATH) as ws:
        hello(ws)
        out = {}
        t = run_tool(client, "sk-a", {"action": "screenshot"}, out)
        call = json.loads(ws.receive_text())
        ws.send_text(json.dumps({"type": "result", "id": call["id"], "ok": True,
                                 "text": "Screenshot of tab 7.",
                                 "image": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}))
        t.join(5)
        f = out["r"]["files"][0]
        assert (f["name"], f["media_type"], f["size"]) == ("capture.jpg", "image/jpeg", len(jpeg))


def test_une_seconde_connexion_remplace_la_premiere(client):
    with client.websocket_connect(browser_link.PATH) as first:
        hello(first)
        with client.websocket_connect(browser_link.PATH) as second:
            hello(second)
            with pytest.raises(WebSocketDisconnect) as exc:
                first.receive_text()
            assert exc.value.code == browser_link.CLOSE_REPLACED
            assert browser_link.LINKS[tools.owner("sk-a")].ws is not None
            state = client.get("/ui/browser/link", headers=bearer("sk-a")).json()
            assert state["connected"] is True and state["version"] == "0.1.0"
    assert client.get("/ui/browser/link", headers=bearer("sk-a")).json() == {"connected": False}


def test_deconnexion_pendant_un_appel(client):
    out = {}
    with client.websocket_connect(browser_link.PATH) as ws:
        hello(ws)
        t = run_tool(client, "sk-a", {"action": "tabs"}, out)
        json.loads(ws.receive_text())
    t.join(5)
    assert out["r"]["error"] == "unavailable"
    assert "disconnected" in out["r"]["result"]


def test_outil_conforme_et_presente():
    assert tools.faults(browser.TOOL) == []
    spec = browser.TOOL.spec(frozenset({"browser"}))["function"]
    assert spec["parameters"]["properties"]["action"]["enum"] == list(browser.ACTIONS)


def test_extension_en_zip(client):
    r = client.get("/ui/browser/extension.zip", headers=bearer("sk-a"))
    assert r.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "llm-proxy-browser/manifest.json" in names
    manifest = json.loads(zipfile.ZipFile(io.BytesIO(r.content)).read("llm-proxy-browser/manifest.json"))
    assert manifest["manifest_version"] == 3


def test_session_propre_a_la_cle_et_au_client(client):
    """La session d'un appel sans conversation : la clé et le logiciel
    client. Une autre clé du même compte, ou un autre logiciel, en est une
    autre ; la même clé, avec le même logiciel, la même."""
    with client.websocket_connect(browser_link.PATH) as ws:
        hello(ws)
        seen = []
        for key, agent in (("sk-a", "codex/1"), ("sk-a", "codex/1"), ("sk-a", "omp/2")):
            out = {}
            def go():
                out["r"] = client.post("/v1/tools/browser", json={"action": "tabs"},
                                       headers={**bearer(key), "user-agent": agent}).json()
            t = threading.Thread(target=go)
            t.start()
            call = json.loads(ws.receive_text())
            seen.append((call["who"]["session"], call["who"]["agent"]))
            ws.send_text(json.dumps({"type": "result", "id": call["id"], "ok": True, "text": "-"}))
            t.join(5)
        assert seen[0] == seen[1] and seen[0][0] != seen[2][0]
        assert seen[2][1] == "omp/2"
