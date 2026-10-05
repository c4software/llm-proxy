"""Le traducteur Responses ↔ OpenAI chat, testé sur des octets : aucun
réseau, aucun serveur — responses_api ne connaît ni FastAPI ni httpx.
Les corps de requête ont la forme de ceux que Codex CLI 0.157 envoie
(capture du 05/10/2026), réduits."""

import json

import pytest

from llm_proxy import responses_api as R


def fn(name, **extra):
    return {"type": "function", "name": name, "description": f"outil {name}",
            "strict": False,
            "parameters": {"type": "object", "properties": {}}, **extra}


def user(text):
    return {"type": "message", "role": "user",
            "content": [{"type": "input_text", "text": text}]}


def codex_request(**extra):
    """Premier tour d'une session Codex : consignes en `developer`,
    contexte puis demande en `user`, outils de trois sortes."""
    return {
        "model": "bigchuck/qwen",
        "instructions": "Tu es Codex.",
        "input": [
            {"type": "message", "id": "msg_1", "role": "developer",
             "content": [{"type": "input_text", "text": "<skills>"},
                         {"type": "input_text", "text": "<permissions>"}]},
            user("<environment_context>"),
            user("Crée hello.txt"),
        ],
        "tools": [
            fn("exec_command"),
            {"type": "namespace", "name": "multi_agent_v1",
             "description": "agents", "tools": [fn("spawn_agent"),
                                                fn("wait_agent", strict=True)]},
            {"type": "web_search", "external_web_access": True},
        ],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
        "reasoning": {"summary": "auto"},
        "store": False,
        "stream": True,
        "include": ["reasoning.encrypted_content"],
        "prompt_cache_key": "01a1",
        "client_metadata": {"session_id": "01a1"},
        **extra,
    }


# ── requête ─────────────────────────────────────────────────────────────

def test_to_chat_string_input():
    out, ctx = R.to_chat({"model": "albert/m", "input": "Bonjour",
                          "max_output_tokens": 50, "temperature": 0.2})
    assert out == {"model": "albert/m", "max_tokens": 50, "temperature": 0.2,
                   "messages": [{"role": "user", "content": "Bonjour"}]}
    assert ctx.model == "albert/m" and not ctx.namespaces and not ctx.ignored


def test_to_chat_codex_first_turn():
    out, ctx = R.to_chat(codex_request())
    # Consignes d'ouverture : UN message system, instructions d'abord.
    assert out["messages"] == [
        {"role": "system", "content": "Tu es Codex.\n\n<skills><permissions>"},
        {"role": "user", "content": "<environment_context>"},
        {"role": "user", "content": "Crée hello.txt"},
    ]
    # Outil hébergé ignoré, namespace aplati, `strict` seulement s'il est vrai.
    names = [t["function"]["name"] for t in out["tools"]]
    assert names == ["exec_command", "spawn_agent", "wait_agent"]
    assert "strict" not in out["tools"][0]["function"]
    assert out["tools"][2]["function"]["strict"] is True
    assert ctx.namespaces == {"spawn_agent": "multi_agent_v1",
                              "wait_agent": "multi_agent_v1"}
    assert ctx.ignored == ["web_search"]
    assert out["tool_choice"] == "auto" and out["parallel_tool_calls"] is True
    assert out["stream"] is True
    assert out["stream_options"] == {"include_usage": True}
    # Le corps est reconstruit : rien d'inconnu ne part vers le backend.
    assert set(out) == {"model", "messages", "tools", "tool_choice",
                        "parallel_tool_calls", "stream", "stream_options"}


def test_to_chat_replayed_tool_turns():
    p = codex_request()
    p["input"] += [
        {"type": "reasoning", "id": "rs_1", "summary": [],
         "content": None, "encrypted_content": None},
        {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "Je regarde."}]},
        {"type": "function_call", "id": "fc_1", "call_id": "call_a",
         "name": "exec_command", "arguments": "{\"cmd\":\"ls\"}"},
        {"type": "function_call", "id": "fc_2", "call_id": "call_b",
         "name": "spawn_agent", "namespace": "multi_agent_v1",
         "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_a", "output": "a.txt"},
        {"type": "function_call_output", "call_id": "call_b", "output": "ok"},
        {"type": "function_call", "call_id": "call_c", "name": "exec_command",
         "arguments": "{\"cmd\":\"cat a.txt\"}"},
        {"type": "function_call_output", "call_id": "call_c", "output": "bonjour"},
    ]
    out, _ = R.to_chat(p)
    tail = out["messages"][3:]
    assert [m["role"] for m in tail] == [
        "assistant", "tool", "tool", "assistant", "tool"]
    # Texte et appels du même tour : un seul message assistant.
    assert tail[0]["content"] == "Je regarde."
    assert [c["id"] for c in tail[0]["tool_calls"]] == ["call_a", "call_b"]
    assert tail[0]["tool_calls"][0]["function"] == {
        "name": "exec_command", "arguments": "{\"cmd\":\"ls\"}"}
    assert tail[1] == {"role": "tool", "tool_call_id": "call_a",
                       "content": "a.txt"}
    assert tail[3]["content"] is None
    assert tail[4]["content"] == "bonjour"


