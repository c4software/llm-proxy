"""Le traducteur Anthropic ↔ OpenAI, testé sur des octets : aucun
réseau, aucun serveur — anthropic_api ne connaît ni FastAPI ni httpx."""

import json

import pytest

from llm_proxy import anthropic_api as A

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


def test_to_openai_assistant_only_tools_has_null_content():
    out = A.to_openai({"model": "m", "messages": [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t", "name": "f", "input": {}}]}]})
    assert out["messages"][0]["content"] is None


def test_to_openai_image_base64():
    out = A.to_openai({"model": "m", "messages": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": "AAAA"}},
        {"type": "text", "text": "Quoi ?"},
    ]}]})
    parts = out["messages"][0]["content"]
    assert parts[0] == {"type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAAA"}}
    assert parts[1] == {"type": "text", "text": "Quoi ?"}


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


def test_from_openai_reasoning_becomes_thinking():
    msg = A.from_openai({"choices": [{"message": {
        "reasoning_content": "hmm", "content": "ok"}, "finish_reason": "stop"}]}, "m")
    assert msg["content"][0] == {"type": "thinking", "thinking": "hmm", "signature": ""}
    assert msg["content"][1] == {"type": "text", "text": "ok"}


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
