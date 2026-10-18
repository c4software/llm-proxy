"""Ce que plusieurs fichiers de tests ont en commun : de quoi fabriquer
un flux upstream chat/completions, le passer à un robinet, relire ce
qu'il rend ; de faux outils hébergés ; et, pour les outils qui
téléchargent (test_tools, test_ocr, test_transcribe, test_code_execution,
test_image_generation), un DNS sans réseau, un faux web, un faux backend
et un PDF écrit à la main. Aucun réseau. Des fonctions simples ; la
fixture `proxy`, qui monte l'application dessus, est dans conftest.py."""

import asyncio
import json
import socket

import conftest  # noqa: F401 — pose CONFIG_PATH avant tout import du paquet
import httpx2

from llm_proxy import tools
from llm_proxy.tools import net, web_fetch, web_search

PUBLIC = "93.184.216.34"


def go(coro):
    return asyncio.run(coro)


def bearer(key) -> dict:
    return {"Authorization": f"Bearer {key}"}


def rpc(client, method, params=None, id_=1, headers=None):
    """Un message JSON-RPC posté sur /mcp, comme un client MCP l'envoie."""
    msg = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        msg["params"] = params
    return client.post("/mcp", json=msg, headers={
        "Accept": "application/json, text/event-stream", **(headers or {})})


# ── flux upstream : ce qu'un backend chat/completions répond ────────────

def chunk(delta=None, finish=None, **extra):
    return {"id": "chatcmpl-1", "created": 1700000000,
            "choices": [{"index": 0, "delta": delta or {},
                         "finish_reason": finish}], **extra}


def tool_call(index, call_id, name, arguments):
    return chunk({"tool_calls": [{"index": index, "id": call_id, "function": {
        "name": name, "arguments": arguments}}]})


def usage(prompt, completion, cached=0, reasoning=0):
    return {"choices": [], "usage": {
        "prompt_tokens": prompt, "completion_tokens": completion,
        "prompt_tokens_details": {"cached_tokens": cached},
        "completion_tokens_details": {"reasoning_tokens": reasoning}}}


def sse(*docs) -> bytes:
    return b"".join(b"data: " + json.dumps(d).encode() + b"\n\n" for d in docs)


def stream(*docs) -> bytes:
    """Un flux upstream complet : les documents, puis `[DONE]`."""
    return sse(*docs) + b"data: [DONE]\n\n"


def chat_doc(message, finish, prompt, completion, cached=0) -> bytes:
    """Une réponse upstream non streamée."""
    return json.dumps({
        "id": "chatcmpl-1", "created": 1700000000,
        "choices": [{"finish_reason": finish, "message": message}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                  "prompt_tokens_details": {"cached_tokens": cached}},
    }).encode()


def feed(t, raw: bytes, cut: int = 7) -> bytes:
    """Un tour upstream entier passé au robinet `t`, finish() compris, par
    morceaux de `cut` octets : les événements ne tombent jamais sur une
    frontière de chunk réseau."""
    out = b"".join(t.feed(raw[i:i + cut]) for i in range(0, len(raw), cut))
    return out + t.finish()


def sse_events(raw: bytes) -> list[tuple[str, dict]]:
    """Les événements d'un flux rendu au client : (`event`, `data`). Une
    ligne de commentaire (`: ping`) n'en est pas un."""
    out = []
    for block in raw.decode().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.split("\n")
                      if ": " in line and not line.startswith(":"))
        if "data" in fields:
            out.append((fields.get("event", ""), json.loads(fields["data"])))
    return out


class FakeUpstream:
    """La réponse ouverte que rend relaying.send_upstream. Sans `content_type`,
    il est déduit du corps : un flux SSE, ou du JSON."""

    def __init__(self, body: bytes, status=200, content_type=None):
        self.body, self.status_code = body, status
        self.headers = {"content-type": content_type or (
            "text/event-stream" if body.startswith(b"data:")
            else "application/json")}
        self.closed = False

    async def aiter_raw(self):
        for i in range(0, len(self.body), 64):
            yield self.body[i:i + 64]

    async def aread(self):
        return self.body

    async def aclose(self):
        self.closed = True


# ── outils hébergés ─────────────────────────────────────────────────────

RESULTS = [
    {"title": "Releases · ggml-org/llama.cpp", "date": "2026-10-03",
     "url": "https://github.com/ggml-org/llama.cpp/releases",
     "snippet": "LLM inference in C/C++ — b6789, «latest»…"},
    {"title": "llama.cpp (blog)", "date": "",
     "url": "https://example.org/blog/llama", "snippet": ""},
]
# Le résultat de l'outil pour ces deux-là, et le texte que le modèle en lit.
SEARCHED = web_search.found(
    "llama.cpp latest release", [tools.Source(**r) for r in RESULTS])
FOUND = SEARCHED.text


def failed(code: str, text: str) -> tools.Result:
    """Un résultat en erreur, par son texte entier («Error: …») tel que
    les tests l'écrivent, et son code."""
    assert text.startswith("Error: ")
    return tools.failure(code, text[len("Error: "):])


