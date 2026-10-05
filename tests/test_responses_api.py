"""Le traducteur Responses ↔ OpenAI chat, testé sur des octets : aucun
réseau, aucun serveur — responses_api ne connaît ni FastAPI ni httpx.
Les corps de requête ont la forme de ceux que Codex CLI 0.157 envoie
(capture du 05/10/2026), réduits."""

import asyncio
import json
import types

import pytest
from fastapi.testclient import TestClient

from llm_proxy import app as A
from llm_proxy import responses_api as R
from llm_proxy import tools
from llm_proxy.backends import Backend


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


# ── outils hébergés ─────────────────────────────────────────────────────
# Aucun réseau : de faux modules d'outil, à l'interface de ceux de
# llm_proxy/tools/, et une mémoire propre à chaque test. Rien ne dépend
# de ce que la configuration d'exemple active.

def fake_tool(name, action_type, key):
    async def run(args):
        return f"résultat de {name} pour {args.get(key)}"

    return types.SimpleNamespace(
        NAME=name, KINDS=("web_search",), ITEM_TYPE="web_search_call",
        ENABLED=True, run=run,
        DEFINITION={"type": "function", "function": {
            "name": name, "description": f"hébergé {name}",
            "parameters": {"type": "object", "properties": {}}}},
        action=lambda args: {"type": action_type,
                             key: str(args.get(key) or "")})


def hosted_tools(memory=None):
    return tools.Hosted(
        modules=[fake_tool("web_search", "search", "query"),
                 fake_tool("web_fetch", "open_page", "url")],
        memory=memory or tools.Memory(8, 60))


def tool_call(index, call_id, name, arguments):
    return chunk({"tool_calls": [{"index": index, "id": call_id, "function": {
        "name": name, "arguments": arguments}}]})


def usage(prompt, completion, cached=0, reasoning=0):
    return {"choices": [], "usage": {
        "prompt_tokens": prompt, "completion_tokens": completion,
        "prompt_tokens_details": {"cached_tokens": cached},
        "completion_tokens_details": {"reasoning_tokens": reasoning}}}


def turn(t, stream: bytes, cut: int = 7) -> list[dict]:
    """Un tour upstream entier passé au robinet `t`, finish() compris."""
    out = b"".join(t.feed(stream[i:i + cut]) for i in range(0, len(stream), cut))
    return events(out + t.finish())


SEARCH_TURN = (
    chunk({"content": "Je cherche."}),
    tool_call(0, "call_x", "web_search", ""),
    chunk({"tool_calls": [{"index": 0, "function": {"arguments": "{\"query\":"}}]}),
    chunk({"tool_calls": [{"index": 0, "function": {"arguments": " \"gufo\"}"}}]}),
    chunk(finish="tool_calls"),
    usage(100, 10, cached=40, reasoning=3),
)
ANSWER_TURN = (chunk({"content": "Voilà."}), chunk(finish="stop"),
               usage(150, 5, cached=100, reasoning=1))


def test_to_chat_declares_hosted_functions():
    hosted = hosted_tools()
    out, ctx = R.to_chat(codex_request(), hosted=hosted)
    names = [t["function"]["name"] for t in out["tools"]]
    assert names == ["exec_command", "spawn_agent", "wait_agent",
                     "web_search", "web_fetch"]
    assert out["tools"][3] == hosted.by_name["web_search"].DEFINITION
    assert list(ctx.hosted) == ["web_search", "web_fetch"]
    assert ctx.memory is hosted.memory and ctx.ignored == []
    # Le client voit revenir SES outils, pas les fonctions du proxy.
    assert ctx.echo["tools"][-1] == {"type": "web_search",
                                     "external_web_access": True}
    # Un annuaire vide ne change rien : l'outil est ignoré, comme sans lui.
    out, ctx = R.to_chat(codex_request(), hosted=tools.Hosted(modules=[]))
    assert not ctx.hosted and ctx.ignored == ["web_search"]


