"""Le traducteur Responses ↔ OpenAI chat, testé sur des octets : aucun
réseau, aucun serveur — responses_api ne connaît ni FastAPI ni httpx.
Les corps de requête ont la forme de ceux que Codex CLI 0.157 envoie
(capture du 05/10/2026), réduits.
Les outils hébergés sont testés en fin de fichier, jusqu'à la route
entière. La boucle d'app.py (`hosted_loop`) est commune aux deux surfaces
traduites : c'est ICI qu'elle est déroulée, scénario par scénario ;
test_anthropic_api.py n'en garde que ce qui est propre à la sienne."""

import asyncio
import json

import pytest

from fakes import (ANSWER_DOC, ANSWER_TURN, FOUND, QUERY, SEARCH_DOC,
                   SEARCH_TURN, FakeUpstream, chat_doc, chunk, feed,
                   hosted_tools, sse, sse_events, stream, tool_call, usage)
from llm_proxy import app as A
from llm_proxy import responses_api as R
from llm_proxy import tools
from llm_proxy.tools import web_fetch, web_search


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


def test_to_chat_refuses_what_it_cannot_honor():
    """Ce qui demanderait un état côté serveur, et les outils mal formés :
    refusés (400) avec un message qui nomme la chose, jamais perdus."""
    ns = lambda name, *nested: {"type": "namespace", "name": name,
                                "tools": list(nested)}
    for patch, needle in (
        ({"previous_response_id": "resp_1"}, "previous_response_id"),
        ({"conversation": "conv_1"}, "conversation"),
        ({"background": True}, "background"),
        ({"input": [{"type": "item_reference", "id": "msg_1"}]}, "item_reference"),
        # Un nom présent deux fois une fois les namespaces aplatis.
        ({"tools": [fn("a"), ns("n", fn("a"))]}, "deux fois"),
        ({"tools": [ns("n1", fn("a")), ns("n2", fn("a"))]}, "deux fois"),
        ({"tools": [{"type": "namespace", "tools": [fn("a")]}]}, "mal formé"),
        ({"tools": [ns("n", {"type": "web_search"})]}, "function"),
        ({"tools": [{"type": "function"}]}, "name"),
    ):
        with pytest.raises(R.Refused, match=needle):
            R.to_chat({"model": "b/m", "input": "x", **patch})


# ── réponse ─────────────────────────────────────────────────────────────

def ctx_of(**extra):
    return R.to_chat(codex_request(**extra))[1]


def events(raw: bytes) -> list[dict]:
    return [data for _, data in sse_events(raw)]


def turn(t, *docs) -> list[dict]:
    """Un tour upstream entier passé au robinet `t`, finish() compris."""
    return events(feed(t, stream(*docs)))


def run(*docs, done=True):
    """Un flux upstream passé à un robinet neuf, sur la requête Codex."""
    t = R.Translator(200, "text/event-stream", ctx_of())
    return t, events(feed(t, stream(*docs) if done else sse(*docs)))


def hosted_translator():
    """Un robinet neuf dont la requête déclare les outils hébergés."""
    ctx = R.to_chat(codex_request(), hosted=hosted_tools())[1]
    return R.Translator(200, "text/event-stream", ctx)


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


def test_length_makes_the_response_incomplete():
    resp = R.from_chat({"choices": [{"finish_reason": "length",
                                     "message": {"content": "coup"}}]}, ctx_of())
    _, ev = run(chunk({"content": "coup"}), chunk(finish="length"))
    assert ev[-1]["type"] == "response.incomplete"
    for r in (resp, ev[-1]["response"]):
        assert r["status"] == "incomplete"
        assert r["incomplete_details"] == {"reason": "max_output_tokens"}


