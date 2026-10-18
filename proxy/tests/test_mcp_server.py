"""Le serveur MCP du proxy (mcp_server.py, POST /mcp) : le protocole tel
qu'un client le joue — initialize, tools/list, tools/call —, sans réseau.
Les outils actifs sont fixés par le test ; la recherche est un faux.
(tools/mcp.py, l'inverse — le proxy CLIENT de serveurs MCP —, a ses tests
dans test_mcp.py.)"""

import pytest

from fakes import rpc
from llm_proxy.access import auth
from llm_proxy.storage import files
from llm_proxy.exposure import mcp_server
from llm_proxy import tools
from llm_proxy.tools import web_fetch, web_search


@pytest.fixture
def mcp(monkeypatch):
    """(client, arguments reçus par l'outil) : MCP actif, la recherche
    seule active et remplacée par un faux, proxy sans clé."""
    from fastapi.testclient import TestClient
    from llm_proxy import app as A

    monkeypatch.setattr(auth, "PROXY_API_KEYS", frozenset())
    monkeypatch.setattr(mcp_server, "ENABLED", True)
    monkeypatch.setattr(mcp_server, "ALLOWED_ORIGINS", [])
    active = [web_search.TOOL]
    monkeypatch.setattr(tools, "enabled", lambda: list(active))
    seen = []

    async def run(self, args, call):
        seen.append((args, call))
        if args.get("query") == "panne":
            return tools.failure("unavailable", "moteur éteint.")
        return tools.Result("[1] Titre\n    https://e.org\n    extrait")
    monkeypatch.setattr(web_search.WebSearch, "run", run)
    return TestClient(A.app), seen, active


def test_initialize_negocie_la_version_et_annonce_les_outils(mcp):
    client, _, _ = mcp
    r = rpc(client, "initialize", {"protocolVersion": "2025-06-18",
                                   "capabilities": {},
                                   "clientInfo": {"name": "t", "version": "0"}})
    assert r.status_code == 200
    res = r.json()["result"]
    assert res["protocolVersion"] == "2025-06-18"
    assert res["capabilities"] == {"tools": {"listChanged": False}}
    assert res["serverInfo"]["name"] == "llm-proxy"
    assert rpc(client, "initialize", {"protocolVersion": "1999-01-01"}).json()[
        "result"]["protocolVersion"] == mcp_server.VERSIONS[0]
    r = client.post("/mcp", json={"jsonrpc": "2.0",
                                  "method": "notifications/initialized"})
    assert r.status_code == 202 and r.content == b""
    assert rpc(client, "ping").json() == {"jsonrpc": "2.0", "id": 1, "result": {}}


def test_tools_list_par_la_fabrique_des_surfaces(mcp):
    client, _, active = mcp
    (outil,) = rpc(client, "tools/list").json()["result"]["tools"]
    assert outil["name"] == "web_search"
    assert outil["inputSchema"] == web_search.TOOL.parameters({"web_search"})
    assert outil["description"] == web_search.TOOL.prompt({"web_search"})
    # Le prompt sait quels outils sont offerts avec lui.
    active.append(web_fetch.TOOL)
    outils = rpc(client, "tools/list").json()["result"]["tools"]
    assert [o["name"] for o in outils] == ["web_search", "web_fetch"]
    assert outils[0]["description"] == web_search.TOOL.prompt(
        {"web_search", "web_fetch"})


def test_tools_call_execute_et_rend_le_texte(mcp):
    client, seen, _ = mcp
    r = rpc(client, "tools/call", {"name": "web_search",
                                   "arguments": {"query": "é", "limit": 3}}, id_="a")
    assert r.json() == {"jsonrpc": "2.0", "id": "a", "result": {
        "content": [{"type": "text", "text": "[1] Titre\n    https://e.org\n    extrait"}],
        "isError": False}}
    args, call = seen[0]
    assert args == {"query": "é", "limit": 3}
    assert call.endpoint == "/mcp" and call.model == "" and call.client == ""
    res = rpc(client, "tools/call", {"name": "web_search",
                                     "arguments": {"query": "panne"}}).json()["result"]
    assert res["isError"] is True and res["content"][0]["text"] == "Error: moteur éteint."
    for params in ({"name": "web_fetch", "arguments": {"url": "https://e.org"}},
                   {"name": "rm_rf"}, {"name": "web_search", "arguments": [1]}):
        res = rpc(client, "tools/call", params).json()["result"]
        assert res["isError"] is True, params
    assert len(seen) == 2


