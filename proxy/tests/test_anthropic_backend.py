"""Un backend Anthropic natif (`api = "anthropic"`) : la requête
chat/completions traduite en Messages, la réponse (JSON ou flux)
retraduite, le catalogue lu sur /v1/models d'Anthropic. Le backend est
simulé (httpx2.MockTransport), tout le reste du proxy tourne."""

import json

import pytest
from fakes import fake_backend
from fastapi.testclient import TestClient

from llm_proxy import app
from llm_proxy.access import auth
from llm_proxy.upstream import anthropic, catalog

CAPS = {"image_input": {"supported": True},
        "thinking": {"supported": True, "types": {"adaptive": {"supported": True}}},
        "effort": {"supported": True, "low": {"supported": True},
                   "high": {"supported": True}, "max": {"supported": True}}}
MODELS = {"data": [
    {"type": "model", "id": "claude-opus-5-5", "display_name": "Claude Opus 5.5",
     "created_at": "2026-09-01T00:00:00Z", "max_input_tokens": 1000000,
     "max_tokens": 128000, "capabilities": CAPS},
    {"type": "model", "id": "claude-haiku-4-5", "display_name": "Claude Haiku 4.5",
     "created_at": "2025-10-01T00:00:00Z", "max_input_tokens": 200000,
     "max_tokens": 64000, "capabilities": {"image_input": {"supported": True}}}],
    "has_more": False, "last_id": "claude-haiku-4-5"}


def sse(*events) -> bytes:
    return b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode()
                    for e in events)


@pytest.fixture
def claude(monkeypatch):
    """Le proxy avec un backend « claude » Anthropic : `replies` (ce que
    /v1/messages répondra, dans l'ordre), `sent` (les requêtes Messages
    reçues, avec leurs en-têtes)."""
    env = type("Env", (), {})()
    env.replies, env.sent = [], []

    def handler(request):
        if request.url.path == "/v1/models":
            assert request.headers["x-api-key"] == "sk-test"
            return anthropic_response(200, MODELS)
        assert request.url.path == "/v1/messages"
        env.sent.append((dict(request.headers), json.loads(request.content)))
        return env.replies.pop(0)

    fake_backend(monkeypatch, "claude", {"url": "https://api.anthropic.test",
                                         "api_key": "sk-test", "api": "anthropic"},
                 handler)
    monkeypatch.setattr(auth, "PROXY_API_KEYS", [])
    catalog.forget()
    anthropic._THINKING.clear()
    env.client = TestClient(app.app)
    yield env
    catalog.forget()


def anthropic_response(status, doc):
    import httpx2
    return httpx2.Response(status, json=doc)


