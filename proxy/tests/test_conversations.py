"""L'historique du chat de la page : le magasin (conversations.py) et ses
routes sous /ui, cloisonnées par compte. Sur une base temporaire."""

import base64

import pytest

from fakes import everywhere

from llm_proxy.access import auth
from llm_proxy.storage import conversations
from llm_proxy.ui import dashboard

PNG = (b"\x89PNG\r\n\x1a\n" + b"\0" * 32)
PNG_URL = "data:image/png;base64," + base64.b64encode(PNG).decode()


@pytest.fixture(autouse=True)
def base(monkeypatch, tmp_path):
    monkeypatch.setattr(conversations, "DB_PATH", str(tmp_path / "conversations.db"))
    monkeypatch.setattr(conversations, "MAX_TOTAL_BYTES", 2_000_000_000)
    everywhere(monkeypatch, dashboard, "UI_CHAT", True)


@pytest.fixture
def proxy(monkeypatch):
    from fastapi.testclient import TestClient
    from llm_proxy import app as A

    monkeypatch.setattr(auth, "PROXY_API_KEYS", frozenset({"sk-a", "sk-b"}))
    return TestClient(A.app)


def conv(cid="c1", **extra):
    return {"id": cid, "title": "Essai", "created": 1_700_000_000_000,
            "updated": 1_700_000_000_000, "messages": [], **extra}


def test_magasin_bornes_et_incognito(monkeypatch):
    conversations.save("a", "c1", conv())
    assert [c["id"] for c in conversations.list_all("a")] == ["c1"]
    assert conversations.list_all("b") == []
    with pytest.raises(conversations.HistoryError, match="incognito"):
        conversations.save("a", "c2", conv("c2", incognito=True))
    with pytest.raises(conversations.HistoryError):
        conversations.save("a", "c2", conv("autre"))          # id ≠ adresse
    with pytest.raises(conversations.HistoryError):
        conversations.save("a", "../x", conv("../x"))
    monkeypatch.setattr(conversations, "MAX_TOTAL_BYTES", 200)
    with pytest.raises(conversations.HistoryError, match="plein") as exc:
        conversations.save("a", "c3", conv("c3", system="x" * 300))
    assert exc.value.status == 413
    # Seules les images matricielles sont gardées.
    with pytest.raises(conversations.HistoryError, match="images"):
        conversations.add_asset("a", "c1", "x.svg", b"<svg onload=alert(1)>")


def test_images_orphelines_effacees(monkeypatch):
    kept = conversations.add_asset("a", "c1", "garde.png", PNG)
    gone = conversations.add_asset("a", "c1", "perdue.png", PNG)
    doc = conv(messages=[{"role": "user", "attachments": [{"data": kept["url"]}]}])
    # Pas encore citée, mais récente : gardée.
    conversations.save("a", "c1", doc)
    assert conversations.asset("a", gone["id"]) is not None
    monkeypatch.setattr(conversations, "ORPHAN_GRACE", -1)
    conversations.save("a", "c1", doc)
    assert conversations.asset("a", gone["id"]) is None
    assert conversations.asset("a", kept["id"])[2] == PNG
    assert conversations.delete("a", "c1") == 1
    assert conversations.asset("a", kept["id"]) is None
    # Une image d'une conversation jamais enregistrée : à la lecture.
    lost = conversations.add_asset("a", "jamais", "x.png", PNG)
    conversations.list_all("a")
    assert conversations.asset("a", lost["id"]) is None


def test_routes_du_compte_connecte(proxy):
    proxy.post("/ui/login", json={"key": "sk-a"})
    r = proxy.post("/ui/assets", json={"conversation": "c1", "name": "i.png", "data": PNG_URL})
    assert r.status_code == 200, r.text
    url = r.json()["url"]
    assert url.startswith("/ui/assets/") and r.json()["type"] == "image/png"
    doc = conv(messages=[{"role": "assistant", "attachments": [{"kind": "image", "data": url}]}])
    assert proxy.put("/ui/conversations/c1", json=doc).json() == {"id": "c1", "saved": True}
    j = proxy.get("/ui/conversations").json()
    assert j["data"] == [doc] and j["usage"]["conversations"] == 1
    got = proxy.get(url)
    assert got.content == PNG and got.headers["content-type"] == "image/png"
    assert got.headers["x-content-type-options"] == "nosniff"
    # Les écritures exigent du JSON ; une image doit en être une.
    assert proxy.put("/ui/conversations/c1", content=b"{}",
                     headers={"content-type": "text/plain"}).status_code in (400, 415)
    bad = "data:text/html;base64," + base64.b64encode(b"<script>").decode()
    assert proxy.post("/ui/assets", json={"conversation": "c1", "data": bad}).status_code == 400
    # Un autre compte ne voit ni la conversation ni l'image.
    proxy.cookies.clear()
    proxy.post("/ui/login", json={"key": "sk-b"})
    assert proxy.get("/ui/conversations").json()["data"] == []
    assert proxy.get(url).status_code == 404
    assert proxy.delete("/ui/conversations/c1").json()["deleted"] is False
    proxy.cookies.clear()
    proxy.post("/ui/login", json={"key": "sk-a"})
    assert proxy.delete("/ui/conversations/c1").json()["deleted"] is True
    assert proxy.get(url).status_code == 404


def test_chat_ferme(proxy, monkeypatch):
    everywhere(monkeypatch, dashboard, "UI_CHAT", False)
    proxy.post("/ui/login", json={"key": "sk-a"})
    assert proxy.get("/ui/conversations").status_code == 404