class Echo(tools.Tool):
    """L'outil MINIMAL du contrat — celui de docs/outils.md, « Écrire un
    outil » : un nom, un prompt, le schéma de ses arguments, une
    exécution. Sans liaison de protocole."""
    name = "echo"

    def prompt(self, present):
        return "Return the given text, unchanged."

    def parameters(self, present):
        return {
            "type": "object",
            "properties": {"text": {"type": "string",
                                    "description": "The text to return."}},
            "required": ["text"]}

    async def run(self, args, call):
        text = args.get("text")
        if not isinstance(text, str) or not text:
            raise tools.ToolError("invalid_input", "`text` is required.")
        return tools.Result(text, meta={"chars": len(text)})


def outil(name="echo", run=None, responses=None):
    """Un outil de test autour d'une fonction `run(args)` qui rend le
    TEXTE du résultat (ou lève). Par défaut il rend ses arguments."""
    async def defaut(args):
        return "reçu " + json.dumps(args, sort_keys=True)

    class Outil(tools.Tool):
        def prompt(self, present):
            return ""

        async def run(self, args, call):
            return tools.Result(await (run or defaut)(args))

    Outil.name, Outil.responses = name, responses
    return Outil()


def hosted_tools(result=SEARCHED, memory=None):
    """L'annuaire tools.Hosted, sans réseau : les deux outils du paquet,
    dont seul `run` est remplacé (spec, summary, render et liaisons sont
    les vrais). Chaque exécution est notée dans `runs` — (nom, arguments,
    réglages du client) — et rend `result`, un tools.Result. Mémoire
    propre à chaque annuaire : rien ne dépend de ce que la configuration
    d'exemple active."""
    runs = []

    def fake(tool):
        class Fake(type(tool)):
            enabled = True

            async def run(self, args, call):
                runs.append((self.name, args, dict(call.settings)))
                return hosted.result

        return Fake()

    hosted = tools.Hosted(
        tools=[fake(web_search.TOOL), fake(web_fetch.TOOL)],
        memory=tools.Memory(8, 60) if memory is None else memory)
    hosted.runs, hosted.result = runs, result
    return hosted


# Le décor des deux surfaces : un tour où le modèle annonce puis lance une
# recherche (arguments fragmentés), et le tour où il conclut. En flux,
# puis les mêmes en JSON.
QUERY = "{\"query\": \"llama.cpp latest release\"}"
SEARCH_TURN = (
    chunk({"content": "Je cherche."}),
    tool_call(0, "call_x", "web_search", ""),
    chunk({"tool_calls": [{"index": 0, "function": {"arguments": "{\"query\":"}}]}),
    chunk({"tool_calls": [{"index": 0, "function": {
        "arguments": " \"llama.cpp latest release\"}"}}]}),
    chunk(finish="tool_calls"),
    usage(100, 10, cached=40, reasoning=3),
)
ANSWER_TURN = (chunk({"content": "Voilà."}), chunk(finish="stop"),
               usage(150, 5, cached=100, reasoning=1))
SEARCH_DOC = chat_doc({"content": "Je cherche.", "tool_calls": [
    {"id": "call_x", "function": {"name": "web_search", "arguments": QUERY}}]},
    "tool_calls", 100, 10, cached=40)
ANSWER_DOC = chat_doc({"content": "Voilà."}, "stop", 150, 5, cached=100)


def search_then_answer(streamed=True) -> list:
    """Les réponses du backend pour ce décor, neuves à chaque appel (un
    FakeUpstream ne se lit qu'une fois)."""
    if streamed:
        return [FakeUpstream(stream(*SEARCH_TURN)),
                FakeUpstream(stream(*ANSWER_TURN))]
    return [FakeUpstream(SEARCH_DOC), FakeUpstream(ANSWER_DOC)]


def hosted_chat(monkeypatch):
    """[chat].hosted_tools actif, avec une mémoire des échanges cachés
    neuve — rendue, pour qui veut la lire."""
    from llm_proxy.surfaces import chat_api
    memory = chat_api.Memory(8, 60, 100_000)
    monkeypatch.setattr(chat_api, "ENABLED", True)
    monkeypatch.setattr(chat_api, "MEMORY", memory)
    return memory


def sans_liaison(tool):
    """Un outil sans liaison de protocole, activé par le test : déclarable
    par son nom sur chat/completions, ignoré des surfaces Responses et
    Anthropic."""
    from llm_proxy.surfaces import anthropic_api, responses_api
    h = tools.Hosted()
    assert h.for_kind(tool.name) == [tool]
    assert tool not in h.for_responses("web_search")
    assert h.for_server(f"{tool.name}_20260101") is None
    ctx = responses_api.to_chat({"model": "m", "input": "x", "tools": [
        {"type": tool.name}]}, hosted=h)[1]
    assert tool.name not in ctx.hosted and ctx.ignored == [tool.name]
    assert not anthropic_api.Context({"tools": [
        {"type": f"{tool.name}_20260101", "name": tool.name}]}, h).hosted


