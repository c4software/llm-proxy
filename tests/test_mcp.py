"""Le client MCP (llm_proxy/tools/mcp.py), face à un FAUX serveur MCP joué
par httpx.MockTransport : aucun réseau. Le faux serveur parle l'une des
deux ères du protocole (moderne, sans état ; héritée, à `initialize` et
session), répond en JSON ou en flux SSE, pagine, oublie ses sessions,
s'éteint. Le registre des outils hébergés est remplacé par une liste
vide, la configuration posée par les tables passées à mcp.Server.

Un test par comportement ; les familles d'entrées sont des tables, et
l'assertion nomme l'entrée fautive."""

import asyncio
import json

import httpx
import pytest

from llm_proxy import tools
from llm_proxy.tools import mcp

URL = "http://mcp.test/mcp"
SEARCH = {"name": "search", "description": "Search the docs.",
          "inputSchema": {"type": "object", "properties": {
              "q": {"type": "string"}}, "required": ["q"]}}
READ = {"name": "page.read", "title": "Read a page",
        "inputSchema": {"type": "object"}}


def go(coro):
    return asyncio.run(coro)


def text(value: str, **more) -> dict:
    return {"content": [{"type": "text", "text": value}], **more}


class FakeMcp:
    """Un serveur MCP. `era` : «modern» (refuse `initialize`, exige les
    en-têtes et `_meta` de chaque requête) ou «legacy» (exige la session
    ouverte par `initialize`, ne connaît pas `server/discover`). `seen` :
    (méthode, en-têtes, corps) de chaque requête reçue."""

    def __init__(self, era="modern", sse=False, defs=(SEARCH,), page=0,
                 version="2025-06-18"):
        self.era, self.sse, self.page, self.version = era, sse, page, version
        self.defs = list(defs)
        self.results = {}           # outil → résultat, erreur ou fonction
        self.sessions, self.opened = set(), 0
        self.amnesia = False        # toute session est aussitôt oubliée
        self.down = False
        self.status = 0             # ce statut, sans corps, à toute requête
        self.delay = 0.0
        self.offers = None          # versions d'un refus -32022
        self.seen = []

    def methods(self):
        return [m for m, _, _ in self.seen]

    def reply(self, body, payload, status=200, headers=None):
        message = {"jsonrpc": "2.0", "id": body.get("id"), **payload}
        if status == 200 and self.sse:
            # Un commentaire de maintien, une notification, puis la
            # réponse en plusieurs lignes `data:` — sans ligne vide finale.
            note = json.dumps({"jsonrpc": "2.0",
                               "method": "notifications/tools/list_changed"})
            lines = json.dumps(message, indent=1, ensure_ascii=False) \
                .split("\n")
            stream = (": ping\r\n\r\nevent: message\r\ndata: " + note
                      + "\r\n\r\nid: 7\r\n"
                      + "\r\n".join("data: " + line for line in lines))
            return httpx.Response(200, content=stream.encode(), headers={
                "content-type": "text/event-stream", **(headers or {})})
        return httpx.Response(status, json=message, headers=headers)

    def error(self, body, code, message, status=200, data=None):
        error = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return self.reply(body, {"error": error}, status)

    async def __call__(self, request):
        if self.down:
            raise httpx.ConnectError("connexion refusée")
        if request.method == "DELETE":
            self.seen.append(("DELETE", request.headers, None))
            self.sessions.discard(request.headers.get("mcp-session-id"))
            return httpx.Response(200)
        body = json.loads(request.content)
        method, params = body.get("method"), body.get("params") or {}
        self.seen.append((method, request.headers, body))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.status:
            return httpx.Response(self.status)
        if self.era == "modern":
            meta = params.get("_meta") or {}
            version = meta.get("io.modelcontextprotocol/protocolVersion")
            if self.offers is not None:
                return self.error(body, -32022, "Unsupported protocol version",
                                  400, {"supported": self.offers})
            if version != mcp.MODERN \
                    or request.headers.get("mcp-protocol-version") != version \
                    or request.headers.get("mcp-method") != method \
                    or "io.modelcontextprotocol/clientCapabilities" not in meta:
                return self.error(body, -32020, "Header mismatch", 400)
            if method == "server/discover":
                return self.reply(body, {"result": {
                    "resultType": "complete",
                    "supportedVersions": [mcp.MODERN],
                    "capabilities": {"tools": {}}}})
        elif method == "initialize":
            self.opened += 1
            session = f"session-{self.opened}"
            if not self.amnesia:
                self.sessions.add(session)
            return self.reply(body, {"result": {
                "protocolVersion": self.version, "capabilities": {"tools": {}},
                "serverInfo": {"name": "faux", "version": "1"}}},
                headers={"Mcp-Session-Id": session})
        elif "mcp-session-id" not in request.headers:
            return self.error(body, -32000, "Bad Request: no session", 400)
        elif request.headers["mcp-session-id"] not in self.sessions:
            return httpx.Response(404)
        elif method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            start = int(params.get("cursor") or 0)
            end = start + self.page if self.page else len(self.defs)
            result = {"tools": self.defs[start:end], "ttlMs": 60_000}
            if end < len(self.defs):
                result["nextCursor"] = str(end)
            return self.reply(body, {"result": result})
        if method == "tools/call":
            result = self.results.get(params.get("name"),
                                      text(json.dumps(params.get("arguments"),
                                                      ensure_ascii=False)))
            if callable(result):
                result = result(params)
            return self.reply(body, result if "error" in result
                              else {"result": result})
        return self.error(body, -32601, "Method not found", 404)


