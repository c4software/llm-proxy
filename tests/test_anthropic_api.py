"""Le traducteur Anthropic ↔ OpenAI, testé sur des octets : aucun
réseau, aucun serveur — anthropic_api ne connaît ni FastAPI ni httpx.
La recherche hébergée (outil serveur `web_search_…`) est testée en fin
de fichier, jusqu'à la route entière par le client de test de Starlette."""

import asyncio
import json
import types

import httpx
import pytest
from fastapi.testclient import TestClient

from llm_proxy import anthropic_api as A
from llm_proxy import app
from llm_proxy import tools
from llm_proxy.backends import Backend
from llm_proxy.tools import web_search

BACKENDS = {"albert": None, "bigchuck": None}


# ── modèles ─────────────────────────────────────────────────────────────

def test_resolve_model_uses_map_then_default():
    assert A.resolve_model("claude-opus-5", BACKENDS) == "albert/deepseek-v4-flash"
    assert A.resolve_model("Claude-Haiku-4-5", BACKENDS) == "albert/deepseek-v4-flash"
    # Suffixe de contexte ignoré.
    assert A.resolve_model("claude-opus-5[1m]", BACKENDS) == "albert/deepseek-v4-flash"
    # Nom inconnu → default.
    assert A.resolve_model("gpt-9", BACKENDS) == A.MODEL_MAP["default"]
    # Déjà préfixé par un backend connu : tel quel.
    assert A.resolve_model("bigchuck/qwen3-32b", BACKENDS) == "bigchuck/qwen3-32b"


def test_resolve_model_without_default(monkeypatch):
    monkeypatch.setattr(A, "MODEL_MAP", {"claude-opus-5": "albert/x"})
    assert A.resolve_model("claude-opus-5", BACKENDS) == "albert/x"
    assert A.resolve_model("whatever", BACKENDS) is None
    assert A.resolve_model("", BACKENDS) is None


# ── requête ─────────────────────────────────────────────────────────────

def test_to_openai_system_and_text():
    out = A.to_openai({
        "model": "albert/m", "max_tokens": 100, "temperature": 0.2,
        "stop_sequences": ["END"], "top_k": 5, "thinking": {"type": "adaptive"},
        "system": [{"type": "text", "text": "Sois bref.",
                    "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": "Salut"}],
        "metadata": {"user_id": "u1"},
    })
    assert out["messages"] == [
        {"role": "system", "content": "Sois bref."},
        {"role": "user", "content": "Salut"},
    ]
    assert out["max_tokens"] == 100 and out["temperature"] == 0.2
    assert out["stop"] == ["END"] and out["user"] == "u1"
    assert "top_k" not in out and "thinking" not in out and "stream" not in out


def test_to_openai_stream_asks_for_usage():
    out = A.to_openai({"model": "m", "stream": True, "messages": []})
    assert out["stream"] is True
    assert out["stream_options"] == {"include_usage": True}


def test_to_openai_tools_and_tool_choice():
    out = A.to_openai({
        "model": "m", "messages": [],
        "tools": [
            {"name": "get_weather", "description": "Météo",
             "input_schema": {"type": "object", "properties": {}}},
            {"type": "web_search_20260209", "name": "web_search"},  # serveur : ignoré
        ],
        "tool_choice": {"type": "any", "disable_parallel_tool_use": True},
    })
    assert out["tools"] == [{"type": "function", "function": {
        "name": "get_weather", "description": "Météo",
        "parameters": {"type": "object", "properties": {}}}}]
    assert out["tool_choice"] == "required"
    assert out["parallel_tool_calls"] is False


@pytest.mark.parametrize("choice,expected", [
    ({"type": "auto"}, "auto"),
    ({"type": "none"}, "none"),
    ({"type": "tool", "name": "f"}, {"type": "function", "function": {"name": "f"}}),
])
def test_tool_choice_values(choice, expected):
    out = A.to_openai({"model": "m", "messages": [],
                       "tools": [{"name": "f", "input_schema": {}}],
                       "tool_choice": choice})
    assert out["tool_choice"] == expected


def test_to_openai_tool_round_trip():
    """assistant(tool_use) + user(tool_result ×2 + texte) → assistant
    avec tool_calls, deux messages `tool`, PUIS le texte user."""
    out = A.to_openai({"model": "m", "messages": [
        {"role": "user", "content": "Météo à Paris et Lyon ?"},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "…", "signature": "x"},
            {"type": "text", "text": "Je regarde."},
            {"type": "tool_use", "id": "toolu_1", "name": "w", "input": {"c": "Paris"}},
            {"type": "tool_use", "id": "toolu_2", "name": "w", "input": {"c": "Lyon"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "20°C"},
            {"type": "tool_result", "tool_use_id": "toolu_2", "is_error": True,
             "content": [{"type": "text", "text": "timeout"}]},
            {"type": "text", "text": "Merci, et demain ?"},
        ]},
    ]})
    msgs = out["messages"]
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["content"] == "Je regarde."
    assert [tc["id"] for tc in msgs[1]["tool_calls"]] == ["toolu_1", "toolu_2"]
    assert json.loads(msgs[1]["tool_calls"][0]["function"]["arguments"]) == {"c": "Paris"}
    assert msgs[2] == {"role": "tool", "tool_call_id": "toolu_1", "content": "20°C"}
    assert msgs[3] == {"role": "tool", "tool_call_id": "toolu_2", "content": "Error: timeout"}
    assert msgs[4] == {"role": "user", "content": "Merci, et demain ?"}
    assert "thinking" not in json.dumps(out)


