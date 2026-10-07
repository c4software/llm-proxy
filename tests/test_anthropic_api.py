"""Le traducteur Anthropic ↔ OpenAI, testé sur des octets : aucun
réseau, aucun serveur — anthropic_api ne connaît ni FastAPI ni httpx.
Les outils hébergés (outils serveur `web_search_…` et `web_fetch_…`) sont
testés en fin de fichier, jusqu'à la route entière par le client de test
de Starlette ;
la boucle d'app.py, commune aux deux surfaces traduites, est déroulée
scénario par scénario dans test_responses_api.py."""

import asyncio
import json
import re
import types

import httpx
import pytest

from fakes import (ANSWER_DOC, ANSWER_TURN, FOUND, QUERY, RESULTS, SEARCH_DOC,
                   SEARCH_TURN, SEARCHED, FakeUpstream, chat_doc, chunk,
                   failed, feed, hosted_tools, sse, stream, tool_call, usage)
from fakes import sse_events as events
from llm_proxy import anthropic_api as A
from llm_proxy import app
from llm_proxy import tools
from llm_proxy.tools import web_fetch, web_search

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

# `events` et `sse` : tests/fakes.py, partagés avec test_responses_api.py.


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
# proxy. Aucun réseau : les faux outils de tests/fakes.py (leurs fonctions
# de texte sont les vraies), et une mémoire propre à chaque test — qui
# doit rester vide, cette surface n'y range rien.

# Ce que le client reçoit pour fakes.RESULTS.
BLOCKS = [
    {"type": "web_search_result", "title": "Releases · ggml-org/llama.cpp",
     "url": "https://github.com/ggml-org/llama.cpp/releases",
     "encrypted_content": "LLM inference in C/C++ — b6789, «latest»…",
     "page_age": "2026-10-03"},
    {"type": "web_search_result", "title": "llama.cpp (blog)",
     "url": "https://example.org/blog/llama", "encrypted_content": "",
     "page_age": None},
]


def search_error(code):
    return {"type": "web_search_tool_result_error", "error_code": code}


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


READ = {"name": "Read", "description": "", "input_schema": {"type": "object"}}


def translator(request=None, hosted=None, content_type="text/event-stream"):
    request = request or claude_code_search()
    ctx = A.Context(request, hosted or hosted_tools())
    return A.Translator(200, content_type, request["model"], ctx)


def turn(t, *docs):
    """Un tour upstream entier passé au robinet `t`, finish() compris."""
    return events(feed(t, stream(*docs)))


def blocks_of(ev) -> list[dict]:
    return [d["content_block"] for e, d in ev if e == "content_block_start"]


def test_to_openai_declares_hosted_search_from_claude_code_request():
    hosted = hosted_tools()
    request = claude_code_search()
    out = A.to_openai(request, hosted=hosted)
    # La fonction du paquet tools/ à la place de l'outil serveur : la même
    # que sur la surface Responses, moins le renvoi à web_fetch — qui
    # n'est PAS présenté (Claude Code ne déclare que la recherche, il lit
    # les pages chez le client), bien que l'annuaire le porte, comme sur
    # un proxy réel.
    assert out["tools"] == [web_search.TOOL.spec({"web_search"})]
    assert "web_fetch" not in json.dumps(out)
    assert out["tool_choice"] == "auto" and out["stream"] is True
    assert out["messages"][0]["role"] == "system"
    assert out["messages"][1] == {
        "role": "user",
        "content": "Perform a web search for the query: llama.cpp latest release"}
    ctx = A.Context(request, hosted)
    assert list(ctx.hosted) == ["web_search"]
    assert ctx.limits == {"web_search": 8} and ctx.options == {}

    # Sans annuaire, ou annuaire sans recherche : ignoré, comme avant.
    for h in (None, tools.Hosted(tools=[]),
              tools.Hosted(tools=hosted.tools[1:])):
        out = A.to_openai(request, hosted=h)
        assert "tools" not in out and "tool_choice" not in out
        assert not A.Context(request, h).hosted