@pytest.fixture(autouse=True)
def registre(monkeypatch):
    """Un registre vide : les outils MCP s'y enregistrent, rien d'autre
    n'y est. Et pas d'attente entre deux découvertes."""
    monkeypatch.setattr(tools, "REGISTRY", [])
    monkeypatch.setattr(mcp, "MIN_GAP", 0)
    return tools.REGISTRY


def serveur(fake, name="docs", **table) -> mcp.Server:
    server = mcp.Server(name, {"url": URL, **table},
                        transport=httpx.MockTransport(fake))
    return server


async def decouvre(fake, **table) -> mcp.Server:
    server = serveur(fake, **table)
    server.open()
    assert await server.refresh(), server.error
    return server


async def appel(name, args=None) -> tools.Result:
    """Par l'exécuteur du proxy, comme une surface ou /v1/tools."""
    return await tools.Hosted().run(name, json.dumps(args or {}), 0)


# ── les deux ères ───────────────────────────────────────────────────────

def test_serveur_moderne_sans_etat_ni_session():
    """Ère moderne : aucune `initialize`, chaque requête porte sa version
    et ses en-têtes ; les outils deviennent des fonctions du proxy."""
    fake = FakeMcp(defs=(SEARCH, READ))

    async def scenario():
        server = await decouvre(fake)
        result = await appel("docs_search", {"q": "llama"})
        await server.close()
        return server, result

    server, result = go(scenario())
    assert fake.methods() == ["server/discover", "tools/list", "tools/call"]
    assert (server.era, server.ttl) == (None, 60.0)     # fermé ; ttlMs lu
    for method, headers, body in fake.seen:
        assert headers["mcp-protocol-version"] == mcp.MODERN, method
        assert headers["mcp-method"] == method
        assert "mcp-session-id" not in headers
        assert "text/event-stream" in headers["accept"] \
            and "application/json" in headers["accept"]
        meta = body["params"]["_meta"]
        assert meta["io.modelcontextprotocol/clientInfo"]["name"] == "llm-proxy"
    assert fake.seen[-1][1]["mcp-name"] == "search"
    assert fake.seen[-1][2]["params"]["arguments"] == {"q": "llama"}
    assert (result.text, result.error) == ('{"q": "llama"}', None)
    assert result.meta == {"server": "docs", "tool": "search"}

    # Les fonctions : préfixées du serveur, le point du nom MCP remplacé,
    # la description du serveur (ou son titre), son schéma.
    by_name = {t.name: t for t in tools.REGISTRY}
    assert list(by_name) == ["docs_search", "docs_page_read"]
    spec = by_name["docs_search"].spec(frozenset(by_name))
    assert spec == {"type": "function", "function": {
        "name": "docs_search", "description": "Search the docs.",
        "parameters": SEARCH["inputSchema"]}}
    assert by_name["docs_page_read"].spec(())["function"]["description"] \
        == "Read a page"
    # Ce qui les déclare sur /v1/chat/completions.
    assert {"docs_search", "mcp:docs", "mcp"} <= tools.kinds()
    assert not tools.enabled()                  # serveur fermé : plus actifs