def test_requete_traduite():
    """Système remonté, résultats d'outils en tête du tour utilisateur,
    image en base64, tool_choice et parallélisme, effort et réflexion
    selon les capabilities, échantillonnage retiré, cache demandé."""
    body = anthropic.to_messages({
        "model": "claude-opus-5-5", "temperature": 0.2, "top_p": 0.9,
        "reasoning_effort": "high", "stop": ["FIN", " "],
        "parallel_tool_calls": False, "tool_choice": "required",
        "tools": [{"type": "function", "function": {"name": "lire", "parameters": {
            "type": "object", "properties": {"p": {"type": "string"}}}}}],
        "messages": [
            {"role": "system", "content": "Sois bref."},
            {"role": "user", "content": [
                {"type": "text", "text": "Regarde"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call.1", "type": "function",
                "function": {"name": "lire", "arguments": '{"p": "a"}'}}]},
            {"role": "tool", "tool_call_id": "call.1", "content": "contenu"},
            {"role": "user", "content": "Et alors ?"},
            {"role": "developer", "content": "En français."}]},
        CAPS, 128000)
    assert body["system"] == "Sois bref.\n\nEn français."
    assert body["max_tokens"] == anthropic.DEFAULT_MAX_TOKENS
    assert body["cache_control"] == {"type": "ephemeral"}
    assert "temperature" not in body and "top_p" not in body
    assert body["stop_sequences"] == ["FIN"]
    assert body["tool_choice"] == {"type": "any", "disable_parallel_tool_use": True}
    assert body["tools"][0]["input_schema"]["properties"] == {"p": {"type": "string"}}
    assert body["output_config"] == {"effort": "high"}
    assert body["thinking"] == {"type": "adaptive", "display": "summarized",
                                "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
    user, assistant, after = body["messages"]
    assert user["content"][1] == {"type": "image", "source": {
        "type": "base64", "media_type": "image/png", "data": "AAAA"}}
    assert assistant["content"] == [{"type": "tool_use", "id": "call_1",
                                     "name": "lire", "input": {"p": "a"}}]
    assert after["role"] == "user"
    assert after["content"][0] == {"type": "tool_result", "tool_use_id": "call_1",
                                   "content": "contenu"}
    assert after["content"][1] == {"type": "text", "text": "Et alors ?"}


def test_requete_sans_capabilities():
    """Modèle sans réflexion adaptative ni effort connus : ni l'un ni
    l'autre ; max_tokens borné par la sortie du modèle."""
    body = anthropic.to_messages({"model": "claude-haiku-4-5", "reasoning_effort": "high",
                                  "messages": [{"role": "user", "content": "Salut"}]},
                                 {"image_input": {"supported": True}}, 8000)
    assert "thinking" not in body and "output_config" not in body
    assert body["max_tokens"] == 8000


def test_catalogue(claude):
    """Les modèles d'Anthropic préfixés, avec contexte et modalités."""
    data = {m["id"]: m for m in claude.client.get("/v1/models").json()["data"]}
    opus = data["claude/claude-opus-5-5"]
    assert opus["max_context_length"] == 1000000
    assert opus["type"] == "image-text-to-text"
    assert opus["architecture"]["modality"] == "text+image->text"
    assert opus["owned_by"] == "anthropic"


def test_reponse_et_reflexion_rejouee(claude):
    """Réponse JSON : outils, réflexion en reasoning_content, usage avec
    le cache. Au tour suivant, le bloc de réflexion signé repart avec
    l'appel d'outil, que le client ne renvoie pas."""
    thinking = {"type": "thinking", "thinking": "Je dois lire.", "signature": "sig"}
    claude.replies.append(anthropic_response(200, {
        "id": "msg_1", "type": "message", "role": "assistant",
        "model": "claude-opus-5-5", "stop_reason": "tool_use",
        "content": [thinking, {"type": "text", "text": "Je lis."},
                    {"type": "tool_use", "id": "toolu_1", "name": "lire",
                     "input": {"p": "a"}}],
        "usage": {"input_tokens": 10, "cache_read_input_tokens": 90,
                  "cache_creation_input_tokens": 0, "output_tokens": 20}}))
    tools = [{"type": "function", "function": {"name": "lire", "parameters": {
        "type": "object", "properties": {"p": {"type": "string"}}}}}]
    first = [{"role": "user", "content": "Lis a"}]
    r = claude.client.post("/v1/chat/completions", json={
        "model": "claude/claude-opus-5-5", "messages": first, "tools": tools})
    assert r.status_code == 200, r.text
    doc = r.json()
    headers, sent = claude.sent[0]
    assert headers["anthropic-version"] == anthropic.VERSION
    # Réflexion demandée : le bêta qui laisse écarter un bloc rejoué dans
    # une conversation modifiée, au lieu d'un 400.
    assert headers["anthropic-beta"] == anthropic.BINDING_BETA
    assert sent["thinking"]["block_binding"] == {"prefix_mismatch_behavior": "drop_block"}
    assert "authorization" not in headers
    assert sent["model"] == "claude-opus-5-5"
    msg = doc["choices"][0]["message"]
    assert doc["choices"][0]["finish_reason"] == "tool_calls"
    assert msg["content"] == "Je lis."
    assert msg["reasoning_content"] == "Je dois lire."
    assert msg["tool_calls"][0]["id"] == "toolu_1"
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"p": "a"}
    assert doc["usage"]["prompt_tokens"] == 100
    assert doc["usage"]["prompt_tokens_details"]["cached_tokens"] == 90

    claude.replies.append(anthropic_response(200, {
        "id": "msg_2", "type": "message", "role": "assistant",
        "model": "claude-opus-5-5", "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "C'est a."}],
        "usage": {"input_tokens": 5, "output_tokens": 3}}))
    r = claude.client.post("/v1/chat/completions", json={
        "model": "claude/claude-opus-5-5", "tools": tools, "messages": first + [
            {"role": "assistant", "content": "Je lis.",
             "tool_calls": msg["tool_calls"]},
            {"role": "tool", "tool_call_id": "toolu_1", "content": "a"}]})
    assert r.json()["choices"][0]["message"]["content"] == "C'est a."
    replayed = claude.sent[1][1]["messages"][1]["content"]
    assert replayed[0] == thinking
    assert [b["type"] for b in replayed] == ["thinking", "text", "tool_use"]