def test_to_openai_hosted_search_variants(monkeypatch):
    hosted = hosted_tools()

    def req(*server, **extra):
        return {"model": "m", "messages": [], "tools": [READ, *server], **extra}

    def names(request):
        return [t["function"]["name"]
                for t in A.to_openai(request, hosted=hosted).get("tools", [])]

    # Toute version datée de l'outil ; à sa place dans la liste ; une fois.
    r = {"model": "m", "messages": [], "tools": [
        {"type": "web_search_20260209", "name": "web_search"}, READ,
        {"type": "web_search_20250305", "name": "web_search", "max_uses": 2}]}
    assert names(r) == ["web_search", "Read"]
    # Le premier déclaré fait foi.
    assert A.Context(r, hosted).limits == {"web_search": 8}
    # Les autres outils serveur restent ignorés, un type non daté aussi.
    r = req({"type": "code_execution_20250825", "name": "code_execution"},
            {"type": "web_search", "name": "web_search"},
            {"type": "web_fetch", "name": "web_fetch"})
    assert names(r) == ["Read"] and not A.Context(r, hosted).hosted
    # max_uses : ne fait que BAISSER la limite du proxy (tools.MAX_CALLS).
    monkeypatch.setattr(tools, "MAX_CALLS", 5)
    for asked, limit in ((3, 3), (50, 5), (None, 5), ("3", 5), (True, 5),
                         (0, 0), (-1, 0)):
        r = req({"type": "web_search_20250305", "name": "web_search",
                 "max_uses": asked})
        assert A.Context(r, hosted).limits == {"web_search": limit}
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
    assert [(c["id"], c["name"], c["arguments"]) for c in t.pending] == [
        (use["id"], "web_search", QUERY)]
    assert t.client_calls == 0

    done = events(t.resolve(t.pending[0], SEARCHED))
    assert [e for e, _ in done] == ["content_block_start", "content_block_stop"]
    assert done[0][1] == {"type": "content_block_start", "index": 2,
                          "content_block": {
                              "type": "web_search_tool_result",
                              "tool_use_id": use["id"], "content": BLOCKS}}
    assert done[1][1] == {"type": "content_block_stop", "index": 2}
    assert not t.pending

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
        {"type": "text", "text": "Voilà."}]
    assert t.summary() == ("stop=end_turn | in=250 out=15 | tools: "
                           "web_search(" + QUERY + ")")
    assert t.finalize() == b"" and t.fail("trop tard") == b""
    # Rien n'a été rangé en mémoire : le bloc porte son résultat.
    assert len(hosted.memory) == 0


def test_stream_two_searches_come_out_as_pairs():
    """Deux recherches lancées d'un coup : le second server_tool_use ne
    sort qu'après le résultat du premier — chaque résultat suit son
    appel, comme chez Anthropic."""
    t = translator()
    ev = turn(t,
              tool_call(0, "call_a", "web_search", "{\"query\":\"un\"}"),
              tool_call(1, "call_b", "web_search", "{\"query\":\"deux\","),
              chunk({"tool_calls": [{"index": 1, "function": {
                  "arguments": "\"limit\":3}"}}]}),
              chunk(finish="tool_calls"), usage(10, 2))
    assert [c["arguments"] for c in t.pending] == [
        "{\"query\":\"un\"}", "{\"query\":\"deux\",\"limit\":3}"]
    assert len(blocks_of(ev)) == 1
    first, second = t.pending
    ev += events(t.resolve(first, SEARCHED))
    ev += events(t.resolve(second, tools.Result("No results for «deux».")))
    t.next_turn()
    ev += turn(t, chunk({"reasoning_content": "Bien."}), *ANSWER_TURN)
    starts = [(d["index"], d["content_block"]["type"]) for e, d in ev
              if e == "content_block_start"]
    assert starts == [
        (0, "server_tool_use"), (1, "web_search_tool_result"),
        (2, "server_tool_use"), (3, "web_search_tool_result"),
        (4, "thinking"), (5, "text")]
    assert [d["index"] for e, d in ev if e == "content_block_stop"] == list(range(6))
    kinds = [e for e, _ in ev]
    assert kinds.count("message_start") == 1 and kinds[-2:] == [
        "message_delta", "message_stop"]
    assert kinds.count("message_delta") == 1
    # Chaque résultat renvoie à l'appel qui le précède ; aucun résultat =
    # liste vide, pas une erreur.
    c = t.content
    assert [c[i + 1]["tool_use_id"] for i in (0, 2)] == [c[i]["id"] for i in (0, 2)]
    assert c[2]["input"] == {"query": "deux", "limit": 3}
    assert c[1]["content"] == BLOCKS and c[3]["content"] == []
    assert ev[-2][1]["usage"]["server_tool_use"] == {"web_search_requests": 2}
    assert t.summary().count("web_search(") == 2


def test_stream_search_errors_become_error_blocks():
    request = claude_code_search()
    request["tools"][0]["max_uses"] = 3
    t = translator(request)
    turn(t, tool_call(0, "a", "web_search", "{\"query\":\"un\"}"),
         tool_call(1, "b", "web_search", "{\"q\":\"sans query\"}"),
         tool_call(2, "c", "web_search", "{pas du json"),
         tool_call(3, "d", "web_search", "{\"query\":\"de trop\"}"),
         tool_call(4, "e", "web_search", "{\"query\":\"lent\"}"),
         chunk(finish="tool_calls"))
    blocks = []
    # Le code du résultat décide de celui du bloc (A.ERROR_CODES) — pas son
    # texte, que seul le modèle lit.
    for code, text in (
            ("unavailable", "Error: search engine unreachable (ConnectError)."),
            ("invalid_input", "Error: `query` is required."),
            ("invalid_input", "Error: the tool arguments are not a JSON object."),
            ("limit", "Error: the limit of 3 web tool calls for one answer is "
                      "reached. Answer now with what you already have."),
            ("timeout", "Error: web_search timed out after 60 s.")):
        blocks += blocks_of(events(t.resolve(t.pending[0], failed(code, text))))[:1]
        assert blocks[-1]["type"] == "web_search_tool_result"
    assert [b["content"] for b in blocks] == [search_error(code) for code in (
        "unavailable", "invalid_tool_input", "invalid_tool_input",
        "max_uses_exceeded", "unavailable")]
    # Chaque code du contrat a sa traduction.
    assert set(A.ERROR_CODES) == set(tools.ERRORS)
    # Arguments illisibles : un input vide, jamais du JSON cassé au client.
    assert [b["input"] for b in t.content if b["type"] == "server_tool_use"] == [
        {"query": "un"}, {"q": "sans query"}, {}, {"query": "de trop"},
        {"query": "lent"}]
    # Aucune recherche aboutie : pas de `server_tool_use` dans l'usage, et
    # un dernier tour clos sur tool_calls sans outil client = end_turn.
    end = events(t.finalize())
    assert end[0][1]["delta"]["stop_reason"] == "end_turn"
    assert "server_tool_use" not in end[0][1]["usage"]


