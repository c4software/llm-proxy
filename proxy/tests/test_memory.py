"""La mémoire persistante : le magasin (memories.py), l'outil `memory`
(tools/memory.py), son cloisonnement par clé par /mcp et /v1/tools, et
les routes de la section Mémoire de /ui. Sur une base temporaire."""

import time

import pytest

from fakes import bearer, go, rendu, rpc
from llm_proxy.access import auth
from llm_proxy.exposure import mcp_server
from llm_proxy.storage import memories
from llm_proxy import tools
from llm_proxy.tools import memory


@pytest.fixture(autouse=True)
def base(monkeypatch, tmp_path):
    monkeypatch.setattr(memories, "DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setattr(memories, "MAX_FILES", 500)
    monkeypatch.setattr(memories, "MAX_FILE_CHARS", 100_000)
    monkeypatch.setattr(memories, "MAX_TOTAL_CHARS", 2_000_000)


def mem(owner="a", **args) -> str:
    """Le texte que lit le modèle : celui du Result, ou de l'échec prévu."""
    return rendu(memory.TOOL.run(args, tools.Call(client=owner))).text


def code(owner="a", **args) -> str | None:
    try:
        go(memory.TOOL.run(args, tools.Call(client=owner)))
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


def test_duree_de_vie(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    # Sans durée : persistant, rien de plus dans la réponse ni dans view.
    assert mem(command="create", path="/memories/p.md", file_text="p") \
        == "File created: /memories/p.md"
    out = mem(command="create", path="/memories/t.md", file_text="t",
              expires_in_days=2)
    assert out.startswith("File created: /memories/t.md (expires ")
    assert memories.stat("a", "/memories/t.md")["expires"] == now[0] + 2 * 86400
    listing = mem(command="view", path="/memories")
    assert "persistent" in listing
    assert [l.endswith(")") for l in listing.split("\n")[1:]] == [False, True]
    assert mem(command="view", path="/memories/t.md").startswith("/memories/t.md (expires ")
    # Modifier sans durée la garde ; 0 rend persistant ; refus hors bornes.
    mem(command="str_replace", path="/memories/t.md", old_str="t", new_str="u")
    assert memories.stat("a", "/memories/t.md")["expires"] == now[0] + 2 * 86400
    mem(command="rename", old_path="/memories/t.md", new_path="/memories/u.md")
    assert memories.stat("a", "/memories/u.md")["expires"] == now[0] + 2 * 86400
    mem(command="insert", path="/memories/p.md", insert_line=0, insert_text="x",
        expires_in_days=0.5)
    assert memories.stat("a", "/memories/p.md")["expires"] == now[0] + 43200
    mem(command="create", path="/memories/p.md", file_text="p", expires_in_days=0)
    assert memories.stat("a", "/memories/p.md")["expires"] is None
    for bad in (-1, "7", True, memory.MAX_DAYS + 1):
        assert code(command="create", path="/memories/x.md", file_text="x",
                    expires_in_days=bad) == "invalid_input"
    # Échu : invisible, et effacé (de tous les clients).
    memories.write("b", "/memories/b.md", "b", now[0] + 10)
    now[0] += 2 * 86400
    assert memories.read("a", "/memories/u.md") is None
    assert "does not exist" in mem(command="view", path="/memories/u.md")
    assert [f["path"] for f in memories.list_files("a")] == ["/memories/p.md"]
    assert memories.usage("b")["files"] == 0


def test_base_d_avant_l_echeance():
    """Une memory.db sans la colonne `expires` : ajoutée, tout persistant."""
    import sqlite3
    conn = sqlite3.connect(memories.DB_PATH)
    conn.execute("CREATE TABLE files (owner TEXT NOT NULL, path TEXT NOT NULL, "
                 "content TEXT NOT NULL, created REAL NOT NULL, updated REAL "
                 "NOT NULL, PRIMARY KEY (owner, path))")
    conn.execute("INSERT INTO files VALUES ('a', '/memories/v.md', 'v', 1, 1)")
    conn.commit()
    conn.close()
    assert memories.stat("a", "/memories/v.md")["expires"] is None
    memories.write("a", "/memories/w.md", "w", time.time() + 60)
    assert [f["expires"] is None for f in memories.list_files("a")] == [True, False]


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

    monkeypatch.setattr(auth, "PROXY_API_KEYS", frozenset({"sk-a", "sk-b"}))
    monkeypatch.setattr(mcp_server, "ENABLED", True)
    monkeypatch.setattr(memory, "ENABLED", True)
    monkeypatch.setattr(tools, "enabled", lambda: [memory.TOOL])
    return TestClient(A.app)


def mcp_call(client, key, **args):
    r = rpc(client, "tools/call", {"name": "memory", "arguments": args},
            headers=bearer(key))
    return r.json()["result"]["content"][0]["text"]


def test_mcp_et_appel_direct_cloisonnes_par_cle(proxy):
    tools = rpc(proxy, "tools/list", headers=bearer("sk-a")).json()[
        "result"]["tools"]
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
    # La session de la page : ouverte par la clé, à l'entrée.
    proxy.post("/ui/login", json={"key": "sk-a"})
    j = proxy.get("/ui/memory").json()
    assert [f["path"] for f in j["data"]] == ["/memories/n.md"] and j["usage"]["files"] == 1
    assert proxy.get("/ui/memory/file", params={"path": "/memories/n.md"}).json()[
        "content"] == "un"
    r = proxy.put("/ui/memory/file", json={"path": "/memories/n.md", "content": "deux"})
    assert r.json() == {"path": "/memories/n.md", "created": False, "expires": None}
    # L'échéance : lue avec le fichier, posée par PUT, gardée sans `expires`.
    soon = time.time() + 3600
    r = proxy.put("/ui/memory/file", json={"path": "/memories/n.md", "content": "deux",
                                           "expires": soon})
    assert r.json()["expires"] == soon
    assert proxy.get("/ui/memory").json()["data"][0]["expires"] == soon
    proxy.put("/ui/memory/file", json={"path": "/memories/n.md", "content": "deux"})
    assert proxy.get("/ui/memory/file", params={"path": "/memories/n.md"}).json()[
        "expires"] == soon
    assert proxy.put("/ui/memory/file", json={"path": "/memories/n.md", "content": "deux",
                                              "expires": time.time() - 1}).status_code == 400
    proxy.put("/ui/memory/file", json={"path": "/memories/n.md", "content": "deux",
                                       "expires": None})
    assert proxy.get("/ui/memory/file", params={"path": "/memories/n.md"}).json()[
        "expires"] is None
    r = proxy.post("/ui/memory/rename", json={"old_path": "/memories/n.md",
                                              "new_path": "/memories/m.md"})
    assert r.json() == {"renamed": 1, "path": "/memories/m.md"}
    assert "deux" in mcp_call(proxy, "sk-a", command="view", path="/memories/m.md")
    assert proxy.put("/ui/memory/file", json={"path": "/etc/x", "content": ""}).status_code == 400
    assert proxy.put("/ui/memory/file", content=b"pas du json",
                     headers={"content-type": "application/json"}).status_code == 400
    # Un formulaire d'une page tierce (text/plain) : refusé avant d'être lu.
    assert proxy.put("/ui/memory/file", content=b'{"path": "/memories/x.md", "content": ""}',
                     headers={"content-type": "text/plain"}).status_code == 415
    assert proxy.post("/ui/memory/rename", content=b'{"old_path": "/memories/m.md", "new_path": "/memories/z.md"}',
                      headers={"content-type": "text/plain"}).status_code == 415
    assert proxy.delete("/ui/memory/file", params={"path": "/memories/m.md"}).json() == {"deleted": 1}
    assert proxy.delete("/ui/memory/file", params={"path": "/memories/m.md"}).status_code == 404
    # Une autre clé ne voit rien de celle-ci.
    mcp_call(proxy, "sk-a", command="create", path="/memories/n.md", file_text="un")
    proxy.cookies.clear()
    proxy.post("/ui/login", json={"key": "sk-b"})
    assert proxy.get("/ui/memory").json()["data"] == []
    assert proxy.get("/ui/memory/file", params={"path": "/memories/n.md"}).status_code == 404


def test_ui_sans_cle_et_memoire_desactivee(proxy, monkeypatch):
    proxy.cookies.clear()
    assert proxy.get("/ui/memory").status_code == 401
    monkeypatch.setattr(memory, "ENABLED", False)
    assert proxy.get("/ui/memory", headers=bearer("sk-a")).status_code == 404
