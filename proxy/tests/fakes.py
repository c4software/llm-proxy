"""Ce que les tests des deux surfaces traduites (test_responses_api.py,
test_anthropic_api.py) ont en commun : de quoi fabriquer un flux upstream
chat/completions, le passer à un robinet, relire ce qu'il rend, et de
faux outils hébergés. Aucun réseau. Des fonctions simples ; la fixture
`proxy`, qui monte l'application dessus, est dans conftest.py."""

import json

import conftest  # noqa: F401 — pose CONFIG_PATH avant tout import du paquet

from llm_proxy import tools
from llm_proxy.tools import web_fetch, web_search


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
    """La réponse ouverte que rend app.send_upstream. Sans `content_type`,
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