def test_search_and_client_tool_in_one_turn():
    """L'outil du client est rendu au fil de l'eau ; la recherche attend
    la fin du tour, puis sort avec son résultat — et le message se clôt
    sur `tool_use` : la main revient au client. En flux, puis en JSON."""
    request = claude_code_search()
    request["tools"].append(READ)
    expected = ["tool_use", "server_tool_use", "web_search_tool_result"]
    t = translator(request)
    ev = turn(t, tool_call(0, "call_x", "web_search", "{\"query\":\"un\"}"),
              tool_call(1, "toolu_1", "Read", "{\"p\":\"a\"}"),
              chunk(finish="tool_calls"), usage(10, 2))
    assert t.client_calls == 1 and len(t.pending) == 1
    ev += events(t.resolve(t.pending[0], SEARCHED))
    ev += events(t.finalize())
    assert [b["type"] for b in blocks_of(ev)] == expected
    assert ev[-2][1]["delta"]["stop_reason"] == "tool_use"
    assert t.content[0] == {"type": "tool_use", "id": "toolu_1", "name": "Read",
                            "input": {"p": "a"}}

    t = translator(request, content_type="application/json")
    t.feed(chat_doc({"content": None, "tool_calls": [
        {"id": "call_x", "function": {"name": "web_search", "arguments": QUERY}},
        {"id": "toolu_1", "function": {"name": "Read", "arguments": "{}"}}]},
        "tool_calls", 10, 2))
    assert t.finish() == b"" and t.client_calls == 1
    assert t.resolve(t.pending[0], SEARCHED) == b""
    msg = json.loads(t.finalize())
    assert [b["type"] for b in msg["content"]] == expected
    assert msg["stop_reason"] == "tool_use"


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
             "content": search_error("unavailable")},
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
    t.resolve(t.pending[0], SEARCHED)
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
        {"role": "assistant", "content": "Voilà."},
        {"role": "user", "content": "Merci"}]
    assert encode(again["tools"]) == encode(looped["tools"]) == encode(first["tools"])
    # Dans la boucle, le modèle lit le texte EXACT d'une erreur ; au rejeu
    # il n'en reste que le code — seul cas où les deux diffèrent.
    t = translator(request, hosted)
    turn(t, *SEARCH_TURN)
    t.resolve(t.pending[0], failed(
        "unavailable", "Error: search engine returned HTTP 502."))
    assert rebuilt()["messages"][3]["content"] == \
        "Error: search engine returned HTTP 502."
    t.results.clear()
    assert rebuilt()["messages"][3]["content"] == \
        "Error: the web search failed (unavailable)."


# ── lecture de page hébergée ────────────────────────────────────────────
# L'outil serveur `web_fetch_…`, par le même chemin que la recherche. Ce
# que le modèle lit d'une page : le texte de tools/web_fetch, en-tête
# compris — ici un premier morceau de 30 caractères, la suite par `offset`.

PAGE = "https://example.org/notes"
HTML = ("<html><head><title>Notes : b6789</title></head><body><p>"
        + "Bonjour à tous. " * 4 + "</p></body></html>").encode()
# Le résultat de l'outil, et le texte que le modèle en lit.
PAGE_READ = web_fetch.render(PAGE, "text/html", HTML, "utf-8", 0, 30)
READ_TEXT = PAGE_READ.text
FETCH_ARGS = "{\"url\": \"https://example.org/notes\"}"
SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search"}


def fetch_tool(**extra):
    return {"type": "web_fetch_20250910", "name": "web_fetch", **extra}


def fetch_request(*tools, **extra):
    return {"model": "essai/qwen", "max_tokens": 100, "stream": True,
            "messages": [{"role": "user", "content": f"Lis {PAGE}"}],
            "tools": list(tools or (SEARCH_TOOL, fetch_tool())), **extra}


def fetch_error(code):
    return {"type": "web_fetch_tool_result_error", "error_code": code}


def fetched(block, text=READ_TEXT, url=PAGE, title="Notes : b6789"):
    """Le bloc `web_fetch_tool_result` est-il celui de `text` ? À
    l'horodatage près, dont seule la forme est vérifiée."""
    content = dict(block["content"])
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ",
                        content.pop("retrieved_at"))
    return block["type"] == "web_fetch_tool_result" and content == {
        "type": "web_fetch_result", "url": url, "content": {
            "type": "document", "title": title, "source": {
                "type": "text", "media_type": "text/plain", "data": text}}}


