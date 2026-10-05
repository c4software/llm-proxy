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
from llm_proxy.tools import web_search

WEB = {"type": "web_search"}
URL = "https://github.com/ggml-org/llama.cpp/releases"
# La conversation des tests de mémoire : la question, et la réponse telle
# que le client la reçoit après SEARCH_TURN puis ANSWER_TURN.
Q1 = {"role": "user", "content": "Dernière version ?"}
A1 = {"role": "assistant", "content": "Je cherche.\n\nVoilà."}
Q2 = {"role": "user", "content": "Et la précédente ?"}


def fn(name):
    return {"type": "function", "function": {
        "name": name, "parameters": {"type": "object", "properties": {}}}}


@pytest.fixture
def chat(proxy, monkeypatch):
    """`proxy`, [chat].hosted_tools actif, les exécutions d'outils notées
    dans `tool_lines` (stats.record_tool), une mémoire des échanges
    cachés neuve, et dans `raws` les OCTETS partis au backend."""
    proxy.tool_lines, proxy.raws = [], []
    inner = A.send_upstream

    async def send_upstream(call, request, path, body):
        proxy.raws.append(body)
        return await inner(call, request, path, body)

    monkeypatch.setattr(A, "send_upstream", send_upstream)
    monkeypatch.setattr(C, "ENABLED", True)
    monkeypatch.setattr(C, "MEMORY", C.Memory(8, 60, 100_000))
    monkeypatch.setattr(A.stats, "record_tool",
                        lambda *a: proxy.tool_lines.append(a))
    return proxy


def post(proxy, **extra):
    body = {"model": "essai/qwen", "stream": True, "tools": [WEB],
            "messages": [{"role": "user", "content": "Dernière version ?"}],
            **extra}
    return proxy.client.post("/v1/chat/completions", json=body)


def ask(proxy, messages, key=None, **extra):
    """Une requête de la conversation `messages`, outil déclaré. Par
    défaut le backend répond ANSWER_TURN (aucune recherche)."""
    if not proxy.replies:
        proxy.replies = [FakeUpstream(stream(*ANSWER_TURN))]
    body = {"model": "essai/qwen", "stream": True, "tools": [WEB],
            "messages": messages, **extra}
    r = proxy.client.post(
        "/v1/chat/completions", json=body,
        headers={"Authorization": f"Bearer {key}"} if key else None)
    assert r.status_code == 200
    return proxy.sent[-1]["messages"]


def searched(proxy, messages=(Q1,), key=None):
    """Le tour d'une réponse AVEC recherche : après lui, l'échange caché
    de A1 est en mémoire."""
    proxy.replies = [FakeUpstream(stream(*SEARCH_TURN)),
                     FakeUpstream(stream(*ANSWER_TURN))]
    return ask(proxy, list(messages), key)


def roles(messages) -> str:
    return " ".join(m["role"] for m in messages)


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


def test_next_request_gets_the_hidden_exchange_back_byte_for_byte(chat):
    """L'objectif de la mémoire : ce que le backend reçoit à la requête
    suivante COMMENCE, à l'octet près, par ce qu'il a reçu au dernier
    tour de la boucle, suivi de ce qu'il a répondu à ce tour-là."""
    loop = searched(chat)
    last = chat.raws[-1]
    seen = last[:last.index(b'], "stream_options"')]   # jusqu'au résultat
    assert seen.endswith(json.dumps(loop[-1], ensure_ascii=False).encode())
    ask(chat, [Q1, A1, Q2])
    answer = json.dumps({"role": "assistant", "content": "Voilà."},
                        ensure_ascii=False).encode()
    assert chat.raws[-1].startswith(seen + b", " + answer + b", ")
    assert roles(chat.sent[-1]["messages"]) == "user assistant tool assistant user"

    # Ce qu'un client change sans changer la réponse : l'échange revient.
    for rewritten in (
        {"role": "assistant", "content": "Je cherche.\n\nVoilà.\n "},
        {"role": "assistant", "content": [
            {"type": "text", "text": "Je cherche.\n\n"},
            {"type": "text", "text": "Voilà."}]},
        {**A1, "annotations": [], "reasoning_content": "hm"},
    ):
        sent = ask(chat, [{"role": "system", "content": "Il est 12 h 04."},
                          Q1, rewritten, Q2])
        assert sent[1:4] == loop and sent[5] == Q2
        # Son message, au texte du dernier tour ; ses champs sont gardés.
        assert sent[4] == {**rewritten, "content": "Voilà."}

    # En JSON aussi l'échange est rangé.
    chat.replies = [FakeUpstream(SEARCH_DOC), FakeUpstream(ANSWER_DOC)]
    ask(chat, [Q2], stream=False)
    assert roles(ask(chat, [Q2, A1, Q1])) == "user assistant tool assistant user"