def test_fichiers_produits_en_resource_link(mcp, monkeypatch):
    client, _, _ = mcp
    monkeypatch.setattr(files, "PUBLIC_URL", "https://proxy.test")

    async def run(self, args, call):
        return tools.Result("Wrote plot.png", files=(
            tools.Artifact("plot.png", "image/png", b"\x89PNG"),))
    monkeypatch.setattr(web_search.WebSearch, "run", run)
    content = rpc(client, "tools/call", {"name": "web_search", "arguments": {}}
                  ).json()["result"]["content"]
    assert content[0] == {"type": "text", "text": "Wrote plot.png"}
    assert content[1]["type"] == "resource_link" and content[1]["name"] == "plot.png"
    assert content[1]["uri"].startswith("https://proxy.test/")
    # Le type SERVI, celui que files.py a retenu pour ces octets.
    assert content[1]["mimeType"] in ("image/png", "application/octet-stream")


def test_erreurs_de_protocole(mcp):
    client, _, _ = mcp
    assert rpc(client, "resources/list").json()["error"]["code"] == mcp_server.METHOD_NOT_FOUND
    assert rpc(client, "tools/call", [1]).json()["error"]["code"] == mcp_server.INVALID_PARAMS
    r = client.post("/mcp", content=b"{pas du json")
    assert r.status_code == 400 and r.json()["error"]["code"] == mcp_server.PARSE_ERROR
    r = client.post("/mcp", json={"id": 1, "method": "ping"})
    assert r.status_code == 400 and r.json()["error"]["code"] == mcp_server.INVALID_REQUEST
    r = client.post("/mcp", json=[
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"}])
    assert [m["id"] for m in r.json()] == [1, 2]
    for method in ("GET", "DELETE"):
        r = client.request(method, "/mcp")
        assert r.status_code == 405 and r.headers["allow"] == "POST"


def test_origine_controlee(mcp, monkeypatch):
    client, _, _ = mcp
    host = {"Host": "llmproxy"}
    assert rpc(client, "ping", headers={**host, "Origin": "https://evil.example"}).status_code == 403
    assert rpc(client, "ping", headers={**host, "Origin": "http://llmproxy"}).status_code == 200
    monkeypatch.setattr(mcp_server, "ALLOWED_ORIGINS", ["https://app.example"])
    assert rpc(client, "ping", headers={**host, "Origin": "https://app.example"}).status_code == 200


def test_cle_exigee_client_passe_et_desactive(mcp, monkeypatch):
    client, seen, _ = mcp
    monkeypatch.setattr(auth, "PROXY_API_KEYS", frozenset({"sk-ok"}))
    assert rpc(client, "ping").status_code == 401
    rpc(client, "tools/call", {"name": "web_search", "arguments": {"query": "x"}},
        headers={"Authorization": "Bearer sk-ok"})
    assert seen[-1][1].client == tools.owner("sk-ok") != ""
    monkeypatch.setattr(mcp_server, "ENABLED", False)
    assert rpc(client, "ping", headers={"Authorization": "Bearer sk-ok"}).status_code == 404


def test_le_client_mcp_du_proxy_parle_a_son_serveur(mcp, monkeypatch):
    """De bout en bout : tools/mcp.py (le proxy CLIENT d'un serveur MCP)
    découvre et appelle les outils de /mcp (le proxy SERVEUR). Le client
    tente l'ère moderne (server/discover), que ce serveur ne connaît pas,
    et retombe sur initialize."""
    import asyncio

    import httpx2

    from llm_proxy.tools import mcp as client_mcp

    test_client, seen, _ = mcp
    monkeypatch.setattr(tools, "REGISTRY", list(tools.REGISTRY))
    monkeypatch.setattr(client_mcp, "MIN_GAP", 0)

    def relais(request: httpx2.Request) -> httpx2.Response:
        r = test_client.post("/mcp", content=request.content,
                             headers={k: v for k, v in request.headers.items()
                                      if k.lower() not in ("host", "content-length")})
        return httpx2.Response(r.status_code, content=r.content,
                              headers={"content-type": r.headers.get(
                                  "content-type", "application/json")})

    async def scenario():
        server = client_mcp.Server("moi", {"url": "http://proxy.test/mcp"},
                                   transport=httpx2.MockTransport(relais))
        server.open()
        ok = await server.refresh()
        outil = next(t for t in tools.REGISTRY if t.name == "moi_web_search")
        result = await outil.run({"query": "llama"}, tools.Call())
        await server.close()
        return ok, server, result

    ok, server, result = asyncio.run(scenario())
    assert ok, server.error
    assert result.error is None and "https://e.org" in result.text
    assert seen[-1][0] == {"query": "llama"}
