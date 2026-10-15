"""La mémoire persistante : le magasin (memories.py), l'outil `memory`
(tools/memory.py), son cloisonnement par clé par /mcp et /v1/tools, et
les routes de la section Mémoire de /ui. Sur une base temporaire."""

import asyncio

import pytest

from llm_proxy import mcp_server, memories, tools
from llm_proxy.tools import memory


@pytest.fixture(autouse=True)
def base(monkeypatch, tmp_path):
    monkeypatch.setattr(memories, "DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setattr(memories, "MAX_FILES", 500)
    monkeypatch.setattr(memories, "MAX_FILE_CHARS", 100_000)
    monkeypatch.setattr(memories, "MAX_TOTAL_CHARS", 2_000_000)


def mem(owner="a", **args) -> str:
    """Le texte que lit le modèle : celui du Result, ou de l'échec prévu."""
    try:
        return asyncio.run(memory.TOOL.run(args, tools.Call(client=owner))).text
    except tools.ToolError as exc:
        return tools.failure(exc.code, exc.message).text


def code(owner="a", **args) -> str | None:
    try:
        asyncio.run(memory.TOOL.run(args, tools.Call(client=owner)))
    except tools.ToolError as exc:
        return exc.code
    return None


# ── magasin ─────────────────────────────────────────────────────────────

def test_chemins_normalises_et_refuses():
    n = memories.normalize
    assert n("/memories/a//b.md") == "/memories/a/b.md"
    assert n("notes.md") == "/memories/notes.md"
    assert n("/memories", directory=True) == "/memories"
    # Tout «..» est refusé, même s'il resterait sous /memories.
    for bad in ("/etc/passwd", "/memories/../x", "../x", "/memories/a/../b.md", "", "/memories/a\nb",
                "/memoriesx/a", "/memories/" + "x" * 300):
        with pytest.raises(memories.MemoryError_):
            n(bad)
    with pytest.raises(memories.MemoryError_):
        n("/memories")                  # un répertoire, pas un fichier


def test_bornes_par_client(monkeypatch):
    monkeypatch.setattr(memories, "MAX_FILES", 2)
    monkeypatch.setattr(memories, "MAX_FILE_CHARS", 10)
    monkeypatch.setattr(memories, "MAX_TOTAL_CHARS", 15)
    memories.write("a", "/memories/1", "x" * 10)
    with pytest.raises(memories.MemoryError_, match="at most 10 characters"):
        memories.write("a", "/memories/2", "x" * 11)
    with pytest.raises(memories.MemoryError_, match="15 characters in all"):
        memories.write("a", "/memories/2", "x" * 6)
    memories.write("a", "/memories/2", "x" * 5)
    with pytest.raises(memories.MemoryError_, match="at most 2 files"):
        memories.write("a", "/memories/3", "")
    memories.write("a", "/memories/1", "y")        # remplacer : permis
    memories.write("b", "/memories/1", "x" * 10)   # un autre client : ses bornes
    # Un fichier ne devient pas répertoire, ni l'inverse.
    with pytest.raises(memories.MemoryError_, match="is a file"):
        memories.write("b", "/memories/1/x", "")


# ── l'outil ─────────────────────────────────────────────────────────────

def test_commandes_de_l_outil():
    assert "empty" in mem(command="view", path="/memories")
    assert mem(command="create", path="/memories/p.md", file_text="un\ndeux\ntrois") \
        == "File created: /memories/p.md"
    assert mem(command="view", path="/memories/p.md") == (
        "/memories/p.md:\n     1\tun\n     2\tdeux\n     3\ttrois")
    assert mem(command="view", path="/memories/p.md", view_range=[2, -1]).startswith(
        "/memories/p.md:\n     2\tdeux\n     3\ttrois")
    assert mem(command="str_replace", path="/memories/p.md", old_str="deux",
               new_str="2") == "File edited: /memories/p.md"
    assert "appears 2 times" in mem(command="str_replace", path="/memories/p.md",
                                    old_str="\n", new_str="")
    assert "not found" in mem(command="str_replace", path="/memories/p.md", old_str="zz")
    mem(command="insert", path="/memories/p.md", insert_line=0, insert_text="zéro")
    assert memories.read("a", "/memories/p.md") == "zéro\nun\n2\ntrois"
    assert "outside" in mem(command="insert", path="/memories/p.md", insert_line=9,
                            insert_text="x")
    mem(command="create", path="/memories/d/x.md", file_text="x")
    listing = mem(command="view", path="/memories")
    assert "2 file(s)" in listing and "/memories/d/x.md" in listing
    assert mem(command="rename", old_path="/memories/d", new_path="/memories/e") \
        == "Renamed: /memories/d → /memories/e"
    assert "already exists" in mem(command="rename", old_path="/memories/p.md",
                                   new_path="/memories/e/x.md")
    assert mem(command="delete", path="/memories/e") == "Deleted: /memories/e"
    assert "does not exist" in mem(command="view", path="/memories/e")
    assert "cannot be deleted" in mem(command="delete", path="/memories")
    assert mem(command="view", path="/etc/passwd").startswith("Error: paths must be under")
    assert mem(command="rm").startswith("Error: unknown command")
    # Les codes du contrat : argument, rien à ce chemin, bornes.
    assert code(command="view", path="/etc") == "invalid_input"
    assert code(command="delete", path="/memories/rien") == "not_accessible"
    assert code(command="create", path="/memories/x", file_text="x" * 200_000) == "failed"


def test_cloisonnement_entre_clients():
    mem("a", command="create", path="/memories/secret.md", file_text="à a")
    assert "does not exist" in mem("b", command="view", path="/memories/secret.md")
    assert "empty" in mem("b", command="view", path="/memories")
    assert "does not exist" in mem("b", command="delete", path="/memories/secret.md")
    assert memories.read("a", "/memories/secret.md") == "à a"


# ── par le proxy : /mcp, /v1/tools, /ui ─────────────────────────────────

@pytest.fixture
def proxy(monkeypatch):
    from fastapi.testclient import TestClient
    from llm_proxy import app as A

    monkeypatch.setattr(A, "PROXY_API_KEYS", frozenset({"sk-a", "sk-b"}))
    monkeypatch.setattr(mcp_server, "ENABLED", True)
    monkeypatch.setattr(memory, "ENABLED", True)
    monkeypatch.setattr(tools, "enabled", lambda: [memory.TOOL])
    return TestClient(A.app)


def bearer(key):
    return {"Authorization": f"Bearer {key}"}


def mcp_call(client, key, **args):
    r = client.post("/mcp", headers=bearer(key), json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "memory", "arguments": args}})
    return r.json()["result"]["content"][0]["text"]