def test_in_doubt_nothing_is_reinserted(chat, monkeypatch):
    """Réponse qui n'est plus celle rangée, historique amputé, entrée
    expirée, mémoire coupée, autre client : l'historique du client part
    tel quel, comme sans mémoire."""
    part = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}
    for case, messages, setup in (
        ("texte changé", [Q1, {**A1, "content": "Voilà."}, Q2], None),
        ("pas que du texte", [Q1, {**A1, "content": [
            {"type": "text", "text": A1["content"]}, part]}, Q2], None),
        ("historique amputé", [A1, Q2], None),
        ("question changée", [{**Q1, "content": "Autre ?"}, A1, Q2], None),
        ("expirée", [Q1, A1, Q2], lambda: setattr(C.MEMORY, "ttl", -1)),
        ("[chat].memory = false", [Q1, A1, Q2],
         lambda: monkeypatch.setattr(C, "MEMORY_ENABLED", False)),
    ):
        monkeypatch.setattr(C, "MEMORY", C.Memory(8, 60, 100_000))
        searched(chat)
        if setup:
            setup()
        assert ask(chat, messages) == messages, case
        monkeypatch.setattr(C, "MEMORY_ENABLED", True)

    # Cloisonnée par client dès que le proxy a des clés.
    monkeypatch.setattr(A, "PROXY_API_KEYS", ["un", "deux"])
    searched(chat, key="un")
    assert ask(chat, [Q1, A1, Q2], key="deux") == [Q1, A1, Q2]
    assert len(ask(chat, [Q1, A1, Q2], key="un")) == 5


def test_two_hidden_exchanges_in_one_conversation(chat):
    """Chaque réponse a le sien, rangé sous ce que le CLIENT envoie — pas
    sous ce qui a été réinséré : le troisième tour retrouve les deux."""
    first = searched(chat)[1:]
    chat.replies = [
        FakeUpstream(stream(tool_call(0, "call_y", "web_fetch",
                                      json.dumps({"url": URL})),
                            chunk(finish="tool_calls"), usage(10, 1))),
        FakeUpstream(stream(chunk({"content": "Lu."}), chunk(finish="stop"),
                            usage(10, 1)))]
    ask(chat, [Q1, A1, Q2])
    last = chat.raws[-1]
    seen = last[:last.index(b'], "stream_options"')]
    second = chat.sent[-1]["messages"][-2:]
    assert [m["role"] for m in second] == ["assistant", "tool"]

    a2 = {"role": "assistant", "content": "Lu."}
    q3 = {"role": "user", "content": "Merci."}
    # (A1 revient cette fois avec une espace de fin : même message.)
    sent = ask(chat, [Q1, {**A1, "content": A1["content"] + " "}, Q2, a2, q3])
    assert sent == [Q1, *first, {**A1, "content": "Voilà."}, Q2, *second, a2, q3]
    assert chat.raws[-1].startswith(
        seen + b", " + json.dumps(a2).encode() + b", ")
    # Le premier a expiré ou est sorti de la borne : le second revient seul.
    del C.MEMORY._data[next(iter(C.MEMORY._data))]
    assert ask(chat, [Q1, A1, Q2, a2, q3]) == [Q1, A1, Q2, *second, a2, q3]


def test_without_declaration_the_history_is_not_read(chat, monkeypatch):
    """Un client qui cesse de déclarer l'outil repasse au relais brut :
    mêmes octets, la mémoire n'est pas consultée — et pas vidée : elle
    sert de nouveau s'il redéclare."""
    searched(chat)
    body = json.dumps({"model": "essai/qwen", "messages": [Q1, A1, Q2],
                       "stream": True}, ensure_ascii=False).encode()
    inner = C.restore
    monkeypatch.setattr(C, "restore", None)     # appelée = 500
    chat.replies = [FakeUpstream(stream(*ANSWER_TURN))]
    r = chat.client.post("/v1/chat/completions", content=body)
    # Seul le préfixe du backend est retiré du modèle.
    assert r.status_code == 200
    assert chat.raws[-1] == body.replace(b"essai/qwen", b"qwen")
    monkeypatch.setattr(C, "restore", inner)
    assert len(ask(chat, [Q1, A1, Q2])) == 5