def test_to_chat_developer_mid_conversation_keeps_its_place():
    out, _ = R.to_chat({"model": "b/m", "input": [
        user("un"),
        {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "deux"}]},
        {"type": "message", "role": "developer",
         "content": [{"type": "input_text", "text": "rappel"}]},
        user("trois"),
        {"type": "message", "role": "developer", "content": "fin"},
    ]})
    assert out["messages"] == [
        {"role": "user", "content": "un"},
        {"role": "assistant", "content": "deux"},
        {"role": "user", "content": "rappel\n\ntrois"},
        {"role": "user", "content": "fin"},
    ]


def test_to_chat_images():
    item = {"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": "Que vois-tu ?"},
        {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]}
    p = {"model": "b/m", "input": [item]}
    assert R.has_images(p) and not R.has_images({"input": [user("x")]})
    out, _ = R.to_chat(p, images=True)
    assert out["messages"][0]["content"] == [
        {"type": "text", "text": "Que vois-tu ?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]
    out, _ = R.to_chat(p, images=False)
    assert out["messages"][0]["content"] == "Que vois-tu ?[image ignorée]"


def test_to_chat_tool_output_image_follows_tool_messages():
    p = {"model": "b/m", "input": [
        user("regarde"),
        {"type": "function_call", "call_id": "c1", "name": "view_image",
         "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": [
            {"type": "input_text", "text": "vu"},
            {"type": "input_image", "image_url": "data:image/png;base64,BBBB"}]},
    ]}
    assert R.has_images(p)
    out, _ = R.to_chat(p, images=True)
    assert out["messages"][2] == {"role": "tool", "tool_call_id": "c1",
                                  "content": "vu"}
    assert out["messages"][3]["role"] == "user"
    assert out["messages"][3]["content"][1]["type"] == "image_url"


def test_to_chat_options():
    out, _ = R.to_chat({
        "model": "b/m", "input": "x", "reasoning": {"effort": "low"},
        "text": {"verbosity": "low", "format": {
            "type": "json_schema", "name": "avis", "strict": True,
            "schema": {"type": "object"}}},
        "tool_choice": {"type": "function", "name": "f"}, "tools": [fn("f")],
    })
    assert out["reasoning_effort"] == "low"
    assert out["response_format"] == {"type": "json_schema", "json_schema": {
        "name": "avis", "schema": {"type": "object"}, "strict": True}}
    assert out["tool_choice"] == {"type": "function", "function": {"name": "f"}}
    # Que des outils hébergés : ni tools ni tool_choice ne partent.
    out, ctx = R.to_chat({"model": "b/m", "input": "x", "tool_choice": "auto",
                          "tools": [{"type": "web_search"}, {"type": "mcp"}]})
    assert "tools" not in out and "tool_choice" not in out
    assert ctx.ignored == ["web_search", "mcp"]


@pytest.mark.parametrize("patch, needle", [
    ({"previous_response_id": "resp_1"}, "previous_response_id"),
    ({"conversation": "conv_1"}, "conversation"),
    ({"background": True}, "background"),
    ({"input": [{"type": "item_reference", "id": "msg_1"}]}, "item_reference"),
    ({"tools": [fn("a"), {"type": "namespace", "name": "n",
                          "tools": [fn("a")]}]}, "deux fois"),
    ({"tools": [{"type": "namespace", "name": "n1", "tools": [fn("a")]},
                {"type": "namespace", "name": "n2", "tools": [fn("a")]}]},
     "deux fois"),
    ({"tools": [{"type": "namespace", "tools": [fn("a")]}]}, "mal formé"),
    ({"tools": [{"type": "namespace", "name": "n",
                 "tools": [{"type": "web_search"}]}]}, "function"),
    ({"tools": [{"type": "function"}]}, "name"),
])
def test_to_chat_refuses_what_it_cannot_honor(patch, needle):
    with pytest.raises(R.Refused, match=needle):
        R.to_chat({"model": "b/m", "input": "x", **patch})


# ── réponse ─────────────────────────────────────────────────────────────

def ctx_of(**extra):
    return R.to_chat(codex_request(**extra))[1]


def test_from_chat_text_tools_and_usage():
    resp = R.from_chat({
        "created": 1700000000,
        "choices": [{"finish_reason": "tool_calls", "message": {
            "content": "Je lance.", "reasoning_content": "Il faut lancer.",
            "tool_calls": [
                {"id": "call_a", "function": {"name": "exec_command",
                                              "arguments": "{\"cmd\":\"ls\"}"}},
                {"id": "call_b", "function": {"name": "spawn_agent",
                                              "arguments": "{}"}}]}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                  "prompt_tokens_details": {"cached_tokens": 60}},
    }, ctx_of())
    assert resp["object"] == "response" and resp["status"] == "completed"
    assert resp["model"] == "bigchuck/qwen" and resp["created_at"] == 1700000000
    assert [o["type"] for o in resp["output"]] == [
        "reasoning", "message", "function_call", "function_call"]
    assert resp["output"][0]["summary"] == [
        {"type": "summary_text", "text": "Il faut lancer."}]
    assert resp["output"][1]["content"][0]["text"] == "Je lance."
    call = resp["output"][2]
    assert (call["call_id"], call["name"], call["arguments"]) == (
        "call_a", "exec_command", "{\"cmd\":\"ls\"}")
    assert "namespace" not in call
    # Le namespace d'origine revient sur l'appel de la fonction aplatie.
    assert resp["output"][3]["namespace"] == "multi_agent_v1"
    # input_tokens INCLUT le cache, au contraire de la surface Anthropic.
    assert resp["usage"] == {
        "input_tokens": 100, "input_tokens_details": {"cached_tokens": 60},
        "output_tokens": 20, "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 120}
    # Les outils reviennent tels que le client les a envoyés.
    assert len(resp["tools"]) == 3 and resp["tool_choice"] == "auto"


def test_from_chat_length_is_incomplete():
    resp = R.from_chat({"choices": [{"finish_reason": "length",
                                     "message": {"content": "coup"}}]}, ctx_of())
    assert resp["status"] == "incomplete"
    assert resp["incomplete_details"] == {"reason": "max_output_tokens"}


def sse(*docs, done=True):
    out = b"".join(b"data: " + json.dumps(d).encode() + b"\n\n" for d in docs)
    return out + (b"data: [DONE]\n\n" if done else b"")


def chunk(delta=None, finish=None, **extra):
    return {"id": "chatcmpl-1", "created": 1700000000,
            "choices": [{"index": 0, "delta": delta or {},
                         "finish_reason": finish}], **extra}


def events(raw: bytes) -> list[dict]:
    return [json.loads(line[5:]) for line in raw.decode().split("\n")
            if line.startswith("data:")]


def run(stream: bytes, ctx=None, cut: int = 7):
    """Le flux passé au robinet par morceaux de `cut` octets : les
    événements ne tombent jamais sur une frontière de chunk réseau."""
    t = R.Translator(200, "text/event-stream", ctx or ctx_of())
    out = b"".join(t.feed(stream[i:i + cut]) for i in range(0, len(stream), cut))
    return t, events(out + t.finish())


def test_stream_text():
    t, ev = run(sse(
        chunk({"role": "assistant", "content": ""}),
        chunk({"content": "Bon"}), chunk({"content": "jour"}),
        chunk(finish="stop"),
        {"choices": [], "usage": {"prompt_tokens": 19, "completion_tokens": 2}},
    ))
    assert [e["type"] for e in ev] == [
        "response.created", "response.in_progress",
        "response.output_item.added", "response.content_part.added",
        "response.output_text.delta", "response.output_text.delta",
        "response.output_text.done", "response.content_part.done",
        "response.output_item.done", "response.completed"]
    assert [e["sequence_number"] for e in ev] == list(range(len(ev)))
    assert ev[0]["response"]["status"] == "in_progress"
    assert ev[0]["response"]["usage"] is None
    assert ev[4]["delta"] == "Bon" and ev[4]["output_index"] == 0
    assert ev[4]["item_id"] == ev[2]["item"]["id"]
    assert ev[6]["text"] == "Bonjour"
    final = ev[-1]["response"]
    assert final["status"] == "completed" and final["id"] == ev[0]["response"]["id"]
    assert final["output"][0]["content"][0]["text"] == "Bonjour"
    assert final["usage"]["input_tokens"] == 19
    assert t.tokens(0) == (19, 2, True) and t.sse and t.ok


def test_stream_tool_calls_fragmented_and_namespaced():
    _, ev = run(sse(
        chunk({"content": "Je lance."}),
        chunk({"tool_calls": [{"index": 0, "id": "call_a", "function": {
            "name": "exec_command", "arguments": ""}}]}),
        chunk({"tool_calls": [{"index": 0, "function": {"arguments": "{\"cmd\":"}}]}),
        chunk({"tool_calls": [{"index": 0, "function": {"arguments": "\"ls\"}"}}]}),
        chunk({"tool_calls": [{"index": 1, "id": "call_b", "function": {
            "name": "spawn_agent", "arguments": "{}"}}]}),
        chunk(finish="tool_calls"),
    ))
    kinds = [e["type"] for e in ev]
    assert kinds.count("response.output_item.added") == 3
    assert kinds.count("response.function_call_arguments.delta") == 3
    added = [e for e in ev if e["type"] == "response.output_item.added"]
    assert [e["output_index"] for e in added] == [0, 1, 2]
    assert added[1]["item"] == {
        "id": added[1]["item"]["id"], "type": "function_call",
        "call_id": "call_a", "name": "exec_command", "arguments": "",
        "status": "in_progress"}
    done = [e for e in ev if e["type"] == "response.function_call_arguments.done"]
    assert done[0]["arguments"] == "{\"cmd\":\"ls\"}"
    assert done[0]["item_id"] == added[1]["item"]["id"]
    out = ev[-1]["response"]["output"]
    assert [o["type"] for o in out] == ["message", "function_call", "function_call"]
    assert out[1]["status"] == "completed" and out[1]["arguments"] == "{\"cmd\":\"ls\"}"
    assert out[2]["namespace"] == "multi_agent_v1"


def test_stream_reasoning_then_text():
    _, ev = run(sse(
        chunk({"reasoning_content": "Hum"}), chunk({"reasoning_content": "."}),
        chunk({"content": "Paris"}), chunk(finish="stop"),
    ))
    kinds = [e["type"] for e in ev]
    assert kinds[2:8] == [
        "response.output_item.added", "response.reasoning_summary_part.added",
        "response.reasoning_summary_text.delta",
        "response.reasoning_summary_text.delta",
        "response.reasoning_summary_text.done",
        "response.reasoning_summary_part.done"]
    out = ev[-1]["response"]["output"]
    assert out[0] == {"id": out[0]["id"], "type": "reasoning",
                      "summary": [{"type": "summary_text", "text": "Hum."}]}
    assert out[1]["content"][0]["text"] == "Paris"


def test_stream_reasoning_dropped_when_disabled(monkeypatch):
    monkeypatch.setattr(R, "REASONING_AS_SUMMARY", False)
    _, ev = run(sse(chunk({"reasoning_content": "Hum"}),
                    chunk({"content": "Paris"}), chunk(finish="stop")))
    assert [o["type"] for o in ev[-1]["response"]["output"]] == ["message"]


def test_stream_length_ends_incomplete():
    _, ev = run(sse(chunk({"content": "coup"}), chunk(finish="length")))
    assert ev[-1]["type"] == "response.incomplete"
    assert ev[-1]["response"]["incomplete_details"] == {
        "reason": "max_output_tokens"}


def test_stream_error_mid_flight_and_missing_done():
    _, ev = run(sse(chunk({"content": "dé"}),
                    {"error": {"message": "GPU perdu", "type": "server_error"}},
                    done=False))
    assert ev[-1]["type"] == "response.failed"
    assert ev[-1]["response"]["status"] == "failed"
    assert ev[-1]["response"]["error"]["message"] == "GPU perdu"
    # Flux coupé sans [DONE] : la réponse est close quand même.
    _, ev = run(sse(chunk({"content": "dé"}), done=False))
    assert ev[-1]["type"] == "response.completed"


def test_json_response_and_upstream_errors():
    t = R.Translator(200, "application/json", ctx_of())
    assert t.feed(json.dumps({
        "choices": [{"finish_reason": "stop", "message": {"content": "Paris"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1,
                  "prompt_tokens_details": {"cached_tokens": 3}},
    }).encode()) == b""
    resp = json.loads(t.finish())
    assert resp["output"][0]["content"][0]["text"] == "Paris"
    assert not t.sse and t.tokens(0) == (5, 1, True) and t.cached() == 3

    # Une erreur déjà à la forme OpenAI passe telle quelle.
    t = R.Translator(400, "application/json", ctx_of())
    body = {"error": {"message": "trop long", "type": "invalid_request_error"}}
    t.feed(json.dumps(body).encode())
    assert json.loads(t.finish()) == body and not t.ok
    t = R.Translator(502, "text/html", ctx_of())
    t.feed(b"<html>Bad Gateway</html>")
    assert json.loads(t.finish())["error"]["message"] == "<html>Bad Gateway</html>"
    assert t.tokens(7) == (7, 0, False)