def test_to_openai_declares_hosted_fetch():
    hosted = hosted_tools()
    defs = lambda *tools, h=hosted: A.to_openai(
        fetch_request(*tools), hosted=h).get("tools")
    both = {"web_search", "web_fetch"}
    alone = web_fetch.TOOL.spec({"web_fetch"})
    assert READ_TEXT.startswith(
        f"URL: {PAGE}\nTitle: Notes : b6789\nContent-Type: text/html\n"
        "Characters: 0-30 of 63 (truncated: pass offset=30 to continue)\n\n---\n")
    # Seul déclaré : seul présenté, et sa description ne renvoie pas à une
    # recherche que le modèle n'a pas. Toute version datée de l'outil.
    for kind in ("web_fetch_20250910", "web_fetch_20260318"):
        assert defs(fetch_tool(type=kind)) == [alone]
    assert "web_search" not in json.dumps(alone)
    assert set(alone["function"]["parameters"]["properties"]) == {
        "url", "offset"}
    # Les deux déclarés : chacun à sa place, une fois, et chaque
    # description renvoie à l'autre — les fonctions de la surface Responses.
    assert defs(fetch_tool(), READ, SEARCH_TOOL, fetch_tool())[::2] == [
        web_fetch.TOOL.spec(both), web_search.TOOL.spec(both)]
    # Lecture non hébergée : ignorée, la recherche reste sans renvoi.
    assert defs(SEARCH_TOOL, fetch_tool(), h=tools.Hosted(
        tools=hosted.tools[:1])) == [web_search.TOOL.spec({"web_search"})]
    # Une fonction du client nommée web_fetch garde son nom.
    mine = {"name": "web_fetch", "description": "la mienne", "input_schema": {}}
    assert [t["function"]["description"]
            for t in defs(mine, fetch_tool())] == ["la mienne"]
    assert not A.Context(fetch_request(mine, fetch_tool()), hosted).hosted
    # Ce que le client règle sur CHAQUE outil : sa limite, ses listes de
    # domaines ; `max_content_tokens` en caractères ; `citations` ignoré.
    ctx = A.Context(fetch_request(
        {**SEARCH_TOOL, "max_uses": 3, "blocked_domains": ["x.test"]},
        fetch_tool(max_uses=2, allowed_domains=["example.org"],
                   max_content_tokens=500, citations={"enabled": True})), hosted)
    assert ctx.limits == {"web_search": 3, "web_fetch": 2} and ctx.cap == 8
    assert ctx.options == {
        "web_search": {"blocked_domains": ["x.test"]},
        "web_fetch": {"allowed_domains": ["example.org"], "max_chars": 2000}}
    for size in (0, -5, True, "500", None):
        assert not A.Context(fetch_request(
            fetch_tool(max_content_tokens=size)), hosted).options


def test_stream_hosted_fetch_blocks():
    hosted = hosted_tools()
    t = translator(fetch_request(), hosted)
    ev = turn(t, chunk({"content": "Je lis."}),
              tool_call(0, "call_f", "web_fetch", "{\"url\": \"https://exa"),
              chunk({"tool_calls": [{"index": 0, "function": {
                  "arguments": "mple.org/notes\"}"}}]}),
              chunk(finish="tool_calls"), usage(100, 10))
    # Les mêmes événements que pour une recherche : le bloc server_tool_use
    # entier, son input en un delta, et rien d'autre avant l'exécution.
    use = ev[4][1]["content_block"]
    assert use == {"type": "server_tool_use", "id": use["id"],
                   "name": "web_fetch", "input": {}}
    assert use["id"].startswith("srvtoolu_")
    assert [d["delta"] for e, d in ev[5:] if e == "content_block_delta"] == [
        {"type": "input_json_delta", "partial_json": FETCH_ARGS}]
    assert ev[-1][0] == "content_block_stop" and len(t.pending) == 1

    done = events(t.resolve(t.pending[0], PAGE_READ))
    assert [e for e, _ in done] == ["content_block_start", "content_block_stop"]
    block = done[0][1]["content_block"]
    # Le document porte le texte ENTIER rendu au modèle ; l'URL et le
    # titre sont ceux de `meta` du résultat.
    assert done[0][1]["index"] == 2 and block["tool_use_id"] == use["id"]
    assert fetched(block)
    # Un résultat qui ne dit rien de la page : l'URL demandée, pas de titre.
    bare = tools.Result("du texte")
    assert A._fetch_content({"arguments": FETCH_ARGS}, bare)["url"] == PAGE
    assert fetched({"type": "web_fetch_tool_result", "content": A._fetch_content(
        {"arguments": "{"}, bare)}, "du texte", "", None)

    t.next_turn()
    end = turn(t, *ANSWER_TURN)
    assert end[-2][1]["delta"]["stop_reason"] == "end_turn"
    assert end[-2][1]["usage"]["server_tool_use"] == {"web_fetch_requests": 1}
    assert [b["type"] for b in t.content] == [
        "text", "server_tool_use", "web_fetch_tool_result", "text"]
    assert t.content[1]["input"] == {"url": PAGE}
    assert t.summary().endswith("tools: web_fetch(" + FETCH_ARGS + ")")
    assert len(hosted.memory) == 0