def test_flux(claude):
    """Le flux Messages → des chunks chat/completions : texte, réflexion,
    appel d'outil morcelé, fin, usage, [DONE]."""
    import httpx2
    events = sse(
        {"type": "message_start", "message": {"id": "msg_s", "model": "claude-opus-5-5",
                                              "usage": {"input_tokens": 7,
                                                        "cache_read_input_tokens": 3}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "thinking_delta", "thinking": "Hmm."}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "signature_delta", "signature": "s"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "text_delta", "text": "Bon"}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "text_delta", "text": "jour"}},
        {"type": "content_block_start", "index": 2,
         "content_block": {"type": "tool_use", "id": "toolu_9", "name": "lire", "input": {}}},
        {"type": "content_block_delta", "index": 2,
         "delta": {"type": "input_json_delta", "partial_json": '{"p": '}},
        {"type": "content_block_delta", "index": 2,
         "delta": {"type": "input_json_delta", "partial_json": '"b"}'}},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
         "usage": {"output_tokens": 12}},
        {"type": "message_stop"})
    class Flux(httpx2.AsyncByteStream):
        async def __aiter__(self):
            for i in range(0, len(events), 50):   # coupé n'importe où
                yield events[i:i + 50]
    claude.replies.append(httpx2.Response(
        200, stream=Flux(), headers={"content-type": "text/event-stream"}))
    r = claude.client.post("/v1/chat/completions", json={
        "model": "claude/claude-opus-5-5", "stream": True,
        "messages": [{"role": "user", "content": "Salut"}]})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    lines = [ln[6:] for ln in r.text.split("\n") if ln.startswith("data: ")]
    assert lines[-1] == "[DONE]"
    docs = [json.loads(x) for x in lines[:-1]]
    deltas = [d["choices"][0]["delta"] for d in docs if d["choices"]]
    assert "".join(d.get("content") or "" for d in deltas) == "Bonjour"
    assert "".join(d.get("reasoning_content") or "" for d in deltas) == "Hmm."
    calls = [c for d in deltas for c in d.get("tool_calls") or []]
    assert calls[0]["id"] == "toolu_9" and calls[0]["function"]["name"] == "lire"
    assert "".join(c["function"]["arguments"] for c in calls) == '{"p": "b"}'
    assert [d["choices"][0]["finish_reason"] for d in docs if d["choices"]][-1] == "tool_calls"
    assert docs[-1]["usage"]["prompt_tokens"] == 10
    assert docs[-1]["usage"]["completion_tokens"] == 12
    assert anthropic._THINKING["toolu_9"][0]["signature"] == "s"


def test_erreur_a_la_forme_openai(claude):
    claude.replies.append(anthropic_response(400, {
        "type": "error", "error": {"type": "invalid_request_error",
                                   "message": "tool_choice non pris en charge"}}))
    r = claude.client.post("/v1/chat/completions", json={
        "model": "claude/claude-opus-5-5", "stream": True,
        "messages": [{"role": "user", "content": "Salut"}]})
    assert r.status_code == 400
    assert r.json()["error"]["message"] == "tool_choice non pris en charge"
    assert r.json()["error"]["type"] == "invalid_request_error"


def test_autre_endpoint_refuse(claude):
    r = claude.client.post("/v1/embeddings", json={
        "model": "claude/claude-opus-5-5", "input": "x"})
    assert r.status_code == 404
    assert "chat/completions" in r.json()["error"]["message"]


def test_surface_messages_double_traduction(claude, monkeypatch):
    """Un client Anthropic (Claude Code) sur la surface /v1/messages du
    proxy, vers ce backend : Messages → chat/completions → Messages, et
    retour, sans rien perdre du texte ni de l'appel d'outil."""
    from fakes import everywhere
    from llm_proxy.surfaces import anthropic_api
    everywhere(monkeypatch, anthropic_api, "ENABLED", True)
    claude.replies.append(anthropic_response(200, {
        "id": "msg_3", "type": "message", "role": "assistant",
        "model": "claude-opus-5-5", "stop_reason": "tool_use",
        "content": [{"type": "text", "text": "Je cherche."},
                    {"type": "tool_use", "id": "toolu_5", "name": "Read",
                     "input": {"file_path": "/a"}}],
        "usage": {"input_tokens": 4, "output_tokens": 6}}))
    r = claude.client.post("/v1/messages", headers={"anthropic-version": "2023-06-01"}, json={
        "model": "claude/claude-opus-5-5", "max_tokens": 1000,
        "system": "Tu es un agent.",
        "tools": [{"name": "Read", "input_schema": {
            "type": "object", "properties": {"file_path": {"type": "string"}}}}],
        "messages": [{"role": "user", "content": "Lis /a"}]})
    assert r.status_code == 200, r.text
    doc = r.json()
    assert doc["stop_reason"] == "tool_use"
    assert [b["type"] for b in doc["content"]] == ["text", "tool_use"]
    assert doc["content"][1]["input"] == {"file_path": "/a"}
    sent = claude.sent[0][1]
    assert sent["system"].startswith("Tu es un agent.")
    assert sent["max_tokens"] == 1000
    assert sent["tools"][0]["name"] == "Read"