def test_stream_text():
    t, ev = run(
        chunk({"role": "assistant", "content": ""}),
        chunk({"content": "Bon"}), chunk({"content": "jour"}),
        chunk(finish="stop"), usage(19, 2))
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
    _, ev = run(
        chunk({"content": "Je lance."}),
        tool_call(0, "call_a", "exec_command", ""),
        chunk({"tool_calls": [{"index": 0, "function": {"arguments": "{\"cmd\":"}}]}),
        chunk({"tool_calls": [{"index": 0, "function": {"arguments": "\"ls\"}"}}]}),
        tool_call(1, "call_b", "spawn_agent", "{}"),
        chunk(finish="tool_calls"))
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


def test_stream_reasoning_then_text(monkeypatch):
    docs = (chunk({"reasoning_content": "Hum"}), chunk({"reasoning_content": "."}),
            chunk({"content": "Paris"}), chunk(finish="stop"))
    _, ev = run(*docs)
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
    # [responses].reasoning_as_summary = false : le raisonnement est jeté.
    monkeypatch.setattr(R, "REASONING_AS_SUMMARY", False)
    _, ev = run(*docs)
    assert [o["type"] for o in ev[-1]["response"]["output"]] == ["message"]


def test_stream_error_mid_flight_and_missing_done():
    _, ev = run(chunk({"content": "dé"}),
                {"error": {"message": "GPU perdu", "type": "server_error"}},
                done=False)
    assert ev[-1]["type"] == "response.failed"
    assert ev[-1]["response"]["status"] == "failed"
    assert ev[-1]["response"]["error"]["message"] == "GPU perdu"
    # Flux coupé sans [DONE] : la réponse est close quand même.
    _, ev = run(chunk({"content": "dé"}), done=False)
    assert ev[-1]["type"] == "response.completed"
    # Un appel hébergé était en cours : plus rien à exécuter.
    t = hosted_translator()
    ev = events(feed(t, sse(tool_call(0, "call_x", "web_search", "{}"),
                            {"error": {"message": "GPU perdu"}})))
    assert ev[-1]["type"] == "response.failed" and not t.pending


def test_json_response_and_upstream_errors():
    t = R.Translator(200, "application/json", ctx_of())
    assert t.feed(chat_doc({"content": "Paris"}, "stop", 5, 1, cached=3)) == b""
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


def test_whitespace_alone_before_a_tool_call_is_not_a_message():
    """Certains gabarits émettent des blancs avant un appel d'outil : pas
    de message vide ; suivis d'un texte, ils sont gardés."""
    call = tool_call(0, "call_a", "exec_command", "{}")
    _, ev = run(chunk({"content": "\n"}), chunk({"content": "\n "}), call,
                chunk(finish="tool_calls"))
    assert [o["type"] for o in ev[-1]["response"]["output"]] == ["function_call"]
    assert "response.output_text.delta" not in [e["type"] for e in ev]
    resp = R.from_chat({"choices": [{"finish_reason": "tool_calls", "message": {
        "content": "\n\n", "tool_calls": [{"id": "c", "function": {
            "name": "exec_command", "arguments": "{}"}}]}}]}, ctx_of())
    assert [o["type"] for o in resp["output"]] == ["function_call"]

    _, ev = run(chunk({"content": "\n\n"}), chunk({"content": "Paris"}),
                chunk(finish="stop"))
    out = ev[-1]["response"]["output"]
    assert [o["type"] for o in out] == ["message"]
    assert out[0]["content"][0]["text"] == "\n\nParis"


# ── outils hébergés ─────────────────────────────────────────────────────
# Aucun réseau : les faux outils de tests/fakes.py, une mémoire propre à
# chaque test.

SEARCH = {"type": "search", "query": "llama.cpp latest release"}