def test_fetch_errors_become_error_codes(monkeypatch):
    """Les erreurs du VRAI outil, par l'exécuteur (aucun réseau : la
    résolution et le transport sont factices) → leur code du contrat →
    les codes de l'outil d'Anthropic."""
    async def public(url, allow_private=False):
        return "https", "93.184.216.34", 443

    def site(request):
        path = request.url.path
        if path == "/png":
            return httpx.Response(200, content=b"\x89PNG",
                                  headers={"content-type": "image/png"})
        if path == "/boucle":
            return httpx.Response(302, headers={"location": "/boucle"})
        if path == "/ailleurs":
            return httpx.Response(302, headers={"location": "https://x.test/"})
        if path == "/panne":
            raise httpx.ConnectError("non")
        return httpx.Response(int(path[1:]))

    for name in ("ALLOWED_DOMAINS", "BLOCKED_DOMAINS"):
        monkeypatch.setattr(web_fetch, name, [])
    monkeypatch.setattr(web_fetch, "ALLOW_PRIVATE", False)
    class Offline(web_fetch.WebFetch):
        async def run(self, args, call):
            return await super().run(args, call, httpx.MockTransport(site))

    real = tools.Hosted([Offline()], tools.Memory(1, 60))
    run = lambda url, **options: asyncio.run(real.run(
        "web_fetch", json.dumps({"url": url}), 0,
        options={"web_fetch": options}))
    # Refusés avant toute requête, par le vrai contrôle d'adresse.
    results = [(run("ftp://example.org/a"), "invalid_tool_input"),
               (run("http://127.0.0.1/admin"), "url_not_allowed")]
    monkeypatch.setattr(web_fetch.net, "public_target", public)
    theirs = {"allowed_domains": ["example.org"], "blocked_domains": ["x.test"]}
    results += [(run(f"https://example.org/{path}", **theirs), code)
                for path, code in (("404", "url_not_accessible"),
                                   ("500", "url_not_accessible"),
                                   ("429", "too_many_requests"),
                                   ("png", "unsupported_content_type"),
                                   ("boucle", "url_not_accessible"),
                                   ("panne", "url_not_accessible"),
                                   # Les listes du CLIENT, à chaque saut.
                                   ("ailleurs", "url_not_allowed"))]
    results += [(run("https://github.com/", **theirs), "url_not_allowed"),
                (failed("timeout", "Error: web_fetch timed out after 60 s."),
                 "unavailable")]
    assert all(r.text.startswith("Error:") and r.error for r, _ in results)
    # Sans `url`, arguments illisibles, et au-delà du `max_uses` de l'outil :
    # toujours par l'exécuteur, qui refuse sans exécuter.
    monkeypatch.setattr(tools, "MAX_CALLS", 50)
    go = lambda *a: asyncio.run(real.run("web_fetch", *a))
    extra = [("{\"uri\": \"x\"}", go("{\"uri\": \"x\"}", 0),
              "invalid_tool_input"),
             ("{pas du json", go("{pas du json", 0), "invalid_tool_input"),
             (FETCH_ARGS, go(FETCH_ARGS, 13, 13), "max_uses_exceeded")]
    assert [r.text for _, r, _ in extra] == [
        "Error: `url` is required.",
        "Error: the tool arguments are not a JSON object.",
        "Error: the limit of 13 web tool calls for one answer is reached. "
        "Answer now with what you already have."]
    calls = [(FETCH_ARGS, *r) for r in results] + extra
    t = translator(fetch_request(fetch_tool(max_uses=len(calls) - 1)))
    turn(t, *(tool_call(i, f"c{i}", "web_fetch", args)
              for i, (args, _, _) in enumerate(calls)),
         chunk(finish="tool_calls"))
    blocks = [blocks_of(events(t.resolve(t.pending[0], result)))[0]
              for _, result, _ in calls]
    assert [(b["type"], b["content"]) for b in blocks] == [
        ("web_fetch_tool_result", fetch_error(code)) for _, _, code in calls]
    end = events(t.finalize())
    assert "server_tool_use" not in end[0][1]["usage"]
    # `max_content_tokens` ne fait que BAISSER la taille d'un morceau.
    monkeypatch.setattr(web_fetch, "MAX_CHARS", 20)
    assert "Characters: 0-20 of 63 " in web_fetch.render(
        PAGE, "text/html", HTML, "utf-8", 0, 10 ** 6).text