def test_to_chat_client_function_wins_over_hosted_namesake():
    p = codex_request()
    p["tools"].insert(0, fn("web_search"))
    out, ctx = R.to_chat(p, hosted=hosted_tools())
    names = [t["function"]["name"] for t in out["tools"]]
    # Une seule fonction `web_search`, celle du client ; web_fetch reste.
    assert names == ["web_search", "exec_command", "spawn_agent",
                     "wait_agent", "web_fetch"]
    assert out["tools"][0]["function"]["description"] == "outil web_search"
    assert list(ctx.hosted) == ["web_fetch"]
    # Son appel est donc rendu au client, en `function_call`.
    t = R.Translator(200, "text/event-stream", ctx)
    ev = turn(t, sse(tool_call(0, "call_x", "web_search", "{}"),
                     chunk(finish="tool_calls")))
    assert not t.pending and t.client_calls == 1
    assert ev[-1]["response"]["output"][0]["type"] == "function_call"


def test_stream_hosted_call_waits_for_its_result():
    hosted = hosted_tools()
    ctx = R.to_chat(codex_request(), hosted=hosted)[1]
    t = R.Translator(200, "text/event-stream", ctx)
    ev = turn(t, sse(*SEARCH_TURN))
    kinds = [e["type"] for e in ev]
    assert kinds == [
        "response.created", "response.in_progress",
        "response.output_item.added", "response.content_part.added",
        "response.output_text.delta", "response.output_text.done",
        "response.content_part.done", "response.output_item.done",
        "response.output_item.added", "response.web_search_call.in_progress",
        "response.web_search_call.searching"]
    # Ni arguments, ni `done`, ni fin de réponse : le proxy doit d'abord
    # exécuter l'appel.
    item = ev[8]["item"]
    assert item == {"id": item["id"], "type": "web_search_call",
                    "status": "in_progress"}
    assert item["id"].startswith("ws_") and ev[8]["output_index"] == 1
    assert ev[9]["item_id"] == ev[10]["item_id"] == item["id"]
    assert ev[9]["output_index"] == ev[10]["output_index"] == 1
    assert t.pending == [{"item_id": item["id"], "name": "web_search",
                          "arguments": "{\"query\": \"gufo\"}", "index": 1}]
    assert t.client_calls == 0 and t.finish() == b""
    assert t.output[1] == item and hosted.memory.recall(item["id"]) is None

    done = events(t.resolve(t.pending[0], "1. gufo.org"))
    assert [e["type"] for e in done] == [
        "response.web_search_call.completed", "response.output_item.done"]
    assert done[0]["output_index"] == 1 and done[0]["item_id"] == item["id"]
    assert done[1]["item"] == {
        "id": item["id"], "type": "web_search_call", "status": "completed",
        "action": {"type": "search", "query": "gufo"}}
    assert not t.pending and t.output[1] == done[1]["item"]
    # Le résultat ne part pas au client : il attend son rejeu en mémoire.
    assert hosted.memory.recall(item["id"]) == {
        "name": "web_search", "arguments": "{\"query\": \"gufo\"}",
        "result": "1. gufo.org"}
    end = events(t.finalize())
    assert [e["type"] for e in end] == ["response.completed"]
    assert [o["type"] for o in end[0]["response"]["output"]] == [
        "message", "web_search_call"]
    seq = [e["sequence_number"] for e in ev + done + end]
    assert seq == list(range(len(seq)))
    assert t.finalize() == b"" and t.fail("trop tard") == b""