def test_serveur_herite_initialize_session_sse_pagination():
    """Ère héritée : la requête moderne est refusée sans erreur moderne →
    `initialize`, `notifications/initialized`, puis tout sous la session.
    Réponses en flux SSE, liste en trois pages."""
    more = {"name": "third", "inputSchema": {"type": "object"}}
    fake = FakeMcp("legacy", sse=True, defs=(SEARCH, READ, more), page=1)

    async def scenario():
        server = await decouvre(fake)
        stale = server._stale.is_set()
        result = await appel("docs_search", {"q": "é"})
        await server.close()
        return server, stale, result

    server, stale, result = go(scenario())
    assert fake.methods() == [
        "server/discover", "initialize", "notifications/initialized",
        "tools/list", "tools/list", "tools/list", "tools/call", "DELETE"]
    assert "mcp-session-id" not in fake.seen[1][1]
    assert "mcp-protocol-version" not in fake.seen[1][1]
    assert fake.seen[1][2]["params"]["protocolVersion"] == mcp.LEGACY[0]
    for method, headers, body in fake.seen[2:]:
        assert headers["mcp-session-id"] == "session-1", method
        # La version que le SERVEUR a retenue, pas celle demandée.
        assert headers["mcp-protocol-version"] == "2025-06-18", method
        assert "mcp-method" not in headers
        assert body is None or "_meta" not in body.get("params", {}), method
    assert [b.get("params", {}).get("cursor") for _, _, b in fake.seen[3:6]] \
        == [None, "1", "2"]
    assert [t.name for t in tools.REGISTRY] \
        == ["docs_search", "docs_page_read", "docs_third"]
    assert result.text == '{"q": "é"}'
    # `notifications/tools/list_changed` lue dans le flux : la prochaine
    # découverte est avancée.
    assert stale


def test_ere_et_version():
    """Qui parle quoi : l'ère retenue, ou le refus."""
    cases = {
        # serveur moderne qui n'offre que des versions héritées (double ère)
        "double": (dict(era="legacy"), None, ("legacy", "2025-06-18")),
        "aucune version commune": (dict(offers=["2030-01-01"]),
                                   "speaks none of the protocol versions",
                                   None),
        "version héritée inconnue": (dict(era="legacy", version="2024-11-05"),
                                     "speaks a protocol version", None),
    }
    for label, (options, refusal, expected) in cases.items():
        offers = options.pop("offers", None)
        fake = FakeMcp(**options)
        fake.offers = offers
        tools.REGISTRY.clear()

        async def scenario():
            server = serveur(fake)
            server.open()
            ok = await server.refresh()
            state = (server.era, server.version)
            await server.close()
            return ok, state, server.error

        ok, state, error = go(scenario())
        if refusal:
            assert not ok and refusal in error, label
        else:
            assert ok and state == expected, label


def test_session_expiree_rouverte_une_fois():
    """404 sous une session : le client en ouvre une autre et rejoue la
    requête — une fois. Un serveur qui oublie tout rend une erreur, pas
    une boucle."""
    fake = FakeMcp("legacy")

    async def scenario():
        server = await decouvre(fake)
        fake.sessions.clear()
        first = await appel("docs_search", {"q": "a"})
        fake.sessions.clear()
        fake.amnesia = True
        second = await appel("docs_search", {"q": "b"})
        await server.close()
        return first, second

    first, second = go(scenario())
    assert (first.text, first.error) == ('{"q": "a"}', None)
    assert fake.methods()[4:9] == [
        "tools/call", "initialize", "notifications/initialized", "tools/call",
        "tools/call"]
    assert fake.seen[7][1]["mcp-session-id"] == "session-2"
    assert second.error == "unavailable" and "HTTP 404" in second.text
    assert fake.opened == 3                     # une seule réouverture