def test_fetch_loop_turn_and_client_replay_send_the_same_bytes():
    """La contrainte du rejeu, pour une recherche PUIS deux lectures : ce
    que le backend reçoit dans la boucle est, octet pour octet, le début
    de ce qu'il recevra quand le client rejouera ses blocs — le document
    porte le texte lu, tronqué ou non, rien n'est reconstruit."""
    hosted = hosted_tools()
    request = fetch_request()
    rest = web_fetch.render(PAGE, "text/html", HTML, "utf-8", 30)
    rest = tools.Result(rest.text + "\n[truncated]", meta=rest.meta)
    t = translator(request, hosted)
    turn(t, *SEARCH_TURN)
    t.resolve(t.pending[0], SEARCHED)
    t.next_turn()
    turn(t, tool_call(0, "a", "web_fetch", FETCH_ARGS),
         tool_call(1, "b", "web_fetch", FETCH_ARGS[:-1] + ", \"offset\": 30}"),
         chunk(finish="tool_calls"), usage(10, 2))
    t.resolve(t.pending[0], PAGE_READ)
    t.resolve(t.pending[0], rest)
    rest = rest.text
    rebuilt = lambda results: A.to_openai(
        {**request, "messages": request["messages"] + [
            {"role": "assistant", "content": t.content}]},
        hosted=hosted, results=results)
    looped = rebuilt(t.results)
    # Chaque résultat suit son appel, pour les lectures comme pour les
    # recherches : deux appels lancés d'un coup font deux messages.
    assert [m["role"] for m in looped["messages"]] == [
        "user", "assistant", "tool", "assistant", "tool", "assistant", "tool"]
    assert [m["content"] for m in looped["messages"][2::2]] == [
        FOUND, READ_TEXT, rest]
    assert [m["tool_calls"][0]["function"] for m in looped["messages"][3::2]] == [
        {"name": "web_fetch", "arguments": FETCH_ARGS},
        {"name": "web_fetch",
         "arguments": "{\"url\": \"https://example.org/notes\", \"offset\": 30}"}]
    t.next_turn()
    turn(t, *ANSWER_TURN)
    # Le bloc rendu au client reste valide avec `offset` dans son input.
    assert t.content[5]["input"] == {"url": PAGE, "offset": 30}
    assert fetched(t.content[6], rest)
    again = A.to_openai({**request, "messages": request["messages"] + [
        {"role": "assistant", "content": json.loads(json.dumps(t.content))},
        {"role": "user", "content": "Merci"}]}, hosted=hosted)
    n = len(looped["messages"])
    encode = lambda doc: json.dumps(doc, ensure_ascii=False)
    assert encode(again["messages"][:n]) == encode(looped["messages"])
    assert again["messages"][n:] == [{"role": "assistant", "content": "Voilà."},
                                     {"role": "user", "content": "Merci"}]
    assert encode(again["tools"]) == encode(looped["tools"])
    assert len(hosted.memory) == 0
    # Une erreur rejouée n'a plus que son code ; un document qui n'est pas
    # du texte (PDF en base64 venu d'Anthropic) est rendu comme un échec.
    t.content[4]["content"] = fetch_error("url_not_accessible")
    t.content[6]["content"]["content"]["source"] = {
        "type": "base64", "media_type": "application/pdf", "data": "JVBERi0="}
    assert [m["content"] for m in rebuilt(None)["messages"][4:7:2]] == [
        "Error: the web fetch failed (url_not_accessible).",
        "Error: the web fetch failed (unavailable)."]
    # Si la requête ne déclare plus la lecture, ses blocs sont ignorés —
    # ceux de la recherche restent rejoués.
    alone = A.to_openai({**request, "tools": [SEARCH_TOOL], "messages":
                         request["messages"] + [
                             {"role": "assistant", "content": t.content}]},
                        hosted=hosted)
    assert [m["role"] for m in alone["messages"]] == [
        "user", "assistant", "tool", "assistant"]


# ── la route /v1/messages ───────────────────────────────────────────────
# La boucle d'app.py est commune aux deux surfaces traduites : ses
# scénarios sont déroulés dans test_responses_api.py. Ici, par la route
# entière (fixture `proxy` de conftest.py), ce qui est propre à celle-ci :
# les blocs rendus, les réglages du client sur son outil, la forme des
# erreurs, les pings.

def post(proxy, **extra):
    return proxy.client.post(
        "/v1/messages", json=claude_code_search(model="essai/qwen", **extra))


def searching(proxy, streamed=True):
    """Le décor : un tour où le modèle cherche, puis celui où il conclut."""
    proxy.replies = [FakeUpstream(stream(*SEARCH_TURN)),
                     FakeUpstream(stream(*ANSWER_TURN))] if streamed \
        else [FakeUpstream(SEARCH_DOC), FakeUpstream(ANSWER_DOC)]
    return list(proxy.replies)


def test_app_runs_the_search_claude_code_asks_for(proxy):
    """La sous-requête WebSearch de Claude Code, par la route : les deux
    blocs qu'elle attend, et le backend relancé avec l'appel et le texte
    du résultat. Une recherche en échec est un bloc d'erreur, pas une
    erreur HTTP — le modèle, lui, lit pourquoi."""
    down = failed("unavailable",
                  "Error: search engine unreachable (ConnectError).")
    for result, content, counted in (
            (SEARCHED, BLOCKS, {"web_search_requests": 1}),
            (down, search_error("unavailable"), None)):
        proxy.hosted.result = result
        proxy.sent.clear()
        searching(proxy)
        r = post(proxy)
        ev = events(r.content)
        assert r.status_code == 200 and ev[-1][0] == "message_stop"
        blocks = blocks_of(ev)
        assert [b["type"] for b in blocks] == [
            "text", "server_tool_use", "web_search_tool_result", "text"]
        assert blocks[2] == {"type": "web_search_tool_result",
                             "tool_use_id": blocks[1]["id"], "content": content}
        assert ev[-2][1]["delta"]["stop_reason"] == "end_turn"
        assert ev[-2][1]["usage"].get("server_tool_use") == counted
        one, two = proxy.sent
        assert [t["function"]["name"] for t in one["tools"]] == ["web_search"]
        assert two["messages"][len(one["messages"]):] == [
            {"role": "assistant", "content": "Je cherche.", "tool_calls": [
                {"id": blocks[1]["id"], "type": "function", "function": {
                    "name": "web_search", "arguments": QUERY}}]},
            {"role": "tool", "tool_call_id": blocks[1]["id"],
             "content": result.text}]
    assert proxy.hosted.runs == [
        ("web_search", {"query": "llama.cpp latest release"}, {})] * 2
    assert [line[3:5] for line in proxy.lines] == [("/v1/messages", 200)] * 2
    assert len(proxy.hosted.memory) == 0