def test_stream_two_upstream_turns_make_one_response():
    hosted = hosted_tools()
    ctx = R.to_chat(codex_request(), hosted=hosted)[1]
    t = R.Translator(200, "text/event-stream", ctx)
    ev = turn(t, sse(*SEARCH_TURN))
    assert t.tokens(0) == (100, 10, True) and t.cached() == 40
    ev += events(t.resolve(t.pending[0], "1. gufo.org"))
    t.next_turn()
    assert t.turns == 2
    # Tour 2 : les index d'outils OpenAI repartent de 0.
    ev += turn(t, sse(
        chunk({"reasoning_content": "Je lis."}),
        tool_call(0, "call_y", "web_fetch", "{\"url\":\"https://gufo.org\"}"),
        chunk(finish="tool_calls"), usage(150, 5, cached=100, reasoning=1)))
    assert [c["name"] for c in t.pending] == ["web_fetch"]
    assert t.pending[0]["index"] == 3
    ev += events(t.resolve(t.pending[0], "Gufo, le hibou."))
    t.next_turn()
    ev += turn(t, sse(chunk({"content": "Voilà."}), chunk(finish="stop"),
                      usage(200, 7, cached=150)))
    kinds = [e["type"] for e in ev]
    # Une seule ouverture, une seule clôture, numérotation continue.
    assert kinds.count("response.created") == 1
    assert kinds.count("response.completed") == 1 and kinds[-1] == "response.completed"
    assert [e["sequence_number"] for e in ev] == list(range(len(ev)))
    added = [e for e in ev if e["type"] == "response.output_item.added"]
    assert [(e["output_index"], e["item"]["type"]) for e in added] == [
        (0, "message"), (1, "web_search_call"), (2, "reasoning"),
        (3, "web_search_call"), (4, "message")]
    done = [e for e in ev if e["type"] == "response.output_item.done"]
    assert [e["output_index"] for e in done] == [0, 1, 2, 3, 4]
    final = ev[-1]["response"]
    assert final["id"] == ev[0]["response"]["id"]
    assert [o["type"] for o in final["output"]] == [
        "message", "web_search_call", "reasoning", "web_search_call", "message"]
    assert final["output"][3]["action"] == {"type": "open_page",
                                            "url": "https://gufo.org"}
    assert final["output"] == t.output
    # Usage CUMULÉ sur les trois tours.
    assert final["usage"] == {
        "input_tokens": 450, "input_tokens_details": {"cached_tokens": 290},
        "output_tokens": 22, "output_tokens_details": {"reasoning_tokens": 4},
        "total_tokens": 472}
    assert t.tokens(0) == (450, 22, True) and t.cached() == 290
    assert not t.pending


def test_stream_hosted_and_client_calls_in_one_turn():
    ctx = R.to_chat(codex_request(), hosted=hosted_tools())[1]
    t = R.Translator(200, "text/event-stream", ctx)
    ev = turn(t, sse(
        tool_call(0, "call_x", "web_search", "{\"query\":\"gufo\"}"),
        tool_call(1, "call_a", "exec_command", "{\"cmd\":\"ls\"}"),
        tool_call(2, "call_y", "web_fetch", "{\"url\":\"https://gufo.org\"}"),
        chunk(finish="tool_calls")))
    kinds = [e["type"] for e in ev]
    assert "response.completed" not in kinds
    assert kinds.count("response.function_call_arguments.delta") == 1
    # L'appel du client est rendu tout de suite, à sa place ; les deux
    # autres attendent.
    assert t.client_calls == 1
    assert [(c["name"], c["index"]) for c in t.pending] == [
        ("web_search", 0), ("web_fetch", 2)]
    fc = next(e for e in ev if e["type"] == "response.output_item.done")
    assert fc["output_index"] == 1 and fc["item"]["call_id"] == "call_a"
    for call in list(t.pending):
        ev += events(t.resolve(call, "ok"))
    ev += events(t.finalize())
    assert ev[-1]["type"] == "response.completed"
    out = ev[-1]["response"]["output"]
    assert [(o["type"], o["status"]) for o in out] == [
        ("web_search_call", "completed"), ("function_call", "completed"),
        ("web_search_call", "completed")]
    assert [e["sequence_number"] for e in ev] == list(range(len(ev)))