def test_to_chat_declares_hosted_functions():
    hosted = hosted_tools()
    out, ctx = R.to_chat(codex_request(), hosted=hosted)
    names = [t["function"]["name"] for t in out["tools"]]
    assert names == ["exec_command", "spawn_agent", "wait_agent",
                     "web_search", "web_fetch"]
    assert out["tools"][3:] == [web_search.DEFINITION, web_fetch.DEFINITION]
    assert list(ctx.hosted) == ["web_search", "web_fetch"] and ctx.ignored == []
    # Le client voit revenir SES outils, pas les fonctions du proxy.
    assert ctx.echo["tools"][-1] == {"type": "web_search",
                                     "external_web_access": True}
    # Sans web_fetch, la recherche ne renvoie pas à lui.
    alone = tools.Hosted(modules=hosted.modules[:1])
    assert R.to_chat(codex_request(), hosted=alone)[0]["tools"][3:] == [
        web_search.definition(fetch=False)]
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
    ev = turn(t, tool_call(0, "call_x", "web_search", "{}"),
              chunk(finish="tool_calls"))
    assert not t.pending
    assert ev[-1]["response"]["output"][0]["type"] == "function_call"


def test_stream_hosted_calls_span_upstream_turns():
    """Un appel hébergé dans le robinet : le client voit un élément
    `web_search_call` s'ouvrir, jamais un `function_call` ; la réponse
    reste ouverte jusqu'au résultat (resolve), dont il ne reçoit que
    l'action. Trois tours upstream font UNE réponse : une ouverture, une
    clôture, des éléments numérotés à la suite, et l'usage CUMULÉ."""
    t = hosted_translator()
    memory = t.ctx.memory
    ev = turn(t, *SEARCH_TURN)
    assert [e["type"] for e in ev] == [
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
    assert item["id"].startswith("ws_")
    assert [(e["output_index"], e["item_id"]) for e in ev[9:]] == [
        (1, item["id"])] * 2
    assert t.pending == [{"item_id": item["id"], "name": "web_search",
                          "arguments": QUERY, "index": 1}]
    assert t.client_calls == 0 and memory.recall(item["id"]) is None
    assert t.tokens(0) == (100, 10, True) and t.cached() == 40

    done = events(t.resolve(t.pending[0], FOUND))
    assert [e["type"] for e in done] == [
        "response.web_search_call.completed", "response.output_item.done"]
    assert done[0]["output_index"] == 1 and done[0]["item_id"] == item["id"]
    assert done[1]["item"] == {"id": item["id"], "type": "web_search_call",
                               "status": "completed", "action": SEARCH}
    # Le résultat ne part pas au client : il attend son rejeu en mémoire.
    assert memory.recall(item["id"]) == {
        "name": "web_search", "arguments": QUERY, "result": FOUND}

    # Tour 2 : les index d'outils OpenAI repartent de 0.
    t.next_turn()
    ev += done + turn(
        t, chunk({"reasoning_content": "Je lis."}),
        tool_call(0, "call_y", "web_fetch", "{\"url\":\"https://gufo.org\"}"),
        chunk(finish="tool_calls"), usage(150, 5, cached=100, reasoning=1))
    assert [(c["name"], c["index"]) for c in t.pending] == [("web_fetch", 3)]
    ev += events(t.resolve(t.pending[0], "Gufo, le hibou."))
    t.next_turn()
    ev += turn(t, chunk({"content": "Voilà."}), chunk(finish="stop"),
               usage(200, 7, cached=150))
    kinds = [e["type"] for e in ev]
    assert kinds.count("response.created") == 1
    assert kinds.count("response.completed") == 1 and kinds[-1] == "response.completed"
    assert [e["sequence_number"] for e in ev] == list(range(len(ev)))
    for moment in ("added", "done"):
        assert [(e["output_index"], e["item"]["type"]) for e in ev
                if e["type"] == "response.output_item." + moment] == [
            (0, "message"), (1, "web_search_call"), (2, "reasoning"),
            (3, "web_search_call"), (4, "message")]
    final = ev[-1]["response"]
    assert final["id"] == ev[0]["response"]["id"]
    assert final["output"] == t.output and not t.pending
    assert final["output"][3]["action"] == {"type": "open_page",
                                            "url": "https://gufo.org"}
    assert final["usage"] == {
        "input_tokens": 450, "input_tokens_details": {"cached_tokens": 290},
        "output_tokens": 22, "output_tokens_details": {"reasoning_tokens": 4},
        "total_tokens": 472}
    assert t.tokens(0) == (450, 22, True) and t.cached() == 290
    assert t.finalize() == b"" and t.fail("trop tard") == b""


def replayed(item_id, action):
    return {"type": "web_search_call", "id": item_id, "status": "completed",
            "action": action}


def test_to_chat_replays_hosted_call_from_memory(monkeypatch):
    hosted = hosted_tools()
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
    turn(t, chunk({"reasoning_content": "Hum."}), *SEARCH_TURN)
    t.resolve(t.pending[0], FOUND)
    # Tour 2, comme app.hosted_stream le reconstruit.
    looped, _ = R.to_chat({**request, "input": request["input"] + t.output},
                          hosted=hosted)
    assert looped["messages"][:3] == first["messages"]
    assert [m["role"] for m in looped["messages"][3:]] == ["assistant", "tool"]
    assert looped["messages"][4]["content"] == FOUND
    t.next_turn()
    final = turn(t, *ANSWER_TURN)[-1]["response"]
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
# La route entière (fixture `proxy` de conftest.py) : seul l'envoi au
# backend est remplacé.

def post(proxy, **extra):
    return proxy.client.post("/v1/responses",
                             json=codex_request(model="essai/qwen", **extra))


def test_app_loop_runs_hosted_calls_until_the_answer(proxy):
    ups = [FakeUpstream(stream(*SEARCH_TURN)), FakeUpstream(stream(*ANSWER_TURN))]
    proxy.replies = list(ups)
    r = post(proxy)
    assert r.status_code == 200
    ev = events(r.content)
    assert [e["sequence_number"] for e in ev] == list(range(len(ev)))
    final = ev[-1]["response"]
    assert ev[-1]["type"] == "response.completed"
    assert [o["type"] for o in final["output"]] == [
        "message", "web_search_call", "message"]
    assert final["usage"]["input_tokens"] == 250
    assert proxy.hosted.runs == [
        ("web_search", {"query": "llama.cpp latest release"}, {})]
    # Deux envois au backend, préfixe retiré ; le second porte l'appel et
    # son résultat, et rien d'autre ne change avant eux.
    one, two = proxy.sent
    assert one["model"] == two["model"] == "qwen" and one["tools"] == two["tools"]
    assert two["messages"][:len(one["messages"])] == one["messages"]
    ws = final["output"][1]["id"]
    assert two["messages"][len(one["messages"]):] == [
        {"role": "assistant", "content": "Je cherche.", "tool_calls": [
            {"id": ws, "type": "function", "function": {
                "name": "web_search", "arguments": QUERY}}]},
        {"role": "tool", "tool_call_id": ws, "content": FOUND}]
    # UNE ligne de stats, usage cumulé.
    assert len(proxy.lines) == 1
    line = proxy.lines[0]
    assert line[:5] == ("essai/qwen", "essai", "qwen", "/v1/responses", 200)
    assert line[6:] == (250, 15, True, True, 140)
    assert all(u.closed for u in ups) and not proxy.replies


def test_app_loop_json_mode(proxy):
    proxy.replies = [FakeUpstream(SEARCH_DOC), FakeUpstream(ANSWER_DOC)]
    resp = post(proxy, stream=False).json()
    assert resp["object"] == "response" and resp["status"] == "completed"
    assert resp["created_at"] == 1700000000
    assert [o["type"] for o in resp["output"]] == [
        "message", "web_search_call", "message"]
    assert resp["output"][1] == {
        "id": resp["output"][1]["id"], "type": "web_search_call",
        "status": "completed", "action": SEARCH}
    assert resp["output"][2]["content"][0]["text"] == "Voilà."
    assert resp["usage"]["total_tokens"] == 265
    assert resp["usage"]["input_tokens_details"] == {"cached_tokens": 140}
    assert "stream" not in proxy.sent[1]
    assert proxy.sent[1]["messages"][-1]["content"] == FOUND
    assert len(proxy.lines) == 1
    assert proxy.lines[0][6:] == (250, 15, True, False, 140)


def test_app_loop_hands_back_when_the_client_is_called_too(proxy):
    """Un tour qui mêle appels hébergés et appel du client, en flux puis
    en JSON : les premiers sont exécutés, l'appel du client est rendu à sa
    place, et la main lui revient — pas de second tour."""
    calls = [("call_x", "web_search", "{\"query\":\"gufo\"}"),
             ("call_a", "exec_command", "{\"cmd\":\"ls\"}"),
             ("call_y", "web_fetch", "{\"url\":\"https://gufo.org\"}")]
    ups = [
        FakeUpstream(stream(*[tool_call(i, *c) for i, c in enumerate(calls)],
                            chunk(finish="tool_calls"), usage(10, 2))),
        FakeUpstream(chat_doc({"content": None, "tool_calls": [
            {"id": call_id, "function": {"name": name, "arguments": arguments}}
            for call_id, name, arguments in calls]}, "tool_calls", 10, 2)),
    ]
    for up, streamed in zip(ups, (True, False)):
        proxy.replies = [up]
        r = post(proxy, stream=streamed)
        final = events(r.content)[-1]["response"] if streamed else r.json()
        assert final["status"] == "completed"
        assert [(o["type"], o["status"]) for o in final["output"]] == [
            ("web_search_call", "completed"), ("function_call", "completed"),
            ("web_search_call", "completed")]
        assert final["output"][1]["call_id"] == "call_a"
        assert up.closed
    assert [name for name, _, _ in proxy.hosted.runs] == [
        "web_search", "web_fetch"] * 2
    assert len(proxy.sent) == 2
    assert [line[6:8] for line in proxy.lines] == [(10, 2)] * 2


def test_app_loop_stops_a_model_that_never_concludes(proxy, monkeypatch):
    monkeypatch.setattr(A, "HOSTED_HARD_LIMIT", 3)
    proxy.replies = [FakeUpstream(stream(
        tool_call(0, f"call_{i}", "web_search", "{\"query\":\"encore\"}"),
        chunk(finish="tool_calls"), usage(10, 1))) for i in range(5)]
    ev = events(post(proxy, tool_choice="required").content)
    assert ev[-1]["type"] == "response.completed"
    assert len(ev[-1]["response"]["output"]) == 3
    assert len(proxy.sent) == 3 and len(proxy.hosted.runs) == 3
    assert proxy.lines[0][6:8] == (30, 3)
    # `tool_choice` forcé par le client : appliqué au premier tour
    # seulement, sinon le modèle ne pourrait jamais conclure.
    assert [s["tool_choice"] for s in proxy.sent] == ["required", "auto", "auto"]


def test_app_loop_failure_on_a_later_turn(proxy):
    """Le 200 est parti avec le premier tour : un échec ensuite se dit par
    `response.failed` (un objet Response `failed` en JSON), et la ligne
    de stats garde ce que le tour 1 a consommé."""
    offline = (503, "backend_offline", "backend «essai» hors ligne")
    refused = FakeUpstream(json.dumps(
        {"error": {"message": "contexte dépassé"}}).encode(), status=400)
    for first, second, status, message in (
        (stream(*SEARCH_TURN), offline, 503, "backend «essai» hors ligne"),
        (stream(*SEARCH_TURN), refused, 400, "contexte dépassé"),
        (SEARCH_DOC, offline, 503, "backend «essai» hors ligne"),
    ):
        proxy.lines.clear()
        proxy.replies = [FakeUpstream(first), second]
        r = post(proxy, stream=first is not SEARCH_DOC)
        resp = r.json() if first is SEARCH_DOC \
            else events(r.content)[-1]["response"]
        assert r.status_code == 200 and resp["status"] == "failed"
        assert resp["error"] == {"code": "server_error", "message": message}
        assert [o["type"] for o in resp["output"]] == ["message", "web_search_call"]
        assert len(proxy.lines) == 1 and proxy.lines[0][4] == status
        assert proxy.lines[0][6:9] == (100, 10, True)
    assert refused.closed

    # Erreur dès le PREMIER tour : le statut et le corps d'erreur habituels.
    proxy.lines.clear()
    proxy.replies = [FakeUpstream(b'{"error": {"message": "non"}}', status=500)]
    r = post(proxy)
    assert r.status_code == 500 and r.json()["error"]["message"] == "non"
    assert len(proxy.lines) == 1 and proxy.lines[0][4] == 500


def test_app_without_hosted_call_answers_in_one_turn(proxy):
    answer = lambda: FakeUpstream(stream(
        chunk({"content": "Bonjour"}), chunk(finish="stop"), usage(5, 1)))
    # Le client ne déclare pas `web_search` : relais ordinaire.
    proxy.replies = [answer()]
    r = proxy.client.post("/v1/responses", json=codex_request(
        model="essai/qwen", tools=[fn("exec_command")]))
    assert events(r.content)[-1]["type"] == "response.completed"
    assert [t["function"]["name"] for t in proxy.sent[0]["tools"]] == [
        "exec_command"]
    # Il le déclare, le modèle ne s'en sert pas : la boucle, un seul tour.
    proxy.replies = [answer()]
    ev = events(post(proxy).content)
    assert ev[-1]["type"] == "response.completed"
    assert [o["type"] for o in ev[-1]["response"]["output"]] == ["message"]
    assert "web_search" in [t["function"]["name"] for t in proxy.sent[1]["tools"]]
    assert not proxy.hosted.runs and len(proxy.sent) == 2
    assert [line[6:8] for line in proxy.lines] == [(5, 1)] * 2


def test_app_loop_client_gone_closes_everything(proxy, monkeypatch):
    """Le client raccroche : le générateur est fermé, l'upstream aussi,
    l'outil en cours est annulé, et la ligne de stats est écrite une fois,
    avec ce qui a été consommé."""
    started, cancelled = [], []

    async def endless(args, **options):
        started.append(args)
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append(args)
            raise

    proxy.hosted.by_name["web_search"].run = endless
    monkeypatch.setattr(A.anthropic_api, "PING_INTERVAL", 0.01)
    request = codex_request(model="essai/qwen")

    async def hang_up(marker: bytes):
        up = FakeUpstream(stream(*SEARCH_TURN))
        _, ctx = R.to_chat(request, hosted=proxy.hosted)
        robinet = R.Translator(200, "text/event-stream", ctx)
        call = A.Call(A.BACKENDS["essai"], "essai/qwen", "/v1/responses")
        loop = A.hosted_stream(call, None, request, False, proxy.hosted,
                               robinet, up, 0)
        async for out in loop:
            if marker in out:
                break
        await loop.aclose()
        await asyncio.sleep(0)      # laisse l'annulation arriver à la tâche
        return up

    # En plein tour upstream : aucun outil n'est lancé pour personne.
    up = asyncio.run(hang_up(b"web_search_call.searching"))
    assert up.closed and not started
    # Pendant la recherche — le commentaire `: ping` qui tient le flux d'un
    # client Responses le temps d'une attente : elle est annulée.
    up = asyncio.run(hang_up(b": ping\n\n"))
    assert up.closed and started and cancelled == started
    assert [(line[4], *line[6:8]) for line in proxy.lines] == [(200, 100, 10)] * 2
    assert not proxy.sent