def test_memory_is_bounded_and_never_holds_an_image(chat, monkeypatch):
    memory = C.Memory(2, 60, 100)
    exchange = lambda n: [{"role": "tool", "tool_call_id": "c", "content": "x" * n}]
    for key, n in (("a", 40), ("b", 40), ("c", 40), ("gros", 101)):
        memory.store(key, exchange(n), "fin")
    # 43 caractères par entrée : la troisième fait sortir la première
    # (borne en caractères), la quatrième dépasse à elle seule.
    assert [memory.recall(k) is not None for k in ("a", "b", "c", "gros")] == [
        False, True, True, False]
    assert (len(memory), memory.size) == (2, 86)
    memory.store("d", exchange(1), "")
    assert len(memory) == 2 and memory.recall("b") is None      # borne en entrées

    # Un résultat qui porte une image : le client la reçoit, la mémoire
    # ne garde que le texte rendu au modèle.
    class Picture(str):
        b64, format = "QUJDREVGRw==", "png"

    chat.hosted.result = Picture("Image generated.")
    searched(chat)
    kept = json.dumps([entry for _, _, entry in C.MEMORY._data.values()])
    assert "Image generated." in kept and Picture.b64 not in kept


def test_one_annotation_per_written_occurrence_of_a_source(chat):
    """`text.find` de chaque source annotait aussi une URL PRÉFIXE de
    celle que le modèle a écrite : deux annotations au même endroit."""
    repo = URL.removesuffix("/releases")
    archive = "https://web.archive.org/web/2026/" + URL
    found = lambda *urls: web_search.render("q", [
        {"title": f"T{i}", "date": "", "url": u, "snippet": ""}
        for i, u in enumerate(urls)])
    search = {"id": "c1", "function": {"name": "web_search", "arguments": QUERY}}
    fetch = {"id": "c2", "function": {"name": "web_fetch",
                                      "arguments": json.dumps({"url": URL})}}
    for case, sources, calls, text, expected in (
        ("préfixe d'une autre", (repo, URL), [search],
         f"Les releases sont à l'URL {URL}.", [URL]),
        ("deux sources, une URL", (URL,), [search, fetch],
         f"Voir {URL}", [URL]),
        ("accents, deux occurrences, les deux URL", (repo, URL), [search],
         f"Dépôt « à jour » : {repo}, releases ({URL}) — où ? {URL}/",
         [repo, URL, URL]),
        ("une URL plus longue qu'une source n'est pas elle", (repo,), [search],
         f"Voir {URL} et {repo}.git", []),
        ("une source à l'intérieur d'une autre", (URL, archive), [search],
         f"Copie : {archive}", [archive]),
    ):
        chat.hosted.result = found(*sources)
        chat.replies = [
            FakeUpstream(chat_doc({"content": "Je vérifie d'abord.",
                                   "tool_calls": calls}, "tool_calls", 1, 1)),
            FakeUpstream(chat_doc({"content": text}, "stop", 1, 1))]
        message = post(chat, stream=False).json()["choices"][0]["message"]
        cited = [a["url_citation"] for a in message.get("annotations", [])]
        assert [c["url"] for c in cited] == expected, case
        # Indices en caractères du contenu rendu, texte du premier tour
        # et ligne vide compris.
        assert message["content"] == "Je vérifie d'abord.\n\n" + text
        assert all(message["content"][c["start_index"]:c["end_index"]]
                   == c["url"] for c in cited), case
        assert [c["start_index"] for c in cited] == sorted(
            {c["start_index"] for c in cited}), case


def test_responses_shaped_tool_choice_names_the_hosted_function(chat):
    forced = {"type": "function", "function": {"name": "web_search"}}
    for choice in (WEB, {"type": "web_search_preview"}):
        chat.replies = [FakeUpstream(stream(*SEARCH_TURN)),
                        FakeUpstream(stream(*ANSWER_TURN))]
        assert post(chat, tool_choice=choice).status_code == 200
        assert [s["tool_choice"] for s in chat.sent[-2:]] == [forced, "auto"]
    sent = len(chat.sent)
    for extra, reason in (
        ({"tools": [fn("ls")]}, "ne déclare pas"),
        ({"tools": [fn("web_search"), fn("web_fetch"), WEB]}, "ne déclare pas"),
        ({"tool_choice": {"type": "image_generation"}}, "désactivé"),
    ):
        r = post(chat, **{"tool_choice": WEB, **extra})
        assert r.status_code == 400 and reason in r.json()["error"]["message"]
    assert len(chat.sent) == sent