# ── résultats et échecs ─────────────────────────────────────────────────

def test_resultats_rendus_en_texte():
    """`tools/call` → Result : le texte tel quel, le reste par une ligne
    qui dit ce qui manque, l'erreur de l'outil avec son code."""
    png = "QUJD" * 100
    cases = {
        "textes": (
            {"content": [{"type": "text", "text": "un"},
                         {"type": "text", "text": "deux"}]},
            "un\n\ndeux", None, {}),
        "erreur de l'outil": (
            text("date must be in the future", isError=True),
            "Error: date must be in the future", "failed", {}),
        "erreur muette": (
            {"content": [], "isError": True},
            "Error: the tool reported an error without a message.",
            "failed", {}),
        "image": (
            {"content": [{"type": "text", "text": "voici"},
                         {"type": "image", "data": png,
                          "mimeType": "image/png"}]},
            "voici\n\n[image content omitted: image/png, 300 bytes — this "
            "proxy returns text only]", None, {"omitted": ["image/png"]}),
        "audio": (
            {"content": [{"type": "audio", "data": "QUJD",
                          "mimeType": "audio/wav"}]},
            "[audio content omitted: audio/wav, 3 bytes — this proxy "
            "returns text only]", None, {"omitted": ["audio/wav"]}),
        "ressource textuelle": (
            {"content": [{"type": "resource", "resource": {
                "uri": "file:///a.rs", "mimeType": "text/x-rust",
                "text": "fn main() {}"}}]},
            "[resource file:///a.rs]\nfn main() {}", None, {}),
        "ressource binaire": (
            {"content": [{"type": "resource", "resource": {
                "uri": "file:///a.bin", "blob": "QUJD"}}]},
            "[resource content omitted: file:///a.bin, 3 bytes — this "
            "proxy returns text only]", None, {"omitted": ["resource"]}),
        "lien": (
            {"content": [{"type": "resource_link", "name": "Guide",
                          "uri": "https://docs.test/guide",
                          "description": "Le guide.",
                          "mimeType": "text/html"}]},
            "[resource link: Guide — https://docs.test/guide (text/html)] "
            "Le guide.", None, {}),
        "structuré seul": (
            {"content": [], "structuredContent": {"t": 22.5}},
            '{"t": 22.5}', None, {"structured": {"t": 22.5}}),
        "structuré doublé d'un texte": (
            text("22,5 °C", structuredContent={"t": 22.5}),
            "22,5 °C", None, {"structured": {"t": 22.5}}),
        "vide": ({"content": []}, "The tool returned no content.", None, {}),
        "demande une saisie": (
            {"resultType": "input_required", "inputRequests": {}},
            "Error: this tool asks for input from the user (elicitation or "
            "sampling), which this proxy cannot provide.", "unsupported",
            None),
        "erreur de protocole, paramètres": (
            {"error": {"code": -32602, "message": "Unknown tool:\nsearch"}},
            "Error: the MCP server docs answered an error (-32602): Unknown "
            "tool: search", "invalid_input", None),
        "erreur de protocole, serveur": (
            {"error": {"code": -32603, "message": "boom"}},
            "Error: the MCP server docs answered an error (-32603): boom",
            "unavailable", None),
    }
    fake = FakeMcp()

    async def scenario():
        server = await decouvre(fake)
        out = {}
        for label, (result, *_) in cases.items():
            fake.results["search"] = result
            out[label] = await appel("docs_search")
        await server.close()
        return out

    for label, got in go(scenario()).items():
        _, wanted, code, meta = cases[label]
        assert (got.text, got.error) == (wanted, code), label
        if meta is not None:
            assert got.meta == {"server": "docs", "tool": "search", **meta}, \
                label


def test_lien_de_ressource_en_source():
    """Un lien http(s) est aussi une source, pour les annotations du
    client ; un lien d'un autre schéma n'en est pas une."""
    result = mcp.render({"content": [
        {"type": "resource_link", "name": "Guide", "uri": "https://d.test/g"},
        {"type": "resource_link", "name": "Local", "uri": "file:///x"}]},
        "docs", "search")
    assert result.sources == (tools.Source("https://d.test/g", "Guide"),)