def test_to_openai_mid_conversation_system_folds_into_next_user():
    """Un system en cours de conversation (rappels de Claude Code) n'est
    pas relayé tel quel — les gabarits Qwen/Mistral le refusent — mais
    fondu en tête du user suivant ; après des tool_result seuls, il
    suit en message user ; en dernière position, il devient un user."""
    out = A.to_openai({"model": "m", "messages": [
        {"role": "user", "content": "Salut"},
        {"role": "assistant", "content": "Oui ?"},
        {"role": "system", "content": "Rappel : sois bref."},
        {"role": "user", "content": [{"type": "text", "text": "Question"}]},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t", "name": "f", "input": {}}]},
        {"role": "system", "content": [{"type": "text", "text": "Rappel 2"}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t", "content": "ok"}]},
        {"role": "system", "content": "Rappel final"},
    ]})["messages"]
    assert [m["role"] for m in out] == [
        "user", "assistant", "user", "assistant", "tool", "user", "user"]
    assert out[2]["content"] == "Rappel : sois bref.\n\nQuestion"
    assert out[5] == {"role": "user", "content": "Rappel 2"}
    assert out[6] == {"role": "user", "content": "Rappel final"}


def test_to_openai_assistant_only_tools_has_null_content():
    out = A.to_openai({"model": "m", "messages": [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t", "name": "f", "input": {}}]}]})
    assert out["messages"][0]["content"] is None


IMG = {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                   "data": "A" * 4096}}


def test_to_openai_image_base64_when_backend_accepts():
    out = A.to_openai({"model": "m", "messages": [{"role": "user", "content": [
        IMG, {"type": "text", "text": "Quoi ?"},
    ]}]}, images=True)
    parts = out["messages"][0]["content"]
    assert parts[0] == {"type": "image_url",
                        "image_url": {"url": "data:image/png;base64," + "A" * 4096}}
    assert parts[1] == {"type": "text", "text": "Quoi ?"}


def test_to_openai_image_placeholder_when_text_only_backend():
    out = A.to_openai({"model": "m", "messages": [{"role": "user", "content": [
        IMG, {"type": "text", "text": "Quoi ?"},
    ]}]})
    # Tout est texte → une chaîne, la forme que tout backend accepte.
    assert out["messages"][0]["content"] == "[image ignorée : image/png, 3 Ko]Quoi ?"


def test_to_openai_tool_result_with_image():
    """Claude Code lit un .png : le tool_result porte une image. Le
    message `tool` reste texte ; l'image suit dans un message user."""
    msgs = {"model": "m", "messages": [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1",
         "content": [{"type": "text", "text": "lu"}, IMG]},
    ]}]}
    out = A.to_openai(msgs, images=True)["messages"]
    assert out[0] == {"role": "tool", "tool_call_id": "t1", "content": "lu"}
    assert out[1]["role"] == "user"
    assert out[1]["content"][0] == {"type": "text", "text": "[résultat de l'outil t1]"}
    assert out[1]["content"][1]["type"] == "image_url"
    # Backend texte seul : l'image devient un mot dans le message `tool`
    # lui-même — rien ne suit.
    out = A.to_openai(msgs)["messages"]
    assert out == [{"role": "tool", "tool_call_id": "t1",
                    "content": "lu[image ignorée : image/png, 3 Ko]"}]


def test_has_images():
    assert not A.has_images({"messages": [{"role": "user", "content": "x"}]})
    assert A.has_images({"messages": [{"role": "user", "content": [IMG]}]})
    assert A.has_images({"messages": [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t", "content": [IMG]}]}]})


def test_to_openai_documents():
    out = A.to_openai({"model": "m", "messages": [{"role": "user", "content": [
        {"type": "document", "source": {"type": "text", "media_type": "text/plain",
                                        "data": "contenu"}},
        {"type": "document", "source": {"type": "base64",
                                        "media_type": "application/pdf",
                                        "data": "B" * 2048}},
    ]}]})
    assert out["messages"][0]["content"] == \
        "contenu[document ignoré : application/pdf, 1 Ko]"


# ── réponse non streamée ────────────────────────────────────────────────

def test_from_openai_text():
    msg = A.from_openai({
        "id": "chatcmpl-1",
        "choices": [{"message": {"role": "assistant", "content": "Bonjour"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3},
    }, "albert/m")
    assert msg["id"] == "chatcmpl-1" and msg["model"] == "albert/m"
    assert msg["content"] == [{"type": "text", "text": "Bonjour"}]
    assert msg["stop_reason"] == "end_turn"
    assert msg["usage"]["input_tokens"] == 12
    assert msg["usage"]["output_tokens"] == 3


def test_from_openai_cached_tokens_split_like_anthropic():
    """OpenAI : prompt_tokens INCLUT le cache ; Anthropic : input_tokens
    l'EXCLUT, cache_read_input_tokens le porte. Le total reste égal."""
    msg = A.from_openai({"choices": [{"message": {"content": "x"},
                                      "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 20000, "completion_tokens": 5,
                                   "prompt_tokens_details": {"cached_tokens": 18000}}}, "m")
    assert msg["usage"]["input_tokens"] == 2000
    assert msg["usage"]["cache_read_input_tokens"] == 18000


def test_from_openai_tool_calls_and_length():
    msg = A.from_openai({"choices": [{"message": {
        "content": None,
        "tool_calls": [{"id": "call_1", "type": "function", "function": {
            "name": "w", "arguments": '{"c": "Paris"}'}}],
    }, "finish_reason": "tool_calls"}]}, "m")
    assert msg["content"] == [{"type": "tool_use", "id": "call_1", "name": "w",
                               "input": {"c": "Paris"}}]
    assert msg["stop_reason"] == "tool_use"
    # Arguments illisibles → {} plutôt qu'une exception.
    msg = A.from_openai({"choices": [{"message": {"tool_calls": [
        {"id": "c", "function": {"name": "w", "arguments": "{oops"}}]},
        "finish_reason": "length"}]}, "m")
    assert msg["content"][0]["input"] == {}
    assert msg["stop_reason"] == "max_tokens"


def test_from_openai_reasoning_becomes_thinking(monkeypatch):
    doc = {"choices": [{"message": {"reasoning_content": "hmm", "content": "ok"},
                        "finish_reason": "stop"}]}
    msg = A.from_openai(doc, "m")
    assert msg["content"][0] == {"type": "thinking", "thinking": "hmm", "signature": ""}
    assert msg["content"][1] == {"type": "text", "text": "ok"}
    monkeypatch.setattr(A, "REASONING_AS_THINKING", False)
    assert A.from_openai(doc, "m")["content"] == [{"type": "text", "text": "ok"}]


# ── le robinet ──────────────────────────────────────────────────────────

def events(raw: bytes) -> list[tuple[str, dict]]:
    out = []
    for block in raw.decode().split("\n\n"):
        if not block.strip():
            continue
        lines = dict(l.split(": ", 1) for l in block.split("\n"))
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def sse(*docs) -> bytes:
    return b"".join(b"data: " + json.dumps(d).encode() + b"\n\n" for d in docs)


def test_translator_json_mode():
    t = A.Translator(200, "application/json", "albert/m")
    assert t.feed(b'{"id":"x","choices":[{"message":{"content":"Hi"},') == b""
    assert t.feed(b'"finish_reason":"stop"}],"usage":{"prompt_tokens":5,'
                  b'"completion_tokens":1}}') == b""
    body = json.loads(t.finish())
    assert body["type"] == "message" and body["content"][0]["text"] == "Hi"
    assert t.tokens(999) == (5, 1, True)
    assert t.sse is False


def test_translator_error_mode():
    t = A.Translator(400, "application/json", "m")
    t.feed(b'{"error": {"message": "bad model", "type": "invalid_request_error"}}')
    body = json.loads(t.finish())
    assert body == {"type": "error", "error": {
        "type": "invalid_request_error", "message": "bad model"}}
    # Statut sans corps JSON.
    t = A.Translator(503, "text/html", "m")
    t.feed(b"<h1>gateway</h1>")
    assert json.loads(t.finish())["error"]["type"] == "api_error"


def test_translator_stream_text():
    t = A.Translator(200, "text/event-stream", "albert/m")
    raw = t.feed(sse(
        {"id": "c1", "choices": [{"delta": {"role": "assistant", "content": ""}}]},
        {"id": "c1", "choices": [{"delta": {"content": "Bon"}}]},
    ))
    raw += t.feed(sse({"id": "c1", "choices": [{"delta": {"content": "jour"}}]}))
    raw += t.feed(sse(
        {"id": "c1", "choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"id": "c1", "choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 2}},
    ))
    raw += t.feed(b"data: [DONE]\n\n")
    raw += t.finish()
    ev = events(raw)
    assert [e for e, _ in ev] == [
        "message_start", "content_block_start", "content_block_delta",
        "content_block_delta", "content_block_stop", "message_delta",
        "message_stop",
    ]
    assert ev[0][1]["message"]["id"] == "c1"
    assert ev[0][1]["message"]["model"] == "albert/m"
    assert ev[1][1]["content_block"] == {"type": "text", "text": ""}
    assert ev[2][1]["delta"] == {"type": "text_delta", "text": "Bon"}
    assert ev[5][1]["delta"]["stop_reason"] == "end_turn"
    assert ev[5][1]["usage"]["output_tokens"] == 2
    assert ev[5][1]["usage"]["input_tokens"] == 7
    assert t.tokens(0) == (7, 2, True)
    assert t.sse is True


def test_translator_stream_tools_fragmented():
    """Deux outils, id/name sur le premier fragment seulement, arguments
    par morceaux, pas de texte : pas de bloc texte vide, deux blocs
    tool_use, stop_reason tool_use."""
    t = A.Translator(200, "text/event-stream", "m")
    raw = t.feed(sse(
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_a",
            "function": {"name": "w", "arguments": ""}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0,
            "function": {"arguments": '{"c":'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0,
            "function": {"arguments": '"Paris"}'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 1, "id": "call_b",
            "function": {"name": "w", "arguments": '{"c":"Lyon"}'}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ))
    raw += t.finish()
    ev = events(raw)
    kinds = [e for e, _ in ev]
    assert kinds == [
        "message_start",
        "content_block_start", "content_block_delta", "content_block_delta",
        "content_block_stop",
        "content_block_start", "content_block_delta", "content_block_stop",
        "message_delta", "message_stop",
    ]
    assert ev[1][1]["content_block"] == {"type": "tool_use", "id": "call_a",
                                         "name": "w", "input": {}}
    assert ev[1][1]["index"] == 0 and ev[5][1]["index"] == 1
    assert "".join(d["delta"]["partial_json"] for e, d in ev
                   if e == "content_block_delta" and d["index"] == 0) == '{"c":"Paris"}'
    assert ev[5][1]["content_block"]["id"] == "call_b"
    assert ev[8][1]["delta"]["stop_reason"] == "tool_use"


def test_translator_stream_text_then_tool_closes_text_first():
    t = A.Translator(200, "text/event-stream", "m")
    raw = t.feed(sse(
        {"choices": [{"delta": {"content": "Je regarde."}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c",
            "function": {"name": "w", "arguments": "{}"}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    )) + t.finish()
    kinds = [e for e, _ in events(raw)]
    assert kinds[:5] == ["message_start", "content_block_start",
                         "content_block_delta", "content_block_stop",
                         "content_block_start"]


def test_translator_stream_reasoning():
    t = A.Translator(200, "text/event-stream", "m")
    raw = t.feed(sse(
        {"choices": [{"delta": {"reasoning_content": "hmm"}}]},
        {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]},
    )) + t.finish()
    ev = events(raw)
    assert ev[1][1]["content_block"]["type"] == "thinking"
    assert ev[2][1]["delta"] == {"type": "thinking_delta", "thinking": "hmm"}
    assert ev[4][1]["content_block"]["type"] == "text"


def test_translator_stream_split_across_chunks():
    """Un événement coupé en plein milieu par la segmentation TCP."""
    t = A.Translator(200, "text/event-stream", "m")
    whole = sse({"choices": [{"delta": {"content": "coupé"}}]})
    raw = t.feed(whole[:10]) + t.feed(whole[10:]) + t.finish()
    deltas = [d for e, d in events(raw) if e == "content_block_delta"]
    assert deltas == [{"type": "content_block_delta", "index": 0,
                       "delta": {"type": "text_delta", "text": "coupé"}}]


def test_translator_stream_error_mid_flow():
    t = A.Translator(200, "text/event-stream", "m")
    raw = t.feed(sse(
        {"choices": [{"delta": {"content": "a"}}]},
        {"error": {"message": "boom", "type": "server_error"}},
    )) + t.finish()
    ev = events(raw)
    assert ("error", {"type": "error", "error": {"type": "api_error",
                                                  "message": "boom"}}) in ev
    assert ev[-1][0] == "message_stop"


def test_translator_stream_without_usage_estimates():
    t = A.Translator(200, "text/event-stream", "m")
    t.feed(sse({"choices": [{"delta": {"content": "x" * 40},
                             "finish_reason": "stop"}]}))
    t.finish()
    assert t.tokens(123) == (123, 10, False)


def test_translator_stream_empty_upstream():
    """Upstream qui ferme sans rien envoyer : un message vide mais
    complet, jamais un flux tronqué."""
    t = A.Translator(200, "text/event-stream", "m")
    assert [e for e, _ in events(t.finish())] == [
        "message_start", "message_delta", "message_stop"]


# ── divers ──────────────────────────────────────────────────────────────

def test_estimate_tokens():
    n = A.estimate_tokens({"system": "x" * 400, "messages": [], "stream": True})
    assert 100 <= n <= 120
    # prompt_text : ce qui part à /tokenize — sans les champs hors prompt.
    assert "stream" not in A.prompt_text({"system": "s", "stream": True})


def test_models_list():
    out = A.models_list([{"id": "albert/m", "created": 1_700_000_000},
                         {"id": "bigchuck/q", "created": 0}])
    assert out["data"][0] == {"type": "model", "id": "albert/m",
                              "display_name": "albert/m",
                              "created_at": "2023-11-14T22:13:20Z"}
    assert out["first_id"] == "albert/m" and out["last_id"] == "bigchuck/q"
    assert out["has_more"] is False


def test_ping_and_sse_error():
    assert A.ping_event() == b'event: ping\ndata: {"type": "ping"}\n\n'
    ev = events(A.sse_error(A.error_body("trop", "rate_limit_error")))
    assert ev == [("error", {"type": "error", "error": {
        "type": "rate_limit_error", "message": "trop"}})]


def test_error_type():
    assert A.error_type(429) == "rate_limit_error"
    assert A.error_type(418) == "invalid_request_error"
    assert A.error_type(502) == "api_error"


def test_mid_conversation_system_keeps_its_place_across_turns():
    """Un rappel system qui ferme la requête N doit rester à la même place
    dans la requête N+1 (avant l'assistant qui le suit), sinon le préfixe
    rendu diverge et le cache de préfixe du backend est perdu."""
    tu = {"type": "tool_use", "id": "t1", "name": "Read", "input": {"p": "a"}}
    tr = {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}
    turn_n = [
        {"role": "user", "content": "Lis a"},
        {"role": "assistant", "content": [tu]},
        {"role": "user", "content": [tr]},
        {"role": "system", "content": "<total_tokens>1</total_tokens>"},
    ]
    tu2 = dict(tu, id="t2")
    tr2 = dict(tr, tool_use_id="t2")
    turn_n1 = turn_n + [
        {"role": "assistant", "content": [tu2]},
        {"role": "user", "content": [tr2]},
        {"role": "system", "content": "<total_tokens>2</total_tokens>"},
    ]
    a = A.to_openai({"model": "m", "messages": turn_n})["messages"]
    b = A.to_openai({"model": "m", "messages": turn_n1})["messages"]
    assert b[:len(a)] == a
    assert a[-1] == {"role": "user", "content": "<total_tokens>1</total_tokens>"}
    assert b[len(a)]["role"] == "assistant"


# ── recherche hébergée ──────────────────────────────────────────────────
# L'outil serveur `web_search_…` d'un client Anthropic, exécuté par le
# proxy. Aucun réseau : le module de tools/ avec un `run` de remplacement
# (ses fonctions de texte, elles, sont les vraies), et une mémoire propre
# à chaque test — qui doit rester vide, cette surface n'y range rien.

RESULTS = [
    {"title": "Releases · ggml-org/llama.cpp", "date": "2026-10-03",
     "url": "https://github.com/ggml-org/llama.cpp/releases",
     "snippet": "LLM inference in C/C++ — b6789, «latest»…"},
    {"title": "llama.cpp (blog)", "date": "",
     "url": "https://example.org/blog/llama", "snippet": ""},
]
FOUND = web_search.render("llama.cpp latest release", RESULTS)
BLOCKS = [
    {"type": "web_search_result", "title": "Releases · ggml-org/llama.cpp",
     "url": "https://github.com/ggml-org/llama.cpp/releases",
     "encrypted_content": "LLM inference in C/C++ — b6789, «latest»…",
     "page_age": "2026-10-03"},
    {"type": "web_search_result", "title": "llama.cpp (blog)",
     "url": "https://example.org/blog/llama", "encrypted_content": "",
     "page_age": None},
]


def search_tool(run=None, name="web_search"):
    async def found(args, **options):
        return FOUND

    return types.SimpleNamespace(
        NAME=name, KINDS=web_search.KINDS, ITEM_TYPE=web_search.ITEM_TYPE,
        ENABLED=True, DEFINITION=web_search.DEFINITION,
        definition=web_search.definition, action=web_search.action,
        parse=web_search.parse, render=web_search.render, run=run or found)


def hosted_tools(run=None):
    # `web_fetch` est actif lui aussi, comme sur un proxy réel : il ne doit
    # jamais être présenté sur cette surface.
    return tools.Hosted(modules=[search_tool(run), search_tool(name="web_fetch")],
                        memory=tools.Memory(8, 60))


def claude_code_search(**extra):
    """Le corps réel de la sous-requête que l'outil WebSearch de Claude
    Code 2.1.287 envoie (capture du 05/10/2026, `metadata` raccourci)."""
    return {
        "model": "bigchuck/qwen3.8-flash-next",
        "messages": [{"role": "user", "content": [{
            "type": "text",
            "text": "Perform a web search for the query: llama.cpp latest release"}]}],
        "system": [
            {"type": "text", "text": "x-anthropic-billing-header: "
                                     "cc_version=2.1.287.d35; cc_entrypoint=sdk-cli;"},
            {"type": "text", "text": "You are a Claude agent, built on "
                                     "Anthropic's Claude Agent SDK."},
            {"type": "text", "text": "You are an assistant for performing a "
                                     "web search tool use"}],
        "tools": [{"type": "web_search_20250305", "name": "web_search",
                   "max_uses": 8}],
        "tool_choice": {"type": "auto"},
        "metadata": {"user_id": "{\"device_id\":\"12d6\",\"session_id\":\"3bf1\"}"},
        "max_tokens": 32000, "output_config": {"effort": "high"},
        "stream": True, **extra}


def chunk(delta=None, finish=None, **extra):
    return {"id": "c1", "choices": [{"delta": delta or {},
                                     "finish_reason": finish}], **extra}


def tool_call(index, call_id, name, arguments):
    return chunk({"tool_calls": [{"index": index, "id": call_id, "function": {
        "name": name, "arguments": arguments}}]})


def usage(prompt, completion, cached=0):
    return {"choices": [], "usage": {
        "prompt_tokens": prompt, "completion_tokens": completion,
        "prompt_tokens_details": {"cached_tokens": cached}}}


def turn(t, *docs, cut: int = 7):
    """Un tour upstream entier passé au robinet `t`, finish() compris."""
    stream = sse(*docs) + b"data: [DONE]\n\n"
    out = b"".join(t.feed(stream[i:i + cut]) for i in range(0, len(stream), cut))
    return events(out + t.finish())


QUERY = "{\"query\": \"llama.cpp latest release\"}"
SEARCH_TURN = (
    chunk({"content": "Je cherche."}),
    tool_call(0, "call_x", "web_search", ""),
    chunk({"tool_calls": [{"index": 0, "function": {"arguments": "{\"query\":"}}]}),
    chunk({"tool_calls": [{"index": 0, "function": {
        "arguments": " \"llama.cpp latest release\"}"}}]}),
    chunk(finish="tool_calls"),
    usage(100, 10, cached=40),
)
ANSWER_TURN = (chunk({"content": "b6789."}), chunk(finish="stop"),
               usage(150, 5, cached=100))


def translator(request=None, hosted=None, content_type="text/event-stream"):
    request = request or claude_code_search()
    ctx = A.Context(request, hosted or hosted_tools())
    return A.Translator(200, content_type, request["model"], ctx)


def test_search_text_and_structure_say_the_same_thing():
    """`parse` est l'inverse de `render` : c'est ce qui permet de tirer
    les blocs du client du texte du modèle, et l'inverse au rejeu."""
    assert web_search.parse(FOUND) == RESULTS
    assert web_search.render("q", web_search.parse(FOUND)) == FOUND
    assert FOUND.split("\n") == [
        "[1] Releases · ggml-org/llama.cpp (2026-10-03)",
        "    https://github.com/ggml-org/llama.cpp/releases",
        "    LLM inference in C/C++ — b6789, «latest»…",
        "[2] llama.cpp (blog)",
        "    https://example.org/blog/llama"]
    # Ni une erreur, ni «aucun résultat», ni la marque de troncature ne
    # sont des résultats ; une entrée coupée avant son URL est écartée.
    assert web_search.parse("Error: search engine returned HTTP 500.") == []
    assert web_search.parse(web_search.render("q", [])) == []
    assert web_search.parse(FOUND + "\n[3] coupé\n[truncated]") == RESULTS
    # Un titre qui finit de lui-même par une date est lu comme daté, une
    # date d'une autre forme reste dans le titre : dans les deux cas le
    # texte, lui, revient à l'identique.
    for title, date in (("Notes (2024-05-01)", ""), ("Notes", "Jan 5, 202")):
        odd = web_search.render("q", [{"title": title, "date": date,
                                       "url": "https://x.test", "snippet": "s"}])
        assert web_search.render("q", web_search.parse(odd)) == odd
    assert web_search.parse(odd)[0]["title"] == "Notes (Jan 5, 202)"
    # La même liste que format_results tire des résultats bruts de SearXNG.
    raw = [{"title": " Un  titre ", "url": "https://a.test/x",
            "publishedDate": "2026-01-02T03:04:05", "content": " du\ntexte "},
           {"title": "sans url"}]
    assert web_search.entries(raw, 5) == [{
        "title": "Un titre", "url": "https://a.test/x", "date": "2026-01-02",
        "snippet": "du texte"}]
    assert web_search.format_results("q", raw, 5) == web_search.render(
        "q", web_search.entries(raw, 5))


def test_search_domain_filters():
    raw = [{"title": str(n), "url": u} for n, u in enumerate([
        "https://github.com/ggml-org/llama.cpp",
        "https://docs.github.com/en/rest",
        "https://notgithub.com/x",
        "https://example.org/blog/post-1",
        "https://example.org/shop",
    ])]
    urls = lambda **kw: [e["url"] for e in web_search.entries(raw, 20, **kw)]
    # Sous-domaines couverts, pas les homonymes ; un sous-domaine précis ne
    # couvre pas son parent ; un chemin restreint à ce qui le prolonge.
    assert urls(allowed=["github.com"]) == [raw[0]["url"], raw[1]["url"]]
    assert urls(allowed=["docs.github.com"]) == [raw[1]["url"]]
    assert urls(allowed=["example.org/blog"]) == [raw[3]["url"]]
    assert urls(allowed=["https://Example.org/blog/*"]) == [raw[3]["url"]]
    assert urls(blocked=["github.com", "example.org/shop"]) == [
        raw[2]["url"], raw[3]["url"]]
    # Le filtre passe AVANT la limite.
    assert [e["url"] for e in web_search.entries(
        raw, 1, allowed=["example.org"])] == [raw[3]["url"]]

    def handler(request):
        assert request.url.params["q"] == "x"   # la requête n'est pas réécrite
        return httpx.Response(200, json={"results": raw})

    go = lambda **kw: asyncio.run(web_search.run(
        {"query": "x"}, transport=httpx.MockTransport(handler), **kw))
    old = web_search.SEARXNG_URL
    web_search.SEARXNG_URL = "http://searxng.test"
    try:
        assert go(allowed_domains=["example.org"], blocked_domains=[
            "example.org/shop"]) == "[1] 3\n    https://example.org/blog/post-1"
        assert go(allowed_domains=["nulle-part.test"]) == "No results for «x»."
        assert go().count("https://") == 5
    finally:
        web_search.SEARXNG_URL = old


def test_hosted_run_passes_client_options_and_limit(monkeypatch):
    seen = []

    async def run(args, **options):
        seen.append((args, options))
        return "ok"

    h = hosted_tools(run)
    go = lambda *a, **kw: asyncio.run(h.run("web_search", "{\"query\":\"x\"}",
                                            *a, **kw))
    assert go(0) == "ok" and seen == [({"query": "x"}, {})]
    opts = {"web_search": {"allowed_domains": ["a.test"]}, "autre": {"x": 1}}
    assert go(0, options=opts) == "ok"
    assert seen[1] == ({"query": "x"}, {"allowed_domains": ["a.test"]})
    # La limite du client ne fait que BAISSER celle du proxy.
    monkeypatch.setattr(tools, "MAX_CALLS", 3)
    assert (h.cap(), h.cap(2), h.cap(99), h.cap(0), h.cap(-1), h.cap("2"),
            h.cap(True)) == (3, 2, 3, 0, 0, 3, 3)
    assert go(1, limit=2) == "ok"
    assert go(2, limit=2).startswith("Error: the limit of 2 web tool calls")
    assert go(2, limit=99) == "ok" and go(3, limit=99).startswith(
        "Error: the limit of 3 ")


def test_to_openai_declares_hosted_search_from_claude_code_request():
    hosted = hosted_tools()
    request = claude_code_search()
    out = A.to_openai(request, hosted=hosted)
    # La fonction du paquet tools/ à la place de l'outil serveur : la même
    # que sur la surface Responses, moins le renvoi à web_fetch — qui
    # n'est PAS présenté (Claude Code lit les pages chez le client).
    assert out["tools"] == [web_search.definition(fetch=False)]
    f = out["tools"][0]["function"]
    assert f["name"] == "web_search" and "web_fetch" not in json.dumps(out)
    assert f["parameters"] == web_search.DEFINITION["function"]["parameters"]
    assert f["description"] == web_search.DEFINITION["function"][
        "description"].replace("Use web_fetch to read a result page. ", "")
    assert out["tool_choice"] == "auto" and out["stream"] is True
    assert out["messages"][0]["role"] == "system"
    assert out["messages"][1] == {
        "role": "user",
        "content": "Perform a web search for the query: llama.cpp latest release"}
    ctx = A.Context(request, hosted)
    assert list(ctx.hosted) == ["web_search"] and ctx.limit == 8
    assert ctx.options == {}
    assert ctx.hosted["web_search"] is hosted.by_name["web_search"]

    # Sans annuaire, ou annuaire sans recherche : ignoré, comme avant.
    for h in (None, tools.Hosted(modules=[]),
              tools.Hosted(modules=[search_tool(name="web_fetch")])):
        assert "tools" not in A.to_openai(request, hosted=h)
        assert "tool_choice" not in A.to_openai(request, hosted=h)
        assert not A.Context(request, h).hosted


def test_to_openai_hosted_search_variants(monkeypatch):
    hosted = hosted_tools()
    fn = {"name": "Read", "description": "", "input_schema": {"type": "object"}}

    def req(*server, **extra):
        return {"model": "m", "messages": [], "tools": [fn, *server], **extra}

    def names(request):
        return [t["function"]["name"]
                for t in A.to_openai(request, hosted=hosted).get("tools", [])]

    # Toute version datée de l'outil ; à sa place dans la liste ; une fois.
    r = {"model": "m", "messages": [], "tools": [
        {"type": "web_search_20260209", "name": "web_search"}, fn,
        {"type": "web_search_20250305", "name": "web_search", "max_uses": 2}]}
    assert names(r) == ["web_search", "Read"]
    assert A.Context(r, hosted).limit == 8      # le premier déclaré fait foi
    # Les autres outils serveur restent ignorés — web_fetch compris.
    r = req({"type": "web_fetch_20250910", "name": "web_fetch"},
            {"type": "code_execution_20250825", "name": "code_execution"},
            {"type": "web_search", "name": "web_search"})
    assert names(r) == ["Read"] and not A.Context(r, hosted).hosted
    # max_uses : respecté, borné par tools.MAX_CALLS.
    monkeypatch.setattr(tools, "MAX_CALLS", 5)
    for asked, limit in ((3, 3), (50, 5), (None, 5), ("3", 5)):
        r = req({"type": "web_search_20250305", "name": "web_search",
                 "max_uses": asked})
        assert A.Context(r, hosted).limit == limit
    # Listes de domaines du client : passées à l'exécution.
    r = req({"type": "web_search_20250305", "name": "web_search",
             "allowed_domains": ["github.com"], "blocked_domains": []})
    assert A.Context(r, hosted).options == {
        "web_search": {"allowed_domains": ["github.com"]}}
    # Une fonction du client nommée web_search garde son nom : l'outil
    # hébergé n'est pas présenté, ses appels reviennent au client.
    r = {"model": "m", "messages": [], "tools": [
        {"name": "web_search", "description": "la mienne", "input_schema": {}},
        {"type": "web_search_20250305", "name": "web_search"}]}
    out = A.to_openai(r, hosted=hosted)
    assert [t["function"]["description"] for t in out["tools"]] == ["la mienne"]
    assert not A.Context(r, hosted).hosted
    # tool_choice forcé sur la recherche : traduit comme pour une fonction.
    r = req({"type": "web_search_20250305", "name": "web_search"},
            tool_choice={"type": "tool", "name": "web_search"})
    assert A.to_openai(r, hosted=hosted)["tool_choice"] == {
        "type": "function", "function": {"name": "web_search"}}


def test_stream_hosted_search_blocks():
    hosted = hosted_tools()
    t = translator(hosted=hosted)
    ev = turn(t, *SEARCH_TURN)
    # Le texte, puis le bloc server_tool_use entier — et RIEN d'autre : ni
    # message_delta ni message_stop, la recherche reste à faire.
    assert [e for e, _ in ev] == [
        "message_start",
        "content_block_start", "content_block_delta", "content_block_stop",
        "content_block_start", "content_block_delta", "content_block_stop"]
    use = ev[4][1]["content_block"]
    assert ev[4][1]["index"] == 1 and use == {
        "type": "server_tool_use", "id": use["id"], "name": "web_search",
        "input": {}}
    assert use["id"].startswith("srvtoolu_")
    # L'input en UN delta de JSON valide, pas les fragments du modèle.
    assert ev[5][1] == {"type": "content_block_delta", "index": 1, "delta": {
        "type": "input_json_delta", "partial_json": QUERY}}
    assert ev[6][1] == {"type": "content_block_stop", "index": 1}
    assert t.pending == [{"id": use["id"], "name": "web_search",
                          "arguments": QUERY, "announced": True}]
    assert t.client_calls == 0 and t.finish() == b""

    done = events(t.resolve(t.pending[0], FOUND))
    assert [e for e, _ in done] == ["content_block_start", "content_block_stop"]
    assert done[0][1] == {"type": "content_block_start", "index": 2,
                          "content_block": {
                              "type": "web_search_tool_result",
                              "tool_use_id": use["id"], "content": BLOCKS}}
    assert done[1][1] == {"type": "content_block_stop", "index": 2}
    assert not t.pending and t.results == {use["id"]: FOUND}

    t.next_turn()
    end = turn(t, *ANSWER_TURN)
    assert [e for e, _ in end] == [
        "content_block_start", "content_block_delta", "content_block_stop",
        "message_delta", "message_stop"]
    assert end[0][1]["index"] == 3
    # Usage CUMULÉ sur les deux tours, à la forme Anthropic (cache à part),
    # et le compte des recherches.
    assert end[3][1] == {"type": "message_delta", "delta": {
        "stop_reason": "end_turn", "stop_sequence": None}, "usage": {
        "input_tokens": 110, "output_tokens": 15,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 140,
        "server_tool_use": {"web_search_requests": 1}}}
    assert t.tokens(0) == (250, 15, True) and t.cached() == 140
    assert t.content == [
        {"type": "text", "text": "Je cherche."},
        {"type": "server_tool_use", "id": use["id"], "name": "web_search",
         "input": {"query": "llama.cpp latest release"}},
        {"type": "web_search_tool_result", "tool_use_id": use["id"],
         "content": BLOCKS},
        {"type": "text", "text": "b6789."}]
    assert t.summary() == ("stop=end_turn | in=250 out=15 | tools: "
                           "web_search(" + QUERY + ")")
    assert t.finalize() == b"" and t.fail("trop tard") == b""
    # Rien n'a été rangé en mémoire : le bloc porte son résultat.
    assert len(hosted.memory) == 0


def test_stream_two_searches_come_out_as_pairs():
    """Deux recherches lancées d'un coup : le second server_tool_use ne
    sort qu'après le résultat du premier — chaque résultat suit son
    appel, comme chez Anthropic. Puis une troisième au tour suivant."""
    t = translator()
    ev = turn(t,
              tool_call(0, "call_a", "web_search", "{\"query\":\"un\"}"),
              tool_call(1, "call_b", "web_search", "{\"query\":\"deux\","),
              chunk({"tool_calls": [{"index": 1, "function": {
                  "arguments": "\"limit\":3}"}}]}),
              chunk(finish="tool_calls"), usage(10, 2))
    assert [c["arguments"] for c in t.pending] == [
        "{\"query\":\"un\"}", "{\"query\":\"deux\",\"limit\":3}"]
    assert [c["announced"] for c in t.pending] == [True, False]
    first, second = t.pending
    ev += events(t.resolve(first, FOUND))
    ev += events(t.resolve(second, "No results for «deux»."))
    t.next_turn()
    ev += turn(t, chunk({"reasoning_content": "Encore."}),
               tool_call(0, "call_c", "web_search", "{\"query\":\"trois\"}"),
               chunk(finish="tool_calls"), usage(20, 3))
    ev += events(t.resolve(t.pending[0], FOUND))
    t.next_turn()
    ev += turn(t, *ANSWER_TURN)
    starts = [(d["index"], d["content_block"]["type"]) for e, d in ev
              if e == "content_block_start"]
    assert starts == [
        (0, "server_tool_use"), (1, "web_search_tool_result"),
        (2, "server_tool_use"), (3, "web_search_tool_result"),
        (4, "thinking"),
        (5, "server_tool_use"), (6, "web_search_tool_result"), (7, "text")]
    stops = [d["index"] for e, d in ev if e == "content_block_stop"]
    assert stops == list(range(8))
    kinds = [e for e, _ in ev]
    assert kinds.count("message_start") == 1 and kinds[-2:] == [
        "message_delta", "message_stop"]
    assert kinds.count("message_delta") == 1
    # Chaque résultat renvoie à l'appel qui le précède ; aucun résultat =
    # liste vide, pas une erreur.
    c = t.content
    assert [c[i + 1]["tool_use_id"] for i in (0, 2, 5)] == [
        c[i]["id"] for i in (0, 2, 5)]
    assert c[2]["input"] == {"query": "deux", "limit": 3}
    assert c[3]["content"] == [] and c[6]["content"] == BLOCKS
    assert ev[-2][1]["usage"]["server_tool_use"] == {"web_search_requests": 3}
    assert ev[-2][1]["usage"]["input_tokens"] == 80
    assert t.turns == 3 and t.summary().count("web_search(") == 3


def test_stream_search_errors_become_error_blocks():
    request = claude_code_search()
    request["tools"][0]["max_uses"] = 3
    t = translator(request)
    turn(t, tool_call(0, "a", "web_search", "{\"query\":\"un\"}"),
         tool_call(1, "b", "web_search", "{\"q\":\"sans query\"}"),
         tool_call(2, "c", "web_search", "{pas du json"),
         tool_call(3, "d", "web_search", "{\"query\":\"de trop\"}"),
         chunk(finish="tool_calls"))
    blocks = []
    for result in ("Error: search engine unreachable (ConnectError).",
                   "Error: `query` is required.",
                   "Error: the tool arguments are not a JSON object.",
                   "Error: the limit of 3 web tool calls for one answer is "
                   "reached. Answer now with what you already have."):
        ev = events(t.resolve(t.pending[0], result))
        blocks.append(ev[0][1]["content_block"])
        assert blocks[-1]["type"] == "web_search_tool_result"
    assert [b["content"] for b in blocks] == [
        {"type": "web_search_tool_result_error", "error_code": code}
        for code in ("unavailable", "invalid_tool_input", "invalid_tool_input",
                     "max_uses_exceeded")]
    # Arguments illisibles : un input vide, jamais du JSON cassé au client.
    assert [b["input"] for b in t.content if b["type"] == "server_tool_use"] == [
        {"query": "un"}, {"q": "sans query"}, {}, {"query": "de trop"}]
    # Le modèle, lui, lira le texte exact de l'erreur au tour suivant.
    assert list(t.results.values())[0].startswith("Error: search engine")
    # Aucune recherche aboutie : pas de `server_tool_use` dans l'usage, et
    # un dernier tour clos sur tool_calls sans outil client = end_turn.
    end = events(t.finalize())
    assert end[0][1]["delta"]["stop_reason"] == "end_turn"
    assert "server_tool_use" not in end[0][1]["usage"]


def test_stream_hosted_and_client_calls_in_one_turn():
    request = claude_code_search()
    request["tools"].append({"name": "Read", "input_schema": {}})
    t = translator(request)
    ev = turn(t, tool_call(0, "call_x", "web_search", "{\"query\":\"un\"}"),
              tool_call(1, "toolu_1", "Read", "{\"p\":\"a\"}"),
              chunk(finish="tool_calls"), usage(10, 2))
    # L'outil du client est rendu au fil de l'eau ; la recherche attend la
    # fin du tour, puis sort avec son résultat.
    assert t.client_calls == 1 and len(t.pending) == 1
    ev += events(t.resolve(t.pending[0], FOUND))
    ev += events(t.finalize())
    assert [d["content_block"]["type"] for e, d in ev
            if e == "content_block_start"] == [
        "tool_use", "server_tool_use", "web_search_tool_result"]
    assert ev[-2][1]["delta"]["stop_reason"] == "tool_use"
    assert t.content[0] == {"type": "tool_use", "id": "toolu_1", "name": "Read",
                            "input": {"p": "a"}}


def test_stream_failure_and_mid_flow_error_with_hosted_search():
    t = translator()
    ev = turn(t, *SEARCH_TURN)
    ev += events(t.resolve(t.pending[0], FOUND))
    ev += events(t.fail("backend «essai» hors ligne", 503))
    assert ev[-1] == ("error", {"type": "error", "error": {
        "type": "api_error", "message": "backend «essai» hors ligne"}})
    assert t.finalize() == b"" and t.tokens(0) == (100, 10, True)
    assert events(translator().fail("quota", 429))[-1][1]["error"]["type"] \
        == "rate_limit_error"
    # Erreur DANS le flux d'un tour : la recherche demandée n'est pas
    # exécutée, le message est clos comme avant.
    t = translator()
    ev = turn(t, tool_call(0, "call_x", "web_search", "{}"),
              {"error": {"message": "GPU perdu"}})
    assert not t.pending
    assert [e for e, _ in ev] == ["message_start", "error", "message_delta",
                                  "message_stop"]


def chat_doc(message, finish, prompt, completion, cached=0):
    return json.dumps({
        "id": "chatcmpl-1",
        "choices": [{"finish_reason": finish, "message": message}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                  "prompt_tokens_details": {"cached_tokens": cached}},
    }).encode()


SEARCH_DOC = chat_doc({"content": "Je cherche.", "tool_calls": [
    {"id": "call_x", "function": {"name": "web_search", "arguments": QUERY}}]},
    "tool_calls", 100, 10, cached=40)
ANSWER_DOC = chat_doc({"content": "b6789."}, "stop", 150, 5, cached=100)


def test_json_hosted_search_then_answer():
    t = translator(claude_code_search(stream=False),
                   content_type="application/json")
    t.feed(SEARCH_DOC)
    # Rien ne part : le message n'est pas fini.
    assert t.finish() == b"" and t.client_calls == 0
    call = t.pending[0]
    assert t.resolve(call, FOUND) == b"" and not t.pending
    t.next_turn()
    t.feed(ANSWER_DOC)
    msg = json.loads(t.finish())
    assert msg["type"] == "message" and msg["id"] == "chatcmpl-1"
    assert msg["stop_reason"] == "end_turn"
    assert msg["content"] == [
        {"type": "text", "text": "Je cherche."},
        {"type": "server_tool_use", "id": call["id"], "name": "web_search",
         "input": {"query": "llama.cpp latest release"}},
        {"type": "web_search_tool_result", "tool_use_id": call["id"],
         "content": BLOCKS},
        {"type": "text", "text": "b6789."}]
    assert msg["usage"] == {
        "input_tokens": 110, "output_tokens": 15,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 140,
        "server_tool_use": {"web_search_requests": 1}}
    assert t.tokens(0) == (250, 15, True) and t.finalize() == b""

    # Recherche et outil du client dans le même tour, puis échec.
    request = claude_code_search(stream=False)
    request["tools"].append({"name": "Read", "input_schema": {}})
    doc = chat_doc({"content": None, "tool_calls": [
        {"id": "call_x", "function": {"name": "web_search", "arguments": QUERY}},
        {"id": "toolu_1", "function": {"name": "Read", "arguments": "{}"}}]},
        "tool_calls", 10, 2)
    t = translator(request, content_type="application/json")
    t.feed(doc)
    assert t.finish() == b"" and t.client_calls == 1
    t.resolve(t.pending[0], FOUND)
    msg = json.loads(t.finalize())
    assert [b["type"] for b in msg["content"]] == [
        "tool_use", "server_tool_use", "web_search_tool_result"]
    assert msg["stop_reason"] == "tool_use"
    t = translator(request, content_type="application/json")
    t.feed(doc)
    t.finish()
    assert json.loads(t.fail("quota épuisé", 429)) == {
        "type": "error", "error": {"type": "rate_limit_error",
                                   "message": "quota épuisé"}}


def test_json_trace_keeps_arguments_after_text():
    """La trace rangeait les arguments sous l'index du BLOC et les
    relisait sous celui de l'OUTIL : après un texte, ils étaient perdus."""
    t = A.Translator(200, "application/json", "m")
    t.feed(chat_doc({"content": "Je lis.", "tool_calls": [{
        "id": "c", "function": {"name": "Read", "arguments": "{\"p\": \"a\"}"}}]},
        "tool_calls", 5, 1))
    t.finish()
    assert t.summary() == "stop=tool_use | in=5 out=1 | tools: Read({\"p\": \"a\"})"


def test_to_openai_replays_search_blocks_without_memory():
    hosted = hosted_tools()
    request = claude_code_search()
    request["messages"] += [
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "…", "signature": ""},
            {"type": "text", "text": "Je cherche."},
            {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search",
             "input": {"query": " llama.cpp latest release ", "limit": 2}},
            {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1",
             "content": BLOCKS},
            {"type": "server_tool_use", "id": "srvtoolu_2", "name": "web_search",
             "input": {"query": "rien"}},
            {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_2",
             "content": []},
            {"type": "server_tool_use", "id": "srvtoolu_3", "name": "web_search",
             "input": {"query": "panne"}},
            {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_3",
             "content": {"type": "web_search_tool_result_error",
                         "error_code": "unavailable"}},
            {"type": "text", "text": "b6789."},
            # Appel sans résultat, résultat sans appel, autre outil serveur :
            # écartés — un appel sans message `tool` casserait le backend.
            {"type": "server_tool_use", "id": "srvtoolu_4", "name": "web_search",
             "input": {"query": "orphelin"}},
            {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_9",
             "content": []},
            {"type": "server_tool_use", "id": "srvtoolu_5", "name": "code_execution",
             "input": {}},
        ]},
        {"role": "user", "content": "Merci. Et la précédente ?"},
    ]
    call = lambda cid, args: {"id": cid, "type": "function", "function": {
        "name": "web_search", "arguments": args}}
    out = A.to_openai(request, hosted=hosted)
    assert out["messages"][2:] == [
        {"role": "assistant", "content": "Je cherche.", "tool_calls": [call(
            "srvtoolu_1", "{\"query\": \" llama.cpp latest release \", \"limit\": 2}")]},
        # Le texte que le modèle avait lu, reconstruit du bloc seul.
        {"role": "tool", "tool_call_id": "srvtoolu_1", "content": FOUND},
        {"role": "assistant", "content": None, "tool_calls": [
            call("srvtoolu_2", "{\"query\": \"rien\"}")]},
        {"role": "tool", "tool_call_id": "srvtoolu_2",
         "content": "No results for «rien»."},
        {"role": "assistant", "content": None, "tool_calls": [
            call("srvtoolu_3", "{\"query\": \"panne\"}")]},
        {"role": "tool", "tool_call_id": "srvtoolu_3",
         "content": "Error: the web search failed (unavailable)."},
        {"role": "assistant", "content": "b6789."},
        {"role": "user", "content": "Merci. Et la précédente ?"},
    ]
    assert len(hosted.memory) == 0
    # Sans recherche hébergée — ou si la requête ne déclare plus l'outil —,
    # les blocs sont ignorés comme avant : un seul message assistant.
    plain = [{"role": "assistant", "content": "Je cherche.b6789."},
             {"role": "user", "content": "Merci. Et la précédente ?"}]
    assert A.to_openai(request)["messages"][2:] == plain
    assert A.to_openai({**request, "tools": []}, hosted=hosted)["messages"][2:] \
        == plain


def test_loop_turn_and_client_replay_send_the_same_bytes():
    """Ce que le backend reçoit au tour 2 de la boucle est, octet pour
    octet, le début de ce qu'il recevra si le client rejoue la réponse :
    le préfixe en cache reste valide, sans mémoire côté proxy."""
    hosted = hosted_tools()
    request = claude_code_search()
    first = A.to_openai(request, hosted=hosted)
    t = translator(request, hosted)
    turn(t, chunk({"reasoning_content": "Hum."}), *SEARCH_TURN)
    t.resolve(t.pending[0], FOUND)
    # Tour 2, comme app.messages le reconstruit.
    rebuilt = lambda: A.to_openai(
        {**request, "messages": request["messages"] + [
            {"role": "assistant", "content": t.content}]},
        hosted=hosted, results=t.results)
    looped = rebuilt()
    assert looped["messages"][:2] == first["messages"]
    assert [m["role"] for m in looped["messages"][2:]] == ["assistant", "tool"]
    assert looped["messages"][3]["content"] == FOUND
    t.next_turn()
    turn(t, *ANSWER_TURN)
    # Requête suivante du client : sa copie des blocs, passée par JSON,
    # et aucune trace côté proxy des textes de la réponse précédente.
    again = A.to_openai({**request, "messages": request["messages"] + [
        {"role": "assistant", "content": json.loads(json.dumps(t.content))},
        {"role": "user", "content": "Merci"}]}, hosted=hosted)
    n = len(looped["messages"])
    encode = lambda messages: json.dumps(messages, ensure_ascii=False)
    assert encode(again["messages"][:n]) == encode(looped["messages"])
    assert again["messages"][n:] == [
        {"role": "assistant", "content": "b6789."},
        {"role": "user", "content": "Merci"}]
    assert encode(again["tools"]) == encode(looped["tools"]) == encode(first["tools"])
    # Dans la boucle, le modèle lit le texte EXACT d'une erreur ; au rejeu
    # il n'en reste que le code — seul cas où les deux diffèrent.
    t = translator(request, hosted)
    turn(t, *SEARCH_TURN)
    t.resolve(t.pending[0], "Error: search engine returned HTTP 502.")
    assert rebuilt()["messages"][3]["content"] == \
        "Error: search engine returned HTTP 502."
    t.results.clear()
    assert rebuilt()["messages"][3]["content"] == \
        "Error: the web search failed (unavailable)."


# ── la boucle d'app.py ──────────────────────────────────────────────────
# La route entière, par le client de test de Starlette ; seul l'envoi au
# backend (app.send_upstream) est remplacé : aucun réseau.

class FakeUpstream:
    def __init__(self, body: bytes, status=200, content_type="text/event-stream"):
        self.body, self.status_code = body, status
        self.headers = {"content-type": content_type}
        self.closed = False

    async def aiter_raw(self):
        for i in range(0, len(self.body), 64):
            yield self.body[i:i + 64]

    async def aread(self):
        return self.body

    async def aclose(self):
        self.closed = True


def stream_of(*docs):
    return FakeUpstream(sse(*docs) + b"data: [DONE]\n\n")


@pytest.fixture
def proxy(monkeypatch):
    """Un backend de test sans quota, la recherche factice, et de quoi
    lire ce qui part au backend (`sent`) et aux stats (`lines`)."""
    env = types.SimpleNamespace(replies=[], sent=[], lines=[], runs=[],
                                result=FOUND)

    async def run(args, **options):
        env.runs.append((args, options))
        return env.result

    env.hosted = hosted_tools(run)

    async def send_upstream(call, request, path, body):
        env.sent.append(json.loads(body))
        reply = env.replies.pop(0)
        if isinstance(reply, tuple):       # backend injoignable
            return call.error(*reply)
        return reply

    monkeypatch.setitem(app.BACKENDS, "essai",
                        Backend("essai", {"url": "http://backend.invalid"}))
    monkeypatch.setattr(app, "PROXY_API_KEYS", [])
    monkeypatch.setattr(app.anthropic_api, "ENABLED", True)
    monkeypatch.setattr(app.tools, "Hosted", lambda: env.hosted)
    monkeypatch.setattr(app, "send_upstream", send_upstream)
    monkeypatch.setattr(app.stats, "record", lambda *a: env.lines.append(a))
    env.client = TestClient(app.app)
    env.post = lambda **extra: env.client.post(
        "/v1/messages", json=claude_code_search(model="essai/qwen", **extra))
    return env


def test_app_loop_runs_the_search_claude_code_asks_for(proxy):
    ups = [stream_of(*SEARCH_TURN), stream_of(*ANSWER_TURN)]
    proxy.replies = list(ups)
    r = proxy.post()
    assert r.status_code == 200
    ev = events(r.content)
    kinds = [e for e, _ in ev]
    assert kinds.count("message_start") == 1
    assert kinds[-2:] == ["message_delta", "message_stop"]
    blocks = [d["content_block"] for e, d in ev if e == "content_block_start"]
    assert [b["type"] for b in blocks] == [
        "text", "server_tool_use", "web_search_tool_result", "text"]
    assert blocks[2] == {"type": "web_search_tool_result",
                         "tool_use_id": blocks[1]["id"], "content": BLOCKS}
    assert ev[-2][1]["usage"]["server_tool_use"] == {"web_search_requests": 1}
    assert ev[-2][1]["delta"]["stop_reason"] == "end_turn"
    assert proxy.runs == [({"query": "llama.cpp latest release"}, {})]
    # Deux envois au backend, préfixe retiré ; le second porte l'appel et
    # son résultat — le texte du modèle, le même que sur la surface
    # Responses —, et rien d'autre ne change avant eux.
    one, two = proxy.sent
    assert one["model"] == two["model"] == "qwen" and one["tools"] == two["tools"]
    assert [t["function"]["name"] for t in one["tools"]] == ["web_search"]
    assert two["messages"][:len(one["messages"])] == one["messages"]
    assert two["messages"][len(one["messages"]):] == [
        {"role": "assistant", "content": "Je cherche.", "tool_calls": [
            {"id": blocks[1]["id"], "type": "function", "function": {
                "name": "web_search", "arguments": QUERY}}]},
        {"role": "tool", "tool_call_id": blocks[1]["id"], "content": FOUND}]
    # UNE ligne de stats, usage cumulé : (clé, backend, modèle, endpoint,
    # statut, durée, prompt, completion, exact, flux, cache).
    assert len(proxy.lines) == 1
    line = proxy.lines[0]
    assert line[:5] == ("essai/qwen", "essai", "qwen", "/v1/messages", 200)
    assert line[6:] == (250, 15, True, True, 140)
    assert all(u.closed for u in ups) and not proxy.replies
    assert len(proxy.hosted.memory) == 0


def test_app_loop_json_mode_and_client_domains(proxy):
    proxy.replies = [FakeUpstream(SEARCH_DOC, content_type="application/json"),
                     FakeUpstream(ANSWER_DOC, content_type="application/json")]
    r = proxy.post(stream=False, tools=[{
        "type": "web_search_20250305", "name": "web_search",
        "allowed_domains": ["github.com"], "blocked_domains": ["x.test"]}])
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/json"
    msg = r.json()
    assert [b["type"] for b in msg["content"]] == [
        "text", "server_tool_use", "web_search_tool_result", "text"]
    assert msg["content"][2]["content"] == BLOCKS
    assert msg["stop_reason"] == "end_turn"
    assert msg["usage"]["server_tool_use"] == {"web_search_requests": 1}
    assert "stream" not in proxy.sent[0]
    # Les listes de domaines du client arrivent à l'exécution.
    assert proxy.runs == [({"query": "llama.cpp latest release"}, {
        "allowed_domains": ["github.com"], "blocked_domains": ["x.test"]})]
    assert proxy.lines[0][4] == 200
    assert proxy.lines[0][6:10] == (250, 15, True, False)


def test_app_loop_honors_max_uses_then_stops(proxy):
    """max_uses = 2 : deux recherches exécutées, les suivantes rendues en
    `max_uses_exceeded` sans être lancées ; le modèle qui insiste encore
    est arrêté 4 appels plus loin."""
    again = lambda i: stream_of(
        tool_call(0, f"call_{i}", "web_search", "{\"query\":\"encore\"}"),
        chunk(finish="tool_calls"), usage(10, 1))
    proxy.replies = [again(i) for i in range(9)]
    r = proxy.post(tools=[{"type": "web_search_20250305", "name": "web_search",
                           "max_uses": 2}], tool_choice={"type": "any"})
    ev = events(r.content)
    results = [d["content_block"]["content"] for e, d in ev
               if e == "content_block_start"
               and d["content_block"]["type"] == "web_search_tool_result"]
    assert results == [BLOCKS, BLOCKS] + [
        {"type": "web_search_tool_result_error",
         "error_code": "max_uses_exceeded"}] * 4
    assert len(proxy.runs) == 2 and len(proxy.sent) == 6
    assert "the limit of 2 web tool calls" in proxy.sent[3]["messages"][-1]["content"]
    assert ev[-2][1]["delta"]["stop_reason"] == "end_turn"
    assert ev[-2][1]["usage"]["server_tool_use"] == {"web_search_requests": 2}
    assert proxy.lines[0][6:8] == (60, 6)
    # `tool_choice` forcé : appliqué au premier tour seulement, sinon le
    # modèle ne pourrait jamais conclure.
    assert [s["tool_choice"] for s in proxy.sent] == ["required"] + ["auto"] * 5


def test_app_loop_search_error_is_a_block_not_an_http_error(proxy):
    proxy.result = "Error: search engine unreachable (ConnectError)."
    proxy.replies = [stream_of(*SEARCH_TURN), stream_of(*ANSWER_TURN)]
    r = proxy.post()
    ev = events(r.content)
    assert r.status_code == 200 and ev[-1][0] == "message_stop"
    block = next(d["content_block"] for e, d in ev if e == "content_block_start"
                 and d["content_block"]["type"] == "web_search_tool_result")
    assert block["content"] == {"type": "web_search_tool_result_error",
                                "error_code": "unavailable"}
    # Le modèle, lui, lit pourquoi.
    assert proxy.sent[1]["messages"][-1]["content"] == proxy.result
    assert "server_tool_use" not in ev[-2][1]["usage"]


def test_app_loop_hands_back_when_the_client_is_called_too(proxy):
    up = stream_of(
        tool_call(0, "call_x", "web_search", "{\"query\":\"un\"}"),
        tool_call(1, "toolu_1", "Read", "{}"),
        chunk(finish="tool_calls"), usage(10, 2))
    proxy.replies = [up]
    r = proxy.post(tools=[
        {"type": "web_search_20250305", "name": "web_search"},
        {"name": "Read", "input_schema": {"type": "object"}}])
    ev = events(r.content)
    assert [d["content_block"]["type"] for e, d in ev
            if e == "content_block_start"] == [
        "tool_use", "server_tool_use", "web_search_tool_result"]
    assert ev[-2][1]["delta"]["stop_reason"] == "tool_use"
    # La recherche est exécutée, mais pas de second tour : au client.
    assert len(proxy.runs) == 1 and len(proxy.sent) == 1 and up.closed
    assert proxy.lines[0][6:8] == (10, 2)


def test_app_loop_failure_on_a_later_turn(proxy):
    # Backend éteint au tour 2, en flux : `event: error` (le 200 est
    # parti), et la ligne de stats garde ce que le tour 1 a consommé.
    proxy.replies = [stream_of(*SEARCH_TURN),
                     (503, "backend_offline", "backend «essai» hors ligne")]
    r = proxy.post()
    ev = events(r.content)
    assert r.status_code == 200
    assert ev[-1] == ("error", {"type": "error", "error": {
        "type": "api_error", "message": "backend «essai» hors ligne"}})
    assert len(proxy.lines) == 1
    assert proxy.lines[0][4] == 503 and proxy.lines[0][6:9] == (100, 10, True)

    # Statut d'erreur upstream au tour 2.
    proxy.lines.clear()
    bad = FakeUpstream(json.dumps({"error": {"message": "contexte dépassé"}})
                       .encode(), status=400, content_type="application/json")
    proxy.replies = [stream_of(*SEARCH_TURN), bad]
    ev = events(proxy.post().content)
    assert ev[-1] == ("error", {"type": "error", "error": {
        "type": "invalid_request_error", "message": "contexte dépassé"}})
    assert bad.closed and proxy.lines[0][4] == 400

    # En JSON rien n'est parti : la réponse prend le vrai statut.
    proxy.lines.clear()
    proxy.replies = [FakeUpstream(SEARCH_DOC, content_type="application/json"),
                     (503, "backend_offline", "backend «essai» hors ligne")]
    r = proxy.post(stream=False)
    assert r.status_code == 503 and r.json() == {"type": "error", "error": {
        "type": "api_error", "message": "backend «essai» hors ligne"}}
    assert len(proxy.lines) == 1 and proxy.lines[0][4] == 503

    # Erreur dès le PREMIER tour : le statut et le corps d'erreur habituels.
    proxy.lines.clear()
    proxy.replies = [FakeUpstream(b'{"error": {"message": "non"}}', status=500,
                                  content_type="application/json")]
    r = proxy.post()
    assert r.status_code == 500 and r.json()["error"] == {
        "type": "api_error", "message": "non"}
    assert len(proxy.lines) == 1 and proxy.lines[0][4] == 500
    proxy.lines.clear()
    proxy.replies = [(503, "backend_offline", "éteint")]
    r = proxy.post()
    assert r.status_code == 503 and r.json()["type"] == "error"
    assert len(proxy.lines) == 1 and not proxy.runs[4:]


def test_app_without_hosted_search_takes_the_plain_path(proxy, monkeypatch):
    answer = lambda: stream_of(chunk({"content": "Bonjour"}),
                               chunk(finish="stop"), usage(5, 1))
    expected = [
        "message_start", "content_block_start", "content_block_delta",
        "content_block_stop", "message_delta", "message_stop"]
    # Le client ne déclare pas la recherche : relais ordinaire, un tour.
    proxy.replies = [answer()]
    r = proxy.post(tools=[{"name": "Read", "input_schema": {"type": "object"}}])
    ev = events(r.content)
    assert [e for e, _ in ev] == expected
    assert "server_tool_use" not in ev[-2][1]["usage"]
    assert [t["function"]["name"] for t in proxy.sent[0]["tools"]] == ["Read"]
    assert len(proxy.lines) == 1
    # Il la déclare, mais le proxy n'héberge rien : ignorée comme avant —
    # ni `tools` ni `tool_choice` ne partent au backend.
    empty = type(proxy.hosted)(modules=[])
    monkeypatch.setattr(app.tools, "Hosted", lambda: empty)
    proxy.replies = [answer()]
    r = proxy.post()
    assert [e for e, _ in events(r.content)] == expected
    assert "tools" not in proxy.sent[1] and "tool_choice" not in proxy.sent[1]
    assert len(proxy.lines) == 2
    # Il la déclare, le proxy l'héberge, le modèle ne cherche pas : un
    # tour, le même flux, aucun compte de recherche.
    monkeypatch.setattr(app.tools, "Hosted", lambda: proxy.hosted)
    proxy.replies = [answer()]
    ev = events(proxy.post().content)
    assert [e for e, _ in ev] == expected
    assert ev[-2][1]["usage"] == {
        "input_tokens": 5, "output_tokens": 1,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    assert not proxy.runs and len(proxy.sent) == 3 and len(proxy.lines) == 3


def test_app_loop_pings_while_the_search_runs(proxy, monkeypatch):
    """En flux, des `ping` tiennent la connexion pendant l'exécution
    (Claude Code coupe un flux muet) ; aucun en JSON."""
    async def slow(args, **options):
        await asyncio.sleep(0.08)
        return FOUND

    proxy.hosted.by_name["web_search"].run = slow
    monkeypatch.setattr(app.anthropic_api, "PING_INTERVAL", 0.02)
    proxy.replies = [stream_of(*SEARCH_TURN), stream_of(*ANSWER_TURN)]
    kinds = [e for e, _ in events(proxy.post().content)]
    assert "ping" in kinds and kinds[-1] == "message_stop"
    # Entre l'annonce de la recherche et son résultat, nulle part ailleurs.
    first, last = kinds.index("ping"), len(kinds) - kinds[::-1].index("ping")
    assert kinds[first - 1] == "content_block_stop"
    assert set(kinds[first:last]) == {"ping"}
    assert kinds[last] == "content_block_start"
    proxy.replies = [FakeUpstream(SEARCH_DOC, content_type="application/json"),
                     FakeUpstream(ANSWER_DOC, content_type="application/json")]
    assert proxy.post(stream=False).json()["stop_reason"] == "end_turn"


def test_app_loop_behind_quotas_goes_through_the_pinged_stream(proxy, monkeypatch):
    """Backend à quotas, en flux : le premier tour passe par pinged_stream
    (200 immédiat, pings pendant l'attente du quota), puis la même boucle."""
    class Limiter:
        name = "essai"

        async def acquire(self, cost, gone=None):
            return 0.0

    backend = app.BACKENDS["essai"]
    monkeypatch.setattr(backend, "quotas", True)
    monkeypatch.setattr(backend, "quota_state", types.SimpleNamespace(
        get_limiter=lambda payload: Limiter()), raising=False)
    ups = [stream_of(*SEARCH_TURN), stream_of(*ANSWER_TURN)]
    proxy.replies = list(ups)
    r = proxy.post()
    assert r.headers["content-type"].startswith("text/event-stream")
    ev = events(r.content)
    assert [d["content_block"]["type"] for e, d in ev
            if e == "content_block_start"] == [
        "text", "server_tool_use", "web_search_tool_result", "text"]
    assert ev[-1][0] == "message_stop" and len(proxy.sent) == 2
    assert len(proxy.lines) == 1 and proxy.lines[0][6:8] == (250, 15)
    assert all(u.closed for u in ups)


def test_app_loop_client_gone_cancels_the_search(proxy):
    """Le client raccroche pendant la recherche : elle est annulée,
    l'upstream fermé, la ligne de stats écrite une fois."""
    started, cancelled = [], []

    async def endless(args, **options):
        started.append(args)
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append(args)
            raise

    proxy.hosted.by_name["web_search"].run = endless
    up = stream_of(*SEARCH_TURN)
    request = claude_code_search(model="essai/qwen")
    ctx = A.Context(request, proxy.hosted)
    robinet = A.Translator(200, "text/event-stream", "essai/qwen", ctx)
    call = app.Call(app.BACKENDS["essai"], "essai/qwen", "/v1/messages",
                    "anthropic")

    async def scenario():
        stream = app.hosted_loop(call, None, proxy.hosted, robinet, up, 0,
                                 lambda r: {}, limit=ctx.limit,
                                 ping=A.ping_event())
        old, A.PING_INTERVAL = A.PING_INTERVAL, 0.01
        try:
            async for out in stream:
                if out == A.ping_event():
                    break
            await stream.aclose()
            await asyncio.sleep(0)      # laisse l'annulation arriver à la tâche
        finally:
            A.PING_INTERVAL = old

    asyncio.run(scenario())
    assert started and cancelled == started
    assert up.closed and len(proxy.lines) == 1
    assert proxy.lines[0][4] == 200 and proxy.lines[0][6:8] == (100, 10)
    assert not proxy.sent