def test_stream_failure_on_a_later_turn():
    ctx = R.to_chat(codex_request(), hosted=hosted_tools())[1]
    t = R.Translator(200, "text/event-stream", ctx)
    ev = turn(t, sse(*SEARCH_TURN))
    ev += events(t.resolve(t.pending[0], "ok"))
    ev += events(t.fail("backend «bigchuck» hors ligne"))
    assert ev[-1]["type"] == "response.failed"
    resp = ev[-1]["response"]
    assert resp["status"] == "failed" and resp["error"] == {
        "code": "server_error", "message": "backend «bigchuck» hors ligne"}
    assert [o["type"] for o in resp["output"]] == ["message", "web_search_call"]
    assert resp["usage"]["input_tokens"] == 100
    assert t.finalize() == b""
    # Erreur DANS le flux d'un tour : plus rien à exécuter.
    t = R.Translator(200, "text/event-stream", ctx)
    ev = turn(t, sse(tool_call(0, "call_x", "web_search", "{}"),
                     {"error": {"message": "GPU perdu"}}, done=False))
    assert ev[-1]["type"] == "response.failed" and not t.pending


def chat_doc(message, finish, prompt, completion, cached=0):
    return json.dumps({
        "created": 1700000000,
        "choices": [{"finish_reason": finish, "message": message}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                  "prompt_tokens_details": {"cached_tokens": cached}},
    }).encode()


def test_json_hosted_call_then_answer():
    hosted = hosted_tools()
    ctx = R.to_chat(codex_request(stream=False), hosted=hosted)[1]
    t = R.Translator(200, "application/json", ctx)
    t.feed(chat_doc({"content": "Je cherche.", "tool_calls": [
        {"id": "call_x", "function": {"name": "web_search",
                                      "arguments": "{\"query\":\"gufo\"}"}}]},
        "tool_calls", 100, 10, cached=40))
    # Rien ne part : la réponse n'est pas finie.
    assert t.finish() == b"" and t.client_calls == 0
    call = t.pending[0]
    assert call == {"item_id": call["item_id"], "name": "web_search",
                    "arguments": "{\"query\":\"gufo\"}", "index": 1}
    assert t.resolve(call, "1. gufo.org") == b"" and not t.pending
    assert hosted.memory.recall(call["item_id"])["result"] == "1. gufo.org"
    t.next_turn()
    t.feed(chat_doc({"content": "Voilà."}, "stop", 150, 5, cached=100))
    resp = json.loads(t.finish())
    assert resp["status"] == "completed" and resp["object"] == "response"
    assert resp["created_at"] == 1700000000
    assert [o["type"] for o in resp["output"]] == [
        "message", "web_search_call", "message"]
    assert resp["output"][1] == {
        "id": call["item_id"], "type": "web_search_call",
        "status": "completed", "action": {"type": "search", "query": "gufo"}}
    assert resp["usage"]["input_tokens"] == 250
    assert resp["usage"]["input_tokens_details"] == {"cached_tokens": 140}
    assert t.tokens(0) == (250, 15, True) and t.cached() == 140
    assert t.finalize() == b""


def test_json_hosted_with_client_call_and_failure():
    ctx = R.to_chat(codex_request(stream=False), hosted=hosted_tools())[1]
    doc = chat_doc({"content": None, "tool_calls": [
        {"id": "call_x", "function": {"name": "web_search", "arguments": "{}"}},
        {"id": "call_a", "function": {"name": "exec_command",
                                      "arguments": "{}"}}]},
        "tool_calls", 10, 2)
    t = R.Translator(200, "application/json", ctx)
    t.feed(doc)
    assert t.finish() == b"" and t.client_calls == 1
    t.resolve(t.pending[0], "ok")
    resp = json.loads(t.finalize())
    assert [(o["type"], o["status"]) for o in resp["output"]] == [
        ("web_search_call", "completed"), ("function_call", "completed")]
    assert resp["status"] == "completed"
    # Tour ultérieur en échec : un objet Response `failed`, le 200 est parti.
    t = R.Translator(200, "application/json", ctx)
    t.feed(doc)
    t.finish()
    t.resolve(t.pending[0], "ok")
    resp = json.loads(t.fail("quota épuisé"))
    assert resp["status"] == "failed"
    assert resp["error"] == {"code": "server_error", "message": "quota épuisé"}
    assert resp["usage"]["input_tokens"] == 10