def test_echecs_du_serveur():
    """Le serveur ne répond pas comme il faut : toujours un Result en
    erreur, jamais une exception — et le code qui convient."""
    cases = {
        "éteint": (dict(down=True), "unavailable", "unreachable"),
        "identifiants refusés": (dict(status=401), "unavailable",
                                 "refused the credentials"),
        "ralentir": (dict(status=429), "too_many_requests", "slow down"),
        "panne": (dict(status=500), "unavailable", "HTTP 500"),
        "trop lent": (dict(delay=0.5), "timeout", "timed out after"),
    }
    for label, (state, code, said) in cases.items():
        fake = FakeMcp()
        tools.REGISTRY.clear()

        async def scenario():
            server = await decouvre(fake, timeout=0.1)
            vars(fake).update(state)
            result = await appel("docs_search", {"q": "x"})
            fake.delay = 0
            await server.close()
            return result

        result = go(scenario())
        assert result.error == code and said in result.text, (label, result)
        assert result.text.startswith("Error: "), label


# ── découverte ──────────────────────────────────────────────────────────

async def jusqua(condition, tries=400):
    for _ in range(tries):
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition jamais atteinte")


def test_serveur_eteint_au_demarrage_puis_liste_qui_change():
    """start() rend la main serveur éteint ; ses outils s'enregistrent
    quand il répond ; un outil retiré est désactivé et garde sa place ;
    une coupure ne retire rien ; stop() ferme la session."""
    fake = FakeMcp("legacy", defs=(SEARCH, READ))
    fake.down = True

    async def scenario():
        server = serveur(fake)
        await mcp.start([server])
        seen = [(server.up, [t.name for t in tools.enabled()])]

        fake.down = False
        server.changed()
        await jusqua(lambda: server.up)
        seen.append([t.name for t in tools.enabled()])
        result = await appel("docs_page_read")

        fake.defs = [READ, dict(SEARCH, name="find")]
        server.changed()
        await jusqua(lambda: len(tools.REGISTRY) == 3)
        seen.append([t.name for t in tools.enabled()])

        fake.down = True
        server.changed()
        await jusqua(lambda: server.up is False)
        seen.append([t.name for t in tools.enabled()])
        lost = await appel("docs_find")

        fake.down = False
        fake.defs = [SEARCH, READ]
        server.changed()
        await jusqua(lambda: server.up)
        seen.append([t.name for t in tools.enabled()])
        state = mcp.status() or [dict(up=server.up, era=server.era)]
        await mcp.stop()
        return seen, result, lost, state

    seen, result, lost, state = go(scenario())
    assert seen == [
        (False, []),
        ["docs_search", "docs_page_read"],
        ["docs_page_read", "docs_find"],         # search retiré
        ["docs_page_read", "docs_find"],         # coupure : rien ne bouge
        ["docs_search", "docs_page_read"],       # revenu, à sa place
    ]
    assert result.error is None
    assert lost.error == "unavailable"
    assert state[0]["up"] is True and state[0]["era"] == "legacy"
    assert fake.methods()[-1] == "DELETE"
    assert [t.name for t in tools.REGISTRY] \
        == ["docs_search", "docs_page_read", "docs_find"]
    assert not tools.enabled() and not mcp._TASKS


def test_demarrage_n_attend_pas_un_serveur_lent(monkeypatch):
    monkeypatch.setattr(mcp, "STARTUP_WAIT", 0.05)
    fake = FakeMcp()
    fake.delay = 0.2

    async def scenario():
        server = serveur(fake)
        loop = asyncio.get_running_loop()
        started = loop.time()
        await mcp.start([server, serveur(fake, "off", enabled=False)])
        waited = loop.time() - started
        early = [t.name for t in tools.enabled()]
        await jusqua(lambda: server.up)
        late = [t.name for t in tools.enabled()]
        await mcp.stop()
        return waited, early, late

    waited, early, late = go(scenario())
    assert waited < 0.19 and early == [] and late == ["docs_search"]