# ── outils qui téléchargent : DNS, web, backend, PDF ────────────────────

def rendu(coro) -> tools.Result:
    """Ce que l'exécuteur (tools.Hosted) fait d'un `run` appelé à la
    main : son Result, ou celui de l'échec prévu (ToolError)."""
    try:
        return go(coro)
    except tools.ToolError as exc:
        return tools.failure(exc.code, exc.message)


def fake_dns(monkeypatch, table: dict) -> dict:
    """Résolution sans réseau. `table` : nom → adresses (None :
    introuvable ; une fonction : un nom qui change de réponse). Hors
    table, seul un littéral NUMÉRIQUE est résolu, par le vrai getaddrinfo
    et AI_NUMERICHOST (aucune requête DNS) : c'est lui qui dit ce que
    valent «2130706433» ou «0x7f.1». Les noms demandés sont notés dans
    table["__seen__"] ; la table rendue peut être complétée par le test."""
    real = socket.getaddrinfo
    seen = []

    def fake(host, port, family=0, type=0, proto=0, flags=0):
        seen.append(host)
        if host in table:
            ips = table[host]
            if callable(ips):
                ips = ips()
            if ips is None:
                raise socket.gaierror(socket.EAI_NONAME, "introuvable")
            return [(socket.AF_INET6 if ":" in ip else socket.AF_INET,
                     socket.SOCK_STREAM, 6, "",
                     (ip, port, 0, 0) if ":" in ip else (ip, port))
                    for ip in ips]
        return real(host, port, family, type, proto,
                    flags | socket.AI_NUMERICHOST)

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    table["__seen__"] = seen
    return table


def net_guard(monkeypatch, blocked=()):
    """Le garde-fou commun ([tools.net]) dans un état connu, quel que soit
    le fichier de configuration chargé à l'import."""
    monkeypatch.setattr(net, "ALLOW_PRIVATE", False)
    monkeypatch.setattr(net, "ALLOWED_DOMAINS", [])
    monkeypatch.setattr(net, "BLOCKED_DOMAINS", list(blocked))


class FakeWeb:
    """Un faux web : `pages[(hôte, chemin)]` — ou `pages[chemin]`, quel
    que soit l'hôte — → réponse (ou fonction de la requête) ; sinon
    `default`, sinon 404. Garde chaque requête reçue, pour vérifier où
    elle est partie."""

    def __init__(self, pages=None, default=None):
        self.pages, self.default, self.requests = pages or {}, default, []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.path
        page = self.pages.get((request.headers["host"], path),
                              self.pages.get(path, self.default))
        if page is None:
            return httpx2.Response(404, text="rien")
        return page(request) if callable(page) else page

    @property
    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handler)


def fake_backend(monkeypatch, name, conf, handler):
    """Un backend `name` du proxy dont le client HTTP parle à `handler`
    (un httpx2.MockTransport) ; rendu, pour que le test le règle."""
    from llm_proxy.upstream import backends
    b = backends.Backend(name, conf)
    b.client = httpx2.AsyncClient(base_url=b.url,
                                 transport=httpx2.MockTransport(handler))
    monkeypatch.setitem(backends.BACKENDS, name, b)
    return b


def quotas(monkeypatch, backend, accepted=1) -> list:
    """Un backend à quotas dont le limiteur accepte `accepted` requêtes,
    puis répond « quota épuisé pour 20 min ». Rendu : le coût de chaque
    demande au limiteur."""
    from llm_proxy.upstream import albert
    taken = []

    class Limiter:
        async def acquire(self, cost, wait_gone=None):
            taken.append(cost)
            if len(taken) > accepted:
                raise albert.QuotaWaitTooLong(1200, "minute")
            return 0.0

    monkeypatch.setattr(backend, "quotas", True)
    monkeypatch.setattr(backend, "quota_state", type("Q", (), {
        "get_limiter": lambda self, payload: Limiter()})())
    return taken


def pdf_bytes(objs: list[bytes]) -> bytes:
    """Un PDF autour de ses objets, numérotés à partir de 1 (le premier :
    le catalogue) : en-tête, objets, table des positions, trailer."""
    out, places = bytearray(b"%PDF-1.4\n"), []
    for i, obj in enumerate(objs, 1):
        places.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, obj)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % place for place in places)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1, xref)
    return bytes(out)


def everywhere(monkeypatch, module, name: str, value) -> None:
    """monkeypatch.setattr d'un réglage dans un module ET, si c'est un
    paquet découpé en parties (`from .common import ENABLED`), dans chacune
    de ses parties qui le détient : chacune en a sa propre liaison."""
    import pkgutil
    import sys
    monkeypatch.setattr(module, name, value)
    for info in pkgutil.iter_modules(getattr(module, "__path__", []) or []):
        sub = sys.modules.get(f"{module.__name__}.{info.name}")
        if sub is not None and hasattr(sub, name):
            monkeypatch.setattr(sub, name, value)