def replayed(item_id, action):
    return {"type": "web_search_call", "id": item_id, "status": "completed",
            "action": action}


def test_to_chat_replays_hosted_call_from_memory(monkeypatch):
    hosted = hosted_tools(tools.Memory(8, 60))
    hosted.memory.store("ws_1", "web_search", "{\"query\": \"gufo\", \"limit\": 3}",
                        "1. gufo.org")
    p = codex_request()
    p["input"] += [
        {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "Je cherche."}]},
        replayed("ws_1", {"type": "search", "query": "gufo"}),
        user("Merci"),
    ]
    out, ctx = R.to_chat(p, hosted=hosted)
    # L'appel revient tel que le modèle l'avait écrit (pas depuis l'action,
    # qui a perdu `limit`), l'id de l'élément servant d'id d'appel.
    assert out["messages"][3:] == [
        {"role": "assistant", "content": "Je cherche.", "tool_calls": [
            {"id": "ws_1", "type": "function", "function": {
                "name": "web_search",
                "arguments": "{\"query\": \"gufo\", \"limit\": 3}"}}]},
        {"role": "tool", "tool_call_id": "ws_1", "content": "1. gufo.org"},
        {"role": "user", "content": "Merci"},
    ]
    assert ctx.ignored == []

    # Entrée expirée (ou proxy redémarré) : reconstruit depuis l'action,
    # avec un résultat qui dit qu'il n'est plus là.
    start = tools.time.monotonic()
    monkeypatch.setattr(tools.time, "monotonic", lambda: start + 61)
    p["input"].insert(-1, replayed("ws_2", {"type": "open_page",
                                           "url": "https://gufo.org"}))
    out, _ = R.to_chat(p, hosted=hosted)
    assert out["messages"][3]["tool_calls"][0]["function"] == {
        "name": "web_search", "arguments": "{\"query\": \"gufo\"}"}
    assert out["messages"][4]["content"] == hosted.expired
    assert out["messages"][5]["tool_calls"] == [
        {"id": "ws_2", "type": "function", "function": {
            "name": "web_fetch", "arguments": "{\"url\": \"https://gufo.org\"}"}}]
    assert out["messages"][6] == {"role": "tool", "tool_call_id": "ws_2",
                                  "content": hosted.expired}
    assert len(hosted.memory) == 0
    # Sans annuaire, l'élément est écarté comme avant.
    out, ctx = R.to_chat(p)
    assert [m["role"] for m in out["messages"][3:]] == ["assistant", "user"]
    assert ctx.ignored.count("web_search_call") == 2