def test_outils_choisis_bornes_ecartes(monkeypatch):
    """Ce qui entre dans le prompt est choisi (`tools`, `exclude`,
    `max_tools`) et borné (description, schéma) ; une définition
    inutilisable écarte SON outil, pas les autres."""
    monkeypatch.setattr(mcp, "DESCRIPTION_CHARS", 20)
    monkeypatch.setattr(mcp, "SCHEMA_CHARS", 200)
    obj = {"type": "object"}
    defs = [
        {"name": "get_a", "description": "x" * 50 + "\x00", "inputSchema": obj},
        {"name": "get_b", "inputSchema": {**obj, "$schema": "http://j/s"}},
        {"name": "get_secret", "inputSchema": obj},
        {"name": "delete_all", "inputSchema": obj},
        {"name": "get_huge", "inputSchema": {**obj, "description": "y" * 300}},
        {"name": "get_null", "inputSchema": None},
        {"name": "get_array", "inputSchema": {"type": "array"}},
        {"name": "get_a", "inputSchema": obj},          # doublon
        {"name": "", "inputSchema": obj},
        {"name": "get_c", "inputSchema": obj},
        {"name": "get_d", "inputSchema": obj},
    ]
    fake = FakeMcp(defs=defs)

    async def scenario():
        server = await decouvre(fake, tools=["get_*"], exclude=["*secret"],
                                max_tools=3, prefix="")
        await server.close()

    go(scenario())
    specs = {t.name: t.spec(())["function"] for t in tools.REGISTRY}
    assert list(specs) == ["get_a", "get_b", "get_c"]
    assert specs["get_a"]["description"] == "x" * 20 + "…"
    assert specs["get_b"] == {
        "name": "get_b", "parameters": obj,
        "description": "Tool get_b of the MCP server docs."}


def test_noms_de_fonction_et_collisions():
    cases = {
        ("docs", "search"): "docs_search",
        ("", "search"): "search",
        ("docs", "admin.tools.list"): "docs_admin_tools_list",
        ("docs", "créer page"): "docs_cr_er_page",
    }
    for (prefix, remote), wanted in cases.items():
        assert mcp.function_name(prefix, remote) == wanted, (prefix, remote)
    # Trop long : coupé, fini par un condensé — unique, et stable.
    long_a, long_b = "t" * 128, "t" * 127 + "u"
    names = [mcp.function_name("docs", n) for n in (long_a, long_b, long_a)]
    assert all(len(n) == 64 for n in names)
    assert names[0] == names[2] != names[1]

    # Deux serveurs, le même outil : deux fonctions. Un nom déjà pris
    # (ici par l'autre écriture du même nom) : l'outil est écarté.
    one = FakeMcp(defs=(SEARCH, {"name": "a.b", "inputSchema": {
        "type": "object"}}, {"name": "a_b", "inputSchema": {"type": "object"}}))
    two = FakeMcp()

    async def scenario():
        first = await decouvre(one)
        second = await decouvre(two, name="wiki")
        fake_names = [t.name for t in tools.enabled()]
        await first.close()
        await second.close()
        return fake_names

    assert go(scenario()) == ["docs_search", "docs_a_b", "wiki_search"]
    assert one.methods().count("tools/call") == 0


