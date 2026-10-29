"""Les voix clonées du chat de la page : le magasin (voices.py) et ses
routes sous /ui, cloisonnées par compte. Sur une base temporaire."""

import base64

import pytest

from fakes import everywhere

from llm_proxy.access import auth
from llm_proxy.storage import conversations, voices
from llm_proxy.ui import dashboard

WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\0" * 28
WAV_URL = "data:audio/wav;base64," + base64.b64encode(WAV).decode()
VID = "0b6f2a4e-1c1d-4a57-9f0e-3c2b8d6e7f10"


@pytest.fixture(autouse=True)
def base(monkeypatch, tmp_path):
    monkeypatch.setattr(conversations, "DB_PATH", str(tmp_path / "conversations.db"))
    everywhere(monkeypatch, dashboard, "UI_CHAT", True)


@pytest.fixture
def proxy(monkeypatch):
    from fastapi.testclient import TestClient
    from llm_proxy import app as A

    monkeypatch.setattr(auth, "PROXY_API_KEYS", frozenset({"sk-a", "sk-b"}))
    return TestClient(A.app)


def test_magasin(monkeypatch):
    got = voices.save("a", VID, {"name": " Moi ", "text": "Bonjour.", "duration": 4.2,
                                 "source": "micro", "created": 1_700_000_000_000}, WAV)
    assert got == {"id": VID, "name": "Moi", "text": "Bonjour.", "duration": 4.2,
                   "source": "micro", "created": 1_700_000_000_000, "size": len(WAV)}
    assert voices.list_all("a") == [got] and voices.list_all("b") == []
    assert voices.wav("a", VID) == WAV and voices.wav("b", VID) is None
    # Remplacée sous le même identifiant : sa date de création reste.
    again = voices.save("a", VID, {"name": "Moi", "text": "Salut.", "created": 0}, WAV)
    assert again["text"] == "Salut." and again["created"] == 1_700_000_000_000
    assert voices.rename("a", VID, "Autre")["name"] == "Autre"
    assert voices.rename("b", VID, "Volée") is None
    with pytest.raises(conversations.HistoryError, match="WAV"):
        voices.save("a", "v2", {"text": "x"}, b"<svg>")
    with pytest.raises(conversations.HistoryError, match="transcription"):
        voices.save("a", "v2", {"text": " "}, WAV)
    with pytest.raises(conversations.HistoryError):
        voices.save("a", "../x", {"text": "x"}, WAV)
    monkeypatch.setattr(voices, "MAX_VOICES", 1)
    with pytest.raises(conversations.HistoryError, match="au plus") as exc:
        voices.save("a", "v2", {"text": "x"}, WAV)
    assert exc.value.status == 413
    assert voices.delete("b", VID) == 0 and voices.delete("a", VID) == 1
    assert voices.list_all("a") == []


def test_routes_du_compte_connecte(proxy):
    proxy.post("/ui/login", json={"key": "sk-a"})
    r = proxy.put(f"/ui/voices/{VID}", json={"name": "Moi", "text": "Bonjour.",
                                             "duration": 3, "source": "micro", "data": WAV_URL})
    assert r.status_code == 200, r.text
    assert [v["id"] for v in proxy.get("/ui/voices").json()["data"]] == [VID]
    got = proxy.get(f"/ui/voices/{VID}/audio")
    assert got.content == WAV and got.headers["content-type"] == "audio/wav"
    assert proxy.patch(f"/ui/voices/{VID}", json={"name": "Autre"}).json()["name"] == "Autre"
    # Les écritures exigent du JSON ; la référence, un WAV.
    assert proxy.put(f"/ui/voices/{VID}", content=b"{}",
                     headers={"content-type": "text/plain"}).status_code in (400, 415)
    assert proxy.put("/ui/voices/v2", json={"text": "x"}).status_code == 400
    # Un autre compte ne la voit pas.
    proxy.cookies.clear()
    proxy.post("/ui/login", json={"key": "sk-b"})
    assert proxy.get("/ui/voices").json()["data"] == []
    assert proxy.get(f"/ui/voices/{VID}/audio").status_code == 404
    assert proxy.patch(f"/ui/voices/{VID}", json={"name": "x"}).status_code == 404
    assert proxy.delete(f"/ui/voices/{VID}").json()["deleted"] is False
    proxy.cookies.clear()
    proxy.post("/ui/login", json={"key": "sk-a"})
    assert proxy.delete(f"/ui/voices/{VID}").json()["deleted"] is True
    assert proxy.get(f"/ui/voices/{VID}/audio").status_code == 404


def test_chat_ferme(proxy, monkeypatch):
    everywhere(monkeypatch, dashboard, "UI_CHAT", False)
    proxy.post("/ui/login", json={"key": "sk-a"})
    assert proxy.get("/ui/voices").status_code == 404