def test_loop_turn_and_client_replay_send_the_same_bytes():
    """Ce que le backend reçoit au tour 2 de la boucle est, octet pour
    octet, le début de ce qu'il recevra quand le client rejouera la
    réponse : le préfixe en cache reste valide."""
    hosted = hosted_tools()
    request = codex_request()
    first, ctx = R.to_chat(request, hosted=hosted)
    t = R.Translator(200, "text/event-stream", ctx)
    turn(t, sse(chunk({"reasoning_content": "Hum."}), *SEARCH_TURN))
    t.resolve(t.pending[0], "1. gufo.org")
    # Tour 2, comme app.hosted_stream le reconstruit.
    looped, _ = R.to_chat({**request, "input": request["input"] + t.output},
                          hosted=hosted)
    assert looped["messages"][:3] == first["messages"]
    assert [m["role"] for m in looped["messages"][3:]] == ["assistant", "tool"]
    assert looped["messages"][4]["content"] == "1. gufo.org"
    t.next_turn()
    final = turn(t, sse(*ANSWER_TURN))[-1]["response"]
    # Requête suivante du client : sa copie des éléments, passée par JSON.
    again, _ = R.to_chat({**request, "input": request["input"]
                          + json.loads(json.dumps(final["output"]))
                          + [user("Merci")]}, hosted=hosted)
    n = len(looped["messages"])
    encode = lambda messages: json.dumps(messages, ensure_ascii=False)
    assert encode(again["messages"][:n]) == encode(looped["messages"])
    assert again["messages"][n:] == [
        {"role": "assistant", "content": "Voilà."},
        {"role": "user", "content": "Merci"}]
    assert encode(again["tools"]) == encode(looped["tools"]) == encode(first["tools"])


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


@pytest.fixture
def proxy(monkeypatch):
    """Un backend de test sans quota, les outils factices, et de quoi
    lire ce qui part au backend (`sent`) et aux stats (`lines`)."""
    env = types.SimpleNamespace(replies=[], sent=[], lines=[], runs=[],
                                hosted=hosted_tools())
    for module in env.hosted.modules:
        async def run(args, name=module.NAME):
            env.runs.append((name, args))
            return f"résultat de {name}"
        module.run = run

    async def send_upstream(call, request, path, body):
        env.sent.append(json.loads(body))
        reply = env.replies.pop(0)
        if isinstance(reply, tuple):       # backend injoignable
            return call.error(*reply)
        return reply

    monkeypatch.setitem(A.BACKENDS, "essai",
                        Backend("essai", {"url": "http://backend.invalid"}))
    monkeypatch.setattr(A, "PROXY_API_KEYS", [])
    monkeypatch.setattr(A.responses_api, "ENABLED", True)
    monkeypatch.setattr(A.tools, "Hosted", lambda: env.hosted)
    monkeypatch.setattr(A, "send_upstream", send_upstream)
    monkeypatch.setattr(A.stats, "record", lambda *a: env.lines.append(a))
    env.client = TestClient(A.app)
    env.post = lambda **extra: env.client.post(
        "/v1/responses", json=codex_request(model="essai/qwen", **extra))
    return env


def test_app_loop_runs_hosted_calls_until_the_answer(proxy):
    ups = [FakeUpstream(sse(*SEARCH_TURN)), FakeUpstream(sse(*ANSWER_TURN))]
    proxy.replies = list(ups)
    r = proxy.post()
    assert r.status_code == 200
    ev = events(r.content)
    kinds = [e["type"] for e in ev]
    assert kinds.count("response.created") == 1
    assert kinds[-1] == "response.completed"
    assert kinds.index("response.web_search_call.searching") \
        < kinds.index("response.web_search_call.completed")
    assert [e["sequence_number"] for e in ev] == list(range(len(ev)))
    final = ev[-1]["response"]
    assert [o["type"] for o in final["output"]] == [
        "message", "web_search_call", "message"]
    assert final["usage"]["input_tokens"] == 250
    assert proxy.runs == [("web_search", {"query": "gufo"})]
    # Deux envois au backend, préfixe retiré ; le second porte l'appel et
    # son résultat, et rien d'autre ne change avant eux.
    one, two = proxy.sent
    assert one["model"] == two["model"] == "qwen" and one["tools"] == two["tools"]
    assert two["messages"][:len(one["messages"])] == one["messages"]
    ws = final["output"][1]["id"]
    assert two["messages"][len(one["messages"]):] == [
        {"role": "assistant", "content": "Je cherche.", "tool_calls": [
            {"id": ws, "type": "function", "function": {
                "name": "web_search", "arguments": "{\"query\": \"gufo\"}"}}]},
        {"role": "tool", "tool_call_id": ws,
         "content": "résultat de web_search"}]
    # UNE ligne de stats, usage cumulé : (clé, backend, modèle, endpoint,
    # statut, durée, prompt, completion, exact, flux, cache).
    assert len(proxy.lines) == 1
    line = proxy.lines[0]
    assert line[:5] == ("essai/qwen", "essai", "qwen", "/v1/responses", 200)
    assert line[6:] == (250, 15, True, True, 140)
    assert all(u.closed for u in ups) and not proxy.replies


