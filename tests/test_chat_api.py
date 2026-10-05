"""Les outils hébergés sur /v1/chat/completions (chat_api.py, et
app.chat_hosted) : la route entière, par la fixture `proxy` de
conftest.py — seul l'envoi au backend est remplacé. Aucun réseau."""

import json

import pytest

from fakes import (
    ANSWER_DOC, ANSWER_TURN, FOUND, QUERY, SEARCH_DOC, SEARCH_TURN,
    FakeUpstream, chat_doc, chunk, stream, tool_call, usage,
)
from llm_proxy import app as A
from llm_proxy import chat_api as C

WEB = {"type": "web_search"}
URL = "https://github.com/ggml-org/llama.cpp/releases"


def fn(name):
    return {"type": "function", "function": {
        "name": name, "parameters": {"type": "object", "properties": {}}}}


@pytest.fixture
def chat(proxy, monkeypatch):
    """`proxy`, [chat].hosted_tools actif, et les exécutions d'outils
    notées dans `tool_lines` (stats.record_tool)."""
    proxy.tool_lines = []
    monkeypatch.setattr(C, "ENABLED", True)
    monkeypatch.setattr(A.stats, "record_tool",
                        lambda *a: proxy.tool_lines.append(a))
    return proxy


def post(proxy, **extra):
    body = {"model": "essai/qwen", "stream": True, "tools": [WEB],
            "messages": [{"role": "user", "content": "Dernière version ?"}],
            **extra}
    return proxy.client.post("/v1/chat/completions", json=body)


def blocks(raw: bytes) -> list[dict]:
    """Les blocs d'un flux rendu au client. Il finit par UN `[DONE]`."""
    lines = [b for b in raw.decode().split("\n\n") if b.startswith("data: ")]
    assert lines[-1] == "data: [DONE]" and raw.count(b"[DONE]") == 1
    return [json.loads(line[6:]) for line in lines[:-1]]


def deltas(docs: list[dict]) -> list[dict]:
    return [c["delta"] for d in docs for c in d["choices"]]


def test_request_without_declaration_is_relayed_byte_for_byte(chat, monkeypatch):
    """Le chemin de toujours : mêmes octets vers le backend, mêmes octets
    vers le client, clé active ou non — et la clé inactive laisse passer
    jusqu'à la déclaration elle-même."""
    raws = []
    inner = A.send_upstream

    async def send_upstream(call, request, path, body):
        raws.append(body)
        return await inner(call, request, path, body)

    monkeypatch.setattr(A, "send_upstream", send_upstream)
    # Sans modèle (backend de repli) ni rien à injecter : aucun octet
    # n'est réécrit, pas même l'espacement.
    body = (b'{"messages":  [{"role": "user", "content": "h\\u00e9"}],\n'
            b' "stream": true, "user": "web_search"}')
    answer = stream(chunk({"role": "assistant", "content": "Bonjour"}),
                    chunk(finish="stop"), usage(7, 2))
    for enabled in (True, False):
        monkeypatch.setattr(C, "ENABLED", enabled)
        chat.replies = [FakeUpstream(answer)]
        r = chat.client.post("/v1/chat/completions", content=body)
        assert raws.pop() == body and r.content == answer
    # Clé inactive : la déclaration part telle quelle, c'est l'affaire du backend.
    chat.replies = [FakeUpstream(answer)]
    post(chat)
    assert chat.sent[-1]["tools"] == [WEB] and "stream_options" not in chat.sent[-1]
    assert not chat.hosted.runs and not chat.tool_lines


def test_declaration_is_replaced_by_the_hosted_functions(chat):
    names = lambda sent: [t["function"]["name"] for t in sent["tools"]]
    for extra, expected in (
        ({"tools": [fn("ls"), WEB]}, ["ls", "web_search", "web_fetch"]),
        ({"tools": [{"type": "web_search_preview"}]}, ["web_search", "web_fetch"]),
        # Le champ d'OpenAI pour ses modèles de recherche : un synonyme.
        ({"tools": [fn("ls")], "web_search_options": {}},
         ["ls", "web_search", "web_fetch"]),
        # Une fonction du client garde son nom.
        ({"tools": [fn("web_fetch"), WEB]}, ["web_fetch", "web_search"]),
    ):
        chat.replies = [FakeUpstream(stream(*ANSWER_TURN))]
        assert post(chat, **extra).status_code == 200
        sent = chat.sent[-1]
        assert names(sent) == expected and "web_search_options" not in sent
        assert all(t["type"] == "function" for t in sent["tools"])
        # Demandé au backend même si le client ne le demande pas : les stats.
        assert sent["stream_options"] == {"include_usage": True}
    # Sans `web_fetch` présenté, la recherche n'y renvoie pas.
    assert "web_fetch" not in sent["tools"][1]["function"]["description"]