def test_app_json_mode_and_client_domains(proxy):
    searching(proxy, streamed=False)
    r = post(proxy, stream=False, tools=[{
        "type": "web_search_20250305", "name": "web_search",
        "allowed_domains": ["github.com"], "blocked_domains": ["x.test"]}])
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/json"
    msg = r.json()
    use = msg["content"][1]["id"]
    assert msg["type"] == "message" and msg["id"] == "chatcmpl-1"
    assert msg["content"] == [
        {"type": "text", "text": "Je cherche."},
        {"type": "server_tool_use", "id": use, "name": "web_search",
         "input": {"query": "llama.cpp latest release"}},
        {"type": "web_search_tool_result", "tool_use_id": use,
         "content": BLOCKS},
        {"type": "text", "text": "Voilà."}]
    assert msg["stop_reason"] == "end_turn"
    assert msg["usage"] == {
        "input_tokens": 110, "output_tokens": 15,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 140,
        "server_tool_use": {"web_search_requests": 1}}
    assert "stream" not in proxy.sent[0]
    # Les listes de domaines du client arrivent à l'exécution.
    assert proxy.hosted.runs == [("web_search", {
        "query": "llama.cpp latest release"}, {
        "allowed_domains": ["github.com"], "blocked_domains": ["x.test"]})]
    assert proxy.lines[0][4] == 200
    assert proxy.lines[0][6:10] == (250, 15, True, False)


def test_app_honors_max_uses_then_stops(proxy):
    """max_uses = 2 : deux recherches exécutées, les suivantes rendues en
    `max_uses_exceeded` sans être lancées ; le modèle qui insiste encore
    est arrêté 4 appels plus loin."""
    proxy.replies = [FakeUpstream(stream(
        tool_call(0, f"call_{i}", "web_search", "{\"query\":\"encore\"}"),
        chunk(finish="tool_calls"), usage(10, 1))) for i in range(9)]
    r = post(proxy, tools=[{"type": "web_search_20250305", "name": "web_search",
                            "max_uses": 2}])
    ev = events(r.content)
    results = [b["content"] for b in blocks_of(ev)
               if b["type"] == "web_search_tool_result"]
    assert results == [BLOCKS, BLOCKS] + [search_error("max_uses_exceeded")] * 4
    assert len(proxy.hosted.runs) == 2 and len(proxy.sent) == 6
    assert "the limit of 2 web tool calls" in proxy.sent[3]["messages"][-1]["content"]
    assert ev[-2][1]["delta"]["stop_reason"] == "end_turn"
    assert ev[-2][1]["usage"]["server_tool_use"] == {"web_search_requests": 2}
    assert proxy.lines[0][6:8] == (60, 6)


def test_app_search_then_fetch_each_under_its_own_max_uses(proxy, monkeypatch):
    """Les deux outils serveur déclarés, en JSON : chaque fonction a SA
    limite (`max_uses`) et reçoit les réglages posés sur son outil ; les
    appels de trop sont rendus en `max_uses_exceeded` sans être exécutés."""
    seen, lines = [], []

    async def read(args, call):
        seen.append((args, dict(call.settings)))
        return PAGE_READ

    proxy.hosted.by_name["web_fetch"].run = read
    monkeypatch.setattr(app.stats, "record_tool", lambda *a: lines.append(a))
    call = lambda name, args: FakeUpstream(chat_doc({"content": None, "tool_calls": [
        {"id": "c", "function": {"name": name, "arguments": args}}]},
        "tool_calls", 10, 1))
    proxy.replies = [call("web_search", QUERY), call("web_fetch", FETCH_ARGS),
                     call("web_fetch", FETCH_ARGS), call("web_fetch", FETCH_ARGS),
                     call("web_search", QUERY), FakeUpstream(ANSWER_DOC)]
    r = proxy.client.post("/v1/messages", json=fetch_request(
        {**SEARCH_TOOL, "max_uses": 1},
        fetch_tool(max_uses=2, allowed_domains=["example.org"],
                   max_content_tokens=500), stream=False))
    msg = r.json()
    assert r.status_code == 200 and msg["stop_reason"] == "end_turn"
    results = [b for b in msg["content"] if b["type"] != "server_tool_use"]
    assert results[0]["content"] == BLOCKS
    assert fetched(results[1]) and fetched(results[2])
    assert [b["content"] for b in results[3:5]] == [
        fetch_error("max_uses_exceeded"), search_error("max_uses_exceeded")]
    assert [b["name"] for b in msg["content"]
            if b["type"] == "server_tool_use"] == [
        "web_search", "web_fetch", "web_fetch", "web_fetch", "web_search"]
    assert msg["usage"]["server_tool_use"] == {
        "web_search_requests": 1, "web_fetch_requests": 2}
    # Exécutés : une recherche, deux lectures — avec les réglages du client.
    assert len(proxy.hosted.runs) == 1 and seen == [({"url": PAGE}, {
        "allowed_domains": ["example.org"], "max_chars": 2000})] * 2
    # Le backend a reçu les deux fonctions, et relit ce qu'il avait lu.
    assert [t["function"]["name"] for t in proxy.sent[0]["tools"]] == [
        "web_search", "web_fetch"]
    assert proxy.sent[2]["messages"][-1]["content"] == READ_TEXT
    assert "the limit of 2 web tool calls" in proxy.sent[4]["messages"][-1]["content"]
    # Une ligne de statistiques par appel, avec la route et le modèle.
    assert [line[:4] for line in lines] == [
        (name, "/v1/messages", "essai/qwen", outcome) for name, outcome in (
            ("web_search", "ok"), ("web_fetch", "ok"), ("web_fetch", "ok"),
            ("web_fetch", "limit"), ("web_search", "limit"))]
    assert len(proxy.hosted.memory) == 0