def test_app_loop_hands_back_when_the_client_is_called_too(proxy):
    up = FakeUpstream(sse(
        tool_call(0, "call_x", "web_search", "{\"query\":\"gufo\"}"),
        tool_call(1, "call_a", "exec_command", "{}"),
        chunk(finish="tool_calls"), usage(10, 2)))
    proxy.replies = [up]
    ev = events(proxy.post().content)
    assert ev[-1]["type"] == "response.completed"
    assert [(o["type"], o["status"]) for o in ev[-1]["response"]["output"]] == [
        ("web_search_call", "completed"), ("function_call", "completed")]
    # L'appel hébergé est exécuté, mais pas de second tour : au client.
    assert len(proxy.runs) == 1 and len(proxy.sent) == 1 and up.closed
    assert proxy.lines[0][6:8] == (10, 2)


def test_app_loop_json_mode(proxy):
    proxy.replies = [
        FakeUpstream(chat_doc({"content": None, "tool_calls": [{
            "id": "call_x", "function": {
                "name": "web_fetch", "arguments": "{\"url\":\"https://gufo.org\"}"}}]},
            "tool_calls", 100, 10), content_type="application/json"),
        FakeUpstream(chat_doc({"content": "Voilà."}, "stop", 150, 5),
                     content_type="application/json")]
    r = proxy.post(stream=False)
    resp = r.json()
    assert resp["status"] == "completed"
    assert resp["output"][0]["action"] == {"type": "open_page",
                                           "url": "https://gufo.org"}
    assert resp["output"][1]["content"][0]["text"] == "Voilà."
    assert resp["usage"]["total_tokens"] == 265
    assert proxy.lines[0][6:10] == (250, 15, True, False)


def test_app_loop_stops_a_model_that_never_concludes(proxy, monkeypatch):
    monkeypatch.setattr(A, "HOSTED_HARD_LIMIT", 3)
    proxy.replies = [FakeUpstream(sse(
        tool_call(0, f"call_{i}", "web_search", "{\"query\":\"encore\"}"),
        chunk(finish="tool_calls"), usage(10, 1))) for i in range(5)]
    ev = events(proxy.post().content)
    assert ev[-1]["type"] == "response.completed"
    assert len(ev[-1]["response"]["output"]) == 3
    assert len(proxy.sent) == 3 and len(proxy.runs) == 3
    assert proxy.lines[0][6:8] == (30, 3)


def test_app_loop_failure_on_a_later_turn(proxy):
    # Backend éteint au tour 2 : `response.failed`, et la ligne de stats
    # garde ce que le tour 1 a consommé.
    proxy.replies = [FakeUpstream(sse(*SEARCH_TURN)),
                     (503, "backend_offline", "backend «essai» hors ligne")]
    r = proxy.post()
    ev = events(r.content)
    assert r.status_code == 200 and ev[-1]["type"] == "response.failed"
    assert ev[-1]["response"]["error"]["message"] == "backend «essai» hors ligne"
    assert len(proxy.lines) == 1
    assert proxy.lines[0][4] == 503 and proxy.lines[0][6:9] == (100, 10, True)

    # Statut d'erreur upstream au tour 2.
    proxy.lines.clear()
    bad = FakeUpstream(json.dumps({"error": {"message": "contexte dépassé"}})
                       .encode(), status=400, content_type="application/json")
    proxy.replies = [FakeUpstream(sse(*SEARCH_TURN)), bad]
    ev = events(proxy.post().content)
    assert ev[-1]["type"] == "response.failed"
    assert ev[-1]["response"]["error"]["message"] == "contexte dépassé"
    assert bad.closed and proxy.lines[0][4] == 400

    # Erreur dès le PREMIER tour : le statut et le corps d'erreur habituels.
    proxy.lines.clear()
    proxy.replies = [FakeUpstream(b'{"error": {"message": "non"}}', status=500,
                                  content_type="application/json")]
    r = proxy.post()
    assert r.status_code == 500 and r.json()["error"]["message"] == "non"
    assert len(proxy.lines) == 1 and proxy.lines[0][4] == 500