def test_stream_of_two_upstream_turns_reads_as_one(chat):
    ups = [FakeUpstream(stream(*SEARCH_TURN)), FakeUpstream(stream(*ANSWER_TURN))]
    chat.replies = list(ups)
    r = post(chat, stream_options={"include_usage": True})
    assert r.status_code == 200
    docs = blocks(r.content)
    assert {d["id"] for d in docs} == {"chatcmpl-1"}
    assert [d["content"] for d in deltas(docs) if d.get("content")] == [
        "Je cherche.", "\n\nVoilà."]
    # L'appel hébergé n'arrive jamais au client ; une seule fin, un seul usage.
    assert not any("tool_calls" in d for d in deltas(docs))
    assert [c["finish_reason"] for d in docs for c in d["choices"]
            if c["finish_reason"]] == ["stop"]
    assert [d["usage"] for d in docs if d.get("usage")] == [{
        "prompt_tokens": 250, "completion_tokens": 15,
        "prompt_tokens_details": {"cached_tokens": 140},
        "completion_tokens_details": {"reasoning_tokens": 4}}]
    assert docs[-1]["choices"] == []
    # Le second envoi : le premier, plus l'appel et son résultat.
    one, two = chat.sent
    assert one["model"] == two["model"] == "qwen" and one["tools"] == two["tools"]
    call_id = two["messages"][-1]["tool_call_id"]
    assert two["messages"] == one["messages"] + [
        {"role": "assistant", "content": "Je cherche.", "tool_calls": [
            {"id": call_id, "type": "function", "function": {
                "name": "web_search", "arguments": QUERY}}]},
        {"role": "tool", "tool_call_id": call_id, "content": FOUND}]
    assert chat.hosted.runs == [
        ("web_search", {"query": "llama.cpp latest release"}, {})]
    # UNE ligne de requête, usage cumulé ; une ligne par exécution d'outil.
    assert len(chat.lines) == 1
    assert chat.lines[0][:5] == (
        "essai/qwen", "essai", "qwen", "/v1/chat/completions", 200)
    assert chat.lines[0][6:] == (250, 15, True, True, 140)
    assert [line[:4] for line in chat.tool_lines] == [
        ("web_search", "/v1/chat/completions", "essai/qwen", "ok")]
    assert all(u.closed for u in ups)

    # Le client n'a pas demandé `include_usage` : pas de bloc d'usage.
    chat.replies = [FakeUpstream(stream(*SEARCH_TURN)),
                    FakeUpstream(stream(*ANSWER_TURN))]
    assert not any("usage" in d for d in blocks(post(chat).content))


def test_json_mode(chat):
    chat.replies = [FakeUpstream(SEARCH_DOC), FakeUpstream(ANSWER_DOC)]
    r = post(chat, stream=False)
    doc = r.json()
    assert r.status_code == 200 and doc["id"] == "chatcmpl-1"
    assert doc["choices"] == [{"finish_reason": "stop", "message": {
        "content": "Je cherche.\n\nVoilà."}}]
    assert doc["usage"] == {"prompt_tokens": 250, "completion_tokens": 15,
                            "prompt_tokens_details": {"cached_tokens": 140}}
    assert "stream_options" not in chat.sent[0]
    assert chat.sent[1]["messages"][-1]["content"] == FOUND
    assert len(chat.lines) == 1
    assert chat.lines[0][6:] == (250, 15, True, False, 140)


def test_cited_sources_come_back_as_url_citations(chat, monkeypatch):
    """Une annotation par URL rendue par l'outil ET écrite par le modèle,
    en flux comme en JSON ; aucune avec [chat].annotations = false."""
    text = f"La b6789, voir {URL}."
    start = len("Je cherche.\n\nLa b6789, voir ")
    expected = [{"type": "url_citation", "url_citation": {
        "start_index": start, "end_index": start + len(URL), "url": URL,
        "title": "Releases · ggml-org/llama.cpp"}}]
    answer = (chunk({"content": text}), chunk(finish="stop"), usage(150, 5))

    chat.replies = [FakeUpstream(stream(*SEARCH_TURN)), FakeUpstream(stream(*answer))]
    docs = blocks(post(chat).content)
    assert [d["annotations"] for d in deltas(docs) if "annotations" in d] == [expected]
    assert docs[-1]["choices"][0]["finish_reason"] == "stop"

    replies = lambda: [FakeUpstream(SEARCH_DOC),
                       FakeUpstream(chat_doc({"content": text}, "stop", 150, 5))]
    chat.replies = replies()
    message = post(chat, stream=False).json()["choices"][0]["message"]
    assert message["annotations"] == expected
    assert message["content"][start:start + len(URL)] == URL

    monkeypatch.setattr(C, "ANNOTATIONS", False)
    chat.replies = replies()
    assert "annotations" not in post(
        chat, stream=False).json()["choices"][0]["message"]