def test_mcp_et_appel_direct_cloisonnes_par_cle(proxy):
    tools = proxy.post("/mcp", headers=bearer("sk-a"), json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()["result"]["tools"]
    assert [t["name"] for t in tools] == ["memory"]
    assert mcp_call(proxy, "sk-a", command="create", path="/memories/n.md",
                    file_text="note de a") == "File created: /memories/n.md"
    assert "does not exist" in mcp_call(proxy, "sk-b", command="view", path="/memories/n.md")
    # L'appel direct voit la même mémoire, pour la même clé.
    r = proxy.post("/v1/tools/memory", headers=bearer("sk-a"),
                   json={"command": "view", "path": "/memories/n.md"})
    assert "note de a" in r.json()["result"]
    r = proxy.post("/v1/tools/memory", headers=bearer("sk-b"),
                   json={"command": "view", "path": "/memories/n.md"})
    assert r.json()["is_error"] is True


def test_routes_ui_de_la_cle_connectee(proxy):
    mcp_call(proxy, "sk-a", command="create", path="/memories/n.md", file_text="un")
    # Le cookie de la page porte la clé : posé par /ui?key=…
    proxy.get("/ui?key=sk-a")
    j = proxy.get("/ui/memory").json()
    assert [f["path"] for f in j["data"]] == ["/memories/n.md"] and j["usage"]["files"] == 1
    assert proxy.get("/ui/memory/file", params={"path": "/memories/n.md"}).json()[
        "content"] == "un"
    r = proxy.put("/ui/memory/file", json={"path": "/memories/n.md", "content": "deux"})
    assert r.json() == {"path": "/memories/n.md", "created": False}
    r = proxy.post("/ui/memory/rename", json={"old_path": "/memories/n.md",
                                              "new_path": "/memories/m.md"})
    assert r.json() == {"renamed": 1, "path": "/memories/m.md"}
    assert "deux" in mcp_call(proxy, "sk-a", command="view", path="/memories/m.md")
    assert proxy.put("/ui/memory/file", json={"path": "/etc/x", "content": ""}).status_code == 400
    assert proxy.put("/ui/memory/file", content=b"pas du json").status_code == 400
    assert proxy.delete("/ui/memory/file", params={"path": "/memories/m.md"}).json() == {"deleted": 1}
    assert proxy.delete("/ui/memory/file", params={"path": "/memories/m.md"}).status_code == 404
    # Une autre clé ne voit rien de celle-ci.
    mcp_call(proxy, "sk-a", command="create", path="/memories/n.md", file_text="un")
    proxy.cookies.clear()
    proxy.get("/ui?key=sk-b")
    assert proxy.get("/ui/memory").json()["data"] == []
    assert proxy.get("/ui/memory/file", params={"path": "/memories/n.md"}).status_code == 404


def test_ui_sans_cle_et_memoire_desactivee(proxy, monkeypatch):
    proxy.cookies.clear()
    assert proxy.get("/ui/memory").status_code == 401
    monkeypatch.setattr(memory, "ENABLED", False)
    assert proxy.get("/ui/memory", headers=bearer("sk-a")).status_code == 404