def test_app_failure_takes_the_anthropic_error_form(proxy):
    offline = (503, "backend_offline", "backend «essai» hors ligne")
    refused = FakeUpstream(json.dumps(
        {"error": {"message": "contexte dépassé"}}).encode(), status=400)
    error = lambda kind, message: {"type": "error", "error": {
        "type": kind, "message": message}}
    # Au tour 2, en flux : `event: error` (le 200 est parti), au type que
    # donne le statut.
    for second, body in (
            (offline, error("api_error", "backend «essai» hors ligne")),
            (refused, error("invalid_request_error", "contexte dépassé"))):
        proxy.replies = [FakeUpstream(stream(*SEARCH_TURN)), second]
        r = post(proxy)
        assert r.status_code == 200 and events(r.content)[-1] == ("error", body)
    # En JSON rien n'est parti : la réponse prend le vrai statut.
    proxy.replies = [FakeUpstream(SEARCH_DOC), offline]
    r = post(proxy, stream=False)
    assert r.status_code == 503
    assert r.json() == error("api_error", "backend «essai» hors ligne")
    # Dès le PREMIER tour : le statut et le corps d'erreur habituels.
    proxy.replies = [FakeUpstream(b'{"error": {"message": "non"}}', status=500)]
    r = post(proxy)
    assert r.status_code == 500 and r.json() == error("api_error", "non")
    # Erreur DANS le flux d'un tour : la recherche qu'il demandait n'est
    # pas exécutée, le message est clos comme avant.
    proxy.replies = [FakeUpstream(stream(
        tool_call(0, "call_x", "web_search", "{}"),
        {"error": {"message": "GPU perdu"}}))]
    assert [e for e, _ in events(post(proxy).content)] == [
        "message_start", "error", "message_delta", "message_stop"]
    # Une ligne de stats par requête, au statut de son issue ; seules les
    # trois premières ont exécuté leur recherche.
    assert [line[4] for line in proxy.lines] == [503, 400, 503, 500, 200]
    assert len(proxy.hosted.runs) == 3


def test_app_pings_while_the_search_runs(proxy, monkeypatch):
    """En flux, des `ping` tiennent la connexion pendant l'exécution
    (Claude Code coupe un flux muet) ; aucun en JSON."""
    async def slow(args, call):
        await asyncio.sleep(0.08)
        return SEARCHED

    proxy.hosted.by_name["web_search"].run = slow
    monkeypatch.setattr(app.anthropic_api, "PING_INTERVAL", 0.02)
    searching(proxy)
    kinds = [e for e, _ in events(post(proxy).content)]
    assert "ping" in kinds and kinds[-1] == "message_stop"
    # Entre l'annonce de la recherche et son résultat, nulle part ailleurs.
    first, last = kinds.index("ping"), len(kinds) - kinds[::-1].index("ping")
    assert kinds[first - 1] == "content_block_stop"
    assert set(kinds[first:last]) == {"ping"}
    assert kinds[last] == "content_block_start"
    searching(proxy, streamed=False)
    assert post(proxy, stream=False).json()["stop_reason"] == "end_turn"


def test_app_behind_quotas_goes_through_the_pinged_stream(proxy, monkeypatch):
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
    ups = searching(proxy)
    r = post(proxy)
    assert r.headers["content-type"].startswith("text/event-stream")
    ev = events(r.content)
    assert [b["type"] for b in blocks_of(ev)] == [
        "text", "server_tool_use", "web_search_tool_result", "text"]
    assert ev[-1][0] == "message_stop" and len(proxy.sent) == 2
    assert len(proxy.lines) == 1 and proxy.lines[0][6:8] == (250, 15)
    assert all(u.closed for u in ups)


# ── blancs seuls avant un appel d'outil ─────────────────────────────────

def test_stream_whitespace_alone_before_a_tool_call_is_not_a_text_block():
    """Un bloc de texte vide, l'API Anthropic le refuse au rejeu ; suivis
    d'un texte, les blancs sont gardés."""
    ev = turn(A.Translator(200, "text/event-stream", "b/m"),
              chunk({"content": "\n\n"}), tool_call(0, "c1", "Read", "{}"),
              chunk(finish="tool_calls"))
    assert [b["type"] for b in blocks_of(ev)] == ["tool_use"]
    ev = turn(A.Translator(200, "text/event-stream", "b/m"),
              chunk({"content": "\n"}), chunk({"content": "Paris"}),
              chunk(finish="stop"))
    assert "".join(d["delta"]["text"] for e, d in ev
                   if e == "content_block_delta") == "\nParis"