def test_mixed_turn_the_first_call_decides(chat):
    search = ("call_x", "web_search", "{\"query\":\"gufo\"}")
    ls = ("call_a", "ls", "{\"path\":\".\"}")
    turn = lambda *calls: stream(
        *[tool_call(i, *c) for i, c in enumerate(calls)],
        chunk(finish="tool_calls"), usage(10, 2))
    both = [fn("ls"), WEB]

    # Hébergé d'abord : exécuté, l'appel du client de ce tour n'est pas
    # transmis, le backend est relancé — et le modèle le réémet.
    chat.replies = [FakeUpstream(turn(search, ls)), FakeUpstream(turn(ls))]
    docs = blocks(post(chat, tools=both).content)
    calls = [tc for d in deltas(docs) for tc in d.get("tool_calls", [])]
    assert [(tc["index"], tc["id"], tc["function"]["name"]) for tc in calls] == [
        (0, "call_a", "ls")]
    assert docs[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert [m["role"] for m in chat.sent[1]["messages"]] == [
        "user", "assistant", "tool"]
    assert [tc["function"]["name"] for tc in
            chat.sent[1]["messages"][1]["tool_calls"]] == ["web_search"]
    assert len(chat.hosted.runs) == 1 and len(chat.sent) == 2

    # Client d'abord : son appel est déjà parti ; l'appel hébergé n'est
    # pas exécuté (le modèle le redemandera), un seul tour.
    chat.replies = [FakeUpstream(turn(ls, search, ls))]
    docs = blocks(post(chat, tools=both).content)
    calls = [tc for d in deltas(docs) for tc in d.get("tool_calls", [])]
    assert [(tc["index"], tc["function"]["name"]) for tc in calls] == [
        (0, "ls"), (1, "ls")]
    assert docs[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert len(chat.hosted.runs) == 1 and len(chat.sent) == 3

    # En JSON, la même règle.
    whole = lambda *calls: chat_doc({"content": None, "tool_calls": [
        {"id": i, "type": "function", "function": {"name": n, "arguments": a}}
        for i, n, a in calls]}, "tool_calls", 10, 2)
    chat.replies = [FakeUpstream(whole(search, ls)), FakeUpstream(whole(ls))]
    choice = post(chat, tools=both, stream=False).json()["choices"][0]
    assert [tc["id"] for tc in choice["message"]["tool_calls"]] == ["call_a"]
    assert choice["finish_reason"] == "tool_calls"
    assert len(chat.hosted.runs) == 2


def test_a_model_that_never_concludes_is_stopped(chat, monkeypatch):
    monkeypatch.setattr(A, "HOSTED_HARD_LIMIT", 3)
    chat.replies = [FakeUpstream(stream(
        tool_call(0, f"call_{i}", "web_search", "{\"query\":\"encore\"}"),
        chunk(finish="tool_calls"), usage(10, 1))) for i in range(5)]
    docs = blocks(post(chat, tool_choice="required").content)
    # Close sans lui : pas de `tool_calls` que le client ne saurait honorer.
    assert [c["finish_reason"] for d in docs for c in d["choices"]] == ["stop"]
    assert len(chat.sent) == 3 and len(chat.hosted.runs) == 3
    assert len(chat.lines) == 1 and chat.lines[0][6:8] == (30, 3)
    # `tool_choice` forcé : au premier tour seulement.
    assert [s["tool_choice"] for s in chat.sent] == ["required", "auto", "auto"]


def test_failure_on_a_later_turn(chat):
    """En flux le 200 est parti : un bloc `error`, puis `[DONE]`. En JSON
    rien n'est parti : le vrai statut. La ligne de stats garde ce que le
    tour 1 a consommé."""
    offline = (503, "backend_offline", "backend «essai» hors ligne")
    refused = lambda: FakeUpstream(json.dumps(
        {"error": {"message": "contexte dépassé"}}).encode(), status=400)
    for first, second, status, message in (
        (stream(*SEARCH_TURN), offline, 503, "backend «essai» hors ligne"),
        (stream(*SEARCH_TURN), refused(), 400, "contexte dépassé"),
        (SEARCH_DOC, offline, 503, "backend «essai» hors ligne"),
    ):
        chat.lines.clear()
        chat.replies = [FakeUpstream(first), second]
        streamed = first is not SEARCH_DOC
        r = post(chat, stream=streamed)
        last = blocks(r.content)[-1] if streamed else r.json()
        assert r.status_code == (200 if streamed else status)
        assert last["error"]["message"] == message
        assert len(chat.lines) == 1 and chat.lines[0][4] == status
        assert chat.lines[0][6:9] == (100, 10, True)

    # Erreur dès le PREMIER tour : le corps du backend, tel quel.
    body = b'{"error": {"message": "non"}}'
    chat.lines.clear()
    chat.replies = [FakeUpstream(body, status=500)]
    r = post(chat)
    assert r.status_code == 500 and r.content == body
    assert len(chat.lines) == 1 and chat.lines[0][4] == 500


def test_declared_but_disabled_tool_is_refused(chat):
    """Ni retiré en silence (le client croirait à une recherche), ni
    laissé au backend (son erreur ne dirait pas pourquoi) : un 400 qui
    nomme le réglage. Rien ne part."""
    # Les outils factices n'ont pas `image_generation`.
    r = post(chat, tools=[{"type": "image_generation"}])
    assert r.status_code == 400
    assert "«image_generation» déclaré mais désactivé" in r.json()["error"]["message"]
    chat.hosted = type(chat.hosted)(modules=[])  # aucun outil actif
    for extra in ({}, {"tools": [], "web_search_options": {}}):
        assert post(chat, **extra).status_code == 400
    assert not chat.sent and not chat.lines