def test_parametres_recopies_en_en_tetes():
    """`x-mcp-header` : la valeur du paramètre part aussi en en-tête
    `Mcp-Param-<nom>`, encodée s'il le faut ; une annotation interdite
    écarte l'outil."""
    values = {
        "us-west1": "us-west1",
        "Hello, 世界": "=?base64?SGVsbG8sIOS4lueVjA==?=",
        " padded ": "=?base64?IHBhZGRlZCA=?=",
        "line1\nline2": "=?base64?bGluZTEKbGluZTI=?=",
        "=?base64?literal?=": "=?base64?PT9iYXNlNjQ/bGl0ZXJhbD89?=",
        42: "42", True: "true", False: "false",
    }
    for value, wanted in values.items():
        assert mcp.header_value(value) == wanted, repr(value)

    region = {"type": "string", "x-mcp-header": "Region"}
    refused = {
        "nom vide": {"properties": {"a": {"type": "string",
                                          "x-mcp-header": ""}}},
        "hors syntaxe": {"properties": {"a": {"type": "string",
                                              "x-mcp-header": "a b"}}},
        "nombre": {"properties": {"a": {"type": "number",
                                        "x-mcp-header": "A"}}},
        "doublon": {"properties": {"a": region, "b": dict(
            region, **{"x-mcp-header": "region"})}},
        "sous items": {"properties": {"a": {"type": "array",
                                            "items": region}}},
        "sous anyOf": {"anyOf": [{"properties": {"a": region}}]},
    }
    for label, schema in refused.items():
        with pytest.raises(ValueError):
            mcp.param_headers({"type": "object", **schema})
        assert label
    # Une PROPRIÉTÉ nommée «x-mcp-header» n'est pas une annotation.
    assert mcp.param_headers({"type": "object", "properties": {
        "x-mcp-header": {"type": "string"}}}) == ()

    sql = {"name": "sql", "inputSchema": {"type": "object", "properties": {
        "region": region, "query": {"type": "string"},
        "opts": {"type": "object", "properties": {
            "dry": {"type": "boolean", "x-mcp-header": "Dry-Run"}}}}}}
    bad = {"name": "bad", "inputSchema": {"type": "object",
                                          **refused["sous items"]}}
    fake = FakeMcp(defs=(sql, bad))

    async def scenario():
        server = await decouvre(fake)
        await appel("docs_sql", {"region": "é", "opts": {"dry": True}})
        await appel("docs_sql", {"query": "select 1", "region": None})
        wrong = await appel("docs_sql", {"region": 1.5})
        await server.close()
        return wrong

    wrong = go(scenario())
    assert [t.name for t in tools.REGISTRY] == ["docs_sql"]
    calls = [h for m, h, _ in fake.seen if m == "tools/call"]
    assert len(calls) == 2                       # le troisième n'est pas parti
    assert (calls[0]["mcp-param-region"], calls[0]["mcp-param-dry-run"]) \
        == ("=?base64?w6k=?=", "true")
    assert not [k for k in calls[1] if k.startswith("mcp-param-")]
    assert wrong.error == "invalid_input" and "`region`" in wrong.text


# ── configuration ───────────────────────────────────────────────────────

def test_configuration():
    """[tools.mcp] : une sous-table par serveur, les valeurs simples sont
    les réglages communs. Les en-têtes partent à chaque requête ; un
    secret absent n'est pas envoyé à moitié."""
    servers = mcp.load({
        "timeout": 30,
        "docs": {"url": URL, "headers": {
            "Authorization": "Bearer abc", "X-Api-Key": "",
            "X-Other": "Bearer "}, "tools": "search, get_*", "refresh": 0},
        "off": {"url": "https://mcp.example.org/mcp?key=secret",
                "enabled": False},
    })
    docs, off = servers
    assert (docs.name, docs.enabled, docs.prefix) == ("docs", True, "docs")
    assert docs.headers["Authorization"] == "Bearer abc"
    assert docs.missing == ["X-Api-Key", "X-Other"]
    assert docs.allow == ["search", "get_*"]
    assert docs.pause(0) is None and 9 <= docs.pause(1) <= 11
    assert (off.enabled, off.where) == (False, "https://mcp.example.org/mcp")
    assert mcp.load(None) == []

    fake = FakeMcp()
    docs.transport = httpx.MockTransport(fake)

    async def scenario():
        docs.open()
        assert await docs.refresh()
        await docs.close()

    go(scenario())
    assert all(h["authorization"] == "Bearer abc" and "x-api-key" not in h
               for _, h, _ in fake.seen)

    invalid = {
        "sans url": {"x": {}},
        "url d'un autre schéma": {"x": {"url": "stdio://serveur"}},
        "nom de serveur": {"a.b": {"url": URL}},
        "en-tête réservé": {"x": {"url": URL, "headers": {
            "Mcp-Session-Id": "1"}}},
        "préfixe": {"x": {"url": URL, "prefix": "a b"}},
        "délai": {"x": {"url": URL, "timeout": "long"}},
    }
    for label, table in invalid.items():
        with pytest.raises(SystemExit):
            mcp.load(table)
        assert label


# ── dans le proxy : déclaration, délai, état ────────────────────────────