def test_app_without_hosted_tool_takes_the_plain_path(proxy):
    # Le client ne déclare pas `web_search` : relais ordinaire, un tour.
    proxy.replies = [FakeUpstream(sse(chunk({"content": "Bonjour"}),
                                      chunk(finish="stop"), usage(5, 1)))]
    r = proxy.client.post("/v1/responses", json=codex_request(
        model="essai/qwen", tools=[fn("exec_command")]))
    assert events(r.content)[-1]["type"] == "response.completed"
    assert [t["function"]["name"] for t in proxy.sent[0]["tools"]] == [
        "exec_command"]
    assert len(proxy.lines) == 1


def test_app_loop_client_gone_closes_everything(proxy):
    """Le client raccroche en plein tour : le générateur est fermé,
    l'upstream aussi, la ligne de stats est écrite une fois et aucun
    outil n'est lancé pour personne."""
    up = FakeUpstream(sse(*SEARCH_TURN))
    request = codex_request(model="essai/qwen")
    _, ctx = R.to_chat(request, hosted=proxy.hosted)
    robinet = R.Translator(200, "text/event-stream", ctx)
    call = A.Call(A.BACKENDS["essai"], "essai/qwen", "/v1/responses")

    async def scenario():
        stream = A.hosted_stream(call, None, request, False, proxy.hosted,
                                 robinet, up, 0)
        async for out in stream:
            if b"web_search_call.searching" in out:
                break
        await stream.aclose()

    asyncio.run(scenario())
    assert up.closed and len(proxy.lines) == 1
    assert proxy.lines[0][4] == 200 and proxy.lines[0][6:8] == (100, 10)
    assert not proxy.sent and not proxy.runs


# ── blancs seuls avant un appel d'outil ─────────────────────────────────

def test_stream_whitespace_before_tool_call_is_not_a_message():
    _, ev = run(sse(
        chunk({"content": "\n"}), chunk({"content": "\n "}),
        chunk({"tool_calls": [{"index": 0, "id": "call_a", "function": {
            "name": "exec_command", "arguments": "{}"}}]}),
        chunk(finish="tool_calls"),
    ))
    assert [o["type"] for o in ev[-1]["response"]["output"]] == ["function_call"]
    assert "response.output_text.delta" not in [e["type"] for e in ev]


def test_stream_leading_whitespace_is_kept_when_text_follows():
    _, ev = run(sse(chunk({"content": "\n\n"}), chunk({"content": "Paris"}),
                    chunk(finish="stop")))
    out = ev[-1]["response"]["output"]
    assert [o["type"] for o in out] == ["message"]
    assert out[0]["content"][0]["text"] == "\n\nParis"


def test_json_whitespace_only_content_is_not_a_message():
    resp = R.from_chat({"choices": [{"finish_reason": "tool_calls", "message": {
        "content": "\n\n", "tool_calls": [{"id": "c", "function": {
            "name": "exec_command", "arguments": "{}"}}]}}]}, ctx_of())
    assert [o["type"] for o in resp["output"]] == ["function_call"]
