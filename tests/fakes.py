"""Ce que les tests des deux surfaces traduites (test_responses_api.py,
test_anthropic_api.py) ont en commun : de quoi fabriquer un flux upstream
chat/completions, le passer à un robinet, relire ce qu'il rend, et de
faux outils hébergés. Aucun réseau. Des fonctions simples ; la fixture
`proxy`, qui monte l'application dessus, est dans conftest.py."""

import json
import types

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
# Le texte que le modèle lit pour ces deux résultats.
FOUND = web_search.render("llama.cpp latest release", RESULTS)


def hosted_tools(result: str = FOUND, memory=None):
    """L'annuaire tools.Hosted, sans réseau : les deux modules du paquet,
    dont seul `run` est remplacé (leurs fonctions de texte sont les
    vraies). Chaque exécution est notée dans `runs` — (nom, arguments,
    options du client) — et rend `result`. Mémoire propre à chaque
    annuaire : rien ne dépend de ce que la configuration d'exemple active."""
    runs = []

    def fake(module):
        async def run(args, **options):
            runs.append((module.NAME, args, options))
            return hosted.result

        kept = ("NAME", "KINDS", "ITEM_TYPE", "DEFINITION", "definition",
                "action", "parse", "render")
        return types.SimpleNamespace(ENABLED=True, run=run, **{
            k: getattr(module, k) for k in kept if hasattr(module, k)})

    hosted = tools.Hosted(
        modules=[fake(web_search), fake(web_fetch)],
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