def test_declaration_connue_des_qu_un_serveur_est_configure(monkeypatch):
    """`mcp` et `mcp:<serveur>` sont des types de déclaration dès que la
    configuration liste un serveur — avant toute découverte : le client
    reçoit alors le refus du proxy, qui dit pourquoi, et non celui d'un
    backend. Sans serveur configuré, ces types ne sont pas ceux du proxy."""
    from llm_proxy import anthropic_api, chat_api, responses_api
    assert mcp.kinds() == frozenset() and "mcp" not in tools.kinds()
    fake = FakeMcp()
    server = serveur(fake)
    monkeypatch.setattr(mcp, "SERVERS", [server])
    assert mcp.kinds() == tools.kinds() == {"mcp", "mcp:docs"}

    def requete(kind):
        return {"model": "essai/qwen", "tools": [{"type": kind}],
                "messages": [{"role": "user", "content": "x"}]}

    for kind in ("mcp", "mcp:docs"):
        assert chat_api.declares(requete(kind), tools.kinds())
        with pytest.raises(chat_api.Refused) as exc:
            chat_api.prepare(requete(kind), tools.Hosted(), tools.kinds())
        assert "pas encore joint" in str(exc.value)
    assert not chat_api.declares(requete("mcp:autre"), tools.kinds())

    async def scenario():
        await mcp.start([server])
        try:
            h = tools.Hosted()
            # Déclarés : tous, ceux d'un serveur, ou un seul.
            for kind in ("mcp", "mcp:docs", "docs_search"):
                payload = requete(kind)
                ctx = chat_api.prepare(payload, h, tools.kinds())
                assert list(ctx.hosted) == ["docs_search"], kind
                assert payload["tools"] == [h.by_name["docs_search"].spec(())]
            # D'office, par leur nom ([chat].always).
            payload = {"model": "essai/qwen", "messages": []}
            ctx = chat_api.prepare(payload, h, tools.kinds(),
                                   ("docs_search", "docs_absent"))
            assert list(ctx.hosted) == ["docs_search"]
            # … ou tous ceux d'un serveur, `mcp:<serveur>` ; `mcp` nu
            # n'est pas une forme de cette liste.
            for always, noms in ((["mcp:docs", "docs_search"], ["docs_search"]),
                                 (["mcp:autre", "mcp"], [])):
                monkeypatch.setattr(chat_api, "ALWAYS", always)
                assert chat_api.offered(h.tools) == noms, always
            # Sans liaison : ni Responses, ni Anthropic.
            assert h.for_responses("mcp") == [] and h.for_server("mcp_1") is None
            ctx = responses_api.to_chat({"model": "m", "input": "x", "tools": [
                {"type": "mcp", "server_label": "docs"}]}, hosted=h)[1]
            assert not ctx.hosted
            assert not anthropic_api.Context({"tools": [
                {"type": "mcp_20260101", "name": "docs_search"}]}, h).hosted
            return mcp.status()
        finally:
            await mcp.stop()

    [etat] = go(scenario())
    assert etat == {"name": "docs", "url": URL, "enabled": True, "up": True,
                    "error": "", "protocol": "2026-07-28", "era": "modern",
                    "tools": ["docs_search"]}


def test_delai_propre_et_echec_de_l_outil_par_l_executeur(monkeypatch):
    """Par tools.Hosted : le délai est celui du SERVEUR (plus de quoi
    rendre son erreur), à la place de [tools].run_timeout ; `isError`
    sort avec le code `failed`, compté `error`."""
    fake = FakeMcp()
    fake.results["search"] = text("quota exceeded upstream", isError=True)
    lines = []
    monkeypatch.setattr(tools.stats, "record_tool", lambda *a: lines.append(a))
    monkeypatch.setattr(tools, "RUN_TIMEOUT", 0.001)

    async def scenario():
        server = serveur(fake, timeout=7)
        await mcp.start([server])
        try:
            tool = tools.Hosted().by_name["docs_search"]
            assert tool.timeout == 7 + mcp.GRACE and tool.max_calls is None
            return await appel("docs_search", {"q": "x"})
        finally:
            await mcp.stop()

    result = go(scenario())
    assert (result.error, result.text) == (
        "failed", "Error: quota exceeded upstream")
    assert [line[3] for line in lines] == ["error"]
