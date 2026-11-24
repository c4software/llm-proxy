"""Un backend OpenAI dont le `content` arrive en liste de morceaux
(Mistral : texte et réflexion) : ramené à une chaîne, la réflexion en
`reasoning_content`, en flux comme en JSON, en relais brut comme dans la
boucle des outils hébergés ; un contenu ordinaire passe octet pour octet.
Le backend est simulé (httpx2.MockTransport), tout le reste du proxy
tourne."""

import json

import httpx2
import pytest

from fakes import chat_doc, chunk, fake_backend, hosted_chat, stream, tool_call, usage
from llm_proxy.upstream import openai, relaying

REAL_SEND = relaying.send_upstream


def thinking(text):
    return {"type": "thinking", "thinking": [{"type": "text", "text": text}]}


def text(value):
    return {"type": "text", "text": value}


class Pieces(httpx2.AsyncByteStream):
    """Un corps livré par morceaux de `size` octets, comme sur le réseau :
    des lignes (et des caractères UTF-8) coupées entre deux morceaux."""

    def __init__(self, data: bytes, size: int):
        self.data, self.size = data, size

    async def __aiter__(self):
        for i in range(0, len(self.data), self.size):
            yield self.data[i:i + self.size]


@pytest.fixture
def mistral(proxy, monkeypatch):
    """Le proxy et un backend « mistral » au transport simulé : `replies`
    ((statut, corps, type) dans l'ordre), `sent` (les corps reçus)."""
    hosted_chat(monkeypatch)
    monkeypatch.setattr(relaying, "send_upstream", REAL_SEND)
    proxy.replies, proxy.sent = [], []

    def handler(request):
        if request.method == "GET":
            return httpx2.Response(200, json={"data": [{"id": "m"}]})
        proxy.sent.append(json.loads(request.content))
        status, body, kind = proxy.replies.pop(0)
        return httpx2.Response(status, stream=Pieces(body, 7),
                               headers={"content-type": kind})

    fake_backend(monkeypatch, "mistral", {"url": "https://api.mistral.test"}, handler)
    return proxy


def ask(env, stream_=True, **extra):
    return env.client.post("/v1/chat/completions", json={
        "model": "mistral/m", "stream": stream_,
        "messages": [{"role": "user", "content": "q"}], **extra})


def deltas(raw: bytes) -> list[dict]:
    docs = [json.loads(line[6:]) for line in raw.decode().split("\n")
            if line.startswith("data: {")]
    return [c["delta"] for d in docs for c in d.get("choices") or ()]


SSE = "text/event-stream"


def test_la_reflexion_en_morceaux_devient_reasoning_content(mistral):
    """Relais brut, en flux (lignes coupées au milieu du JSON et d'un
    caractère accentué) puis en JSON : la réflexion arrive en
    `reasoning_content`, le texte en chaîne ; [DONE] et `usage` restent."""
    mistral.replies.append((200, stream(
        chunk({"role": "assistant", "content": [thinking("Je réfléchis…")]}),
        chunk({"content": [thinking(" encore"), text("Réponse é")]}),
        chunk({"content": [text("tée.")]}, "stop"),
        usage(10, 4)), SSE))
    r = ask(mistral)
    assert r.status_code == 200, r.text
    got = deltas(r.content)
    assert "".join(d.get("reasoning_content", "") for d in got) == "Je réfléchis… encore"
    assert "".join(d.get("content") or "" for d in got) == "Réponse étée."
    assert all(not isinstance(d.get("content"), list) for d in got)
    assert r.content.endswith(b"data: [DONE]\n\n") and b'"usage"' in r.content

    # Un emoji coupé entre deux deltas (substitut isolé), une dernière
    # ligne sans fin de ligne : le flux va jusqu'au bout.
    mistral.replies.append((200, b'data: {"choices": [{"delta": {"content": '
                            b'[{"type": "text", "text": "a\\ud83d"}]}}]}\n\n'
                            b'data: {"choices": [{"delta": {"content": '
                            b'[{"type": "text", "text": "b"}]}}]}', SSE))
    got = deltas(ask(mistral).content)
    assert "".join(d["content"] for d in got) == "a\ud83db"

    mistral.replies.append((200, chat_doc(
        {"role": "assistant", "content": [thinking("Hmm."), text("Oui.")]}, "stop", 3, 1),
        "application/json"))
    msg = ask(mistral, stream_=False).json()["choices"][0]["message"]
    assert (msg["reasoning_content"], msg["content"]) == ("Hmm.", "Oui.")


def test_un_contenu_ordinaire_passe_octet_pour_octet(mistral):
    """Des chaînes (llama.cpp), des fins de ligne `\\r\\n`, un commentaire :
    le flux rendu est celui de l'amont. Une erreur, ou une autre route que
    chat/completions, dont le corps a un `content` en liste aussi."""
    raw = (b": ping\r\n\r\n"
           + stream(chunk({"content": "Bonjour é"}), chunk({}, "stop")).replace(b"\n", b"\r\n"))
    mistral.replies.append((200, raw, SSE))
    assert ask(mistral).content == raw
    listed = b'{"choices": [{"message": {"content": [{"type": "text", "text": "x"}]}}]}'
    mistral.replies.append((400, listed, "application/json"))
    r = ask(mistral, stream_=False)
    assert r.status_code == 400 and r.content == listed
    mistral.replies.append((200, listed, "application/json"))
    r = mistral.client.post("/v1/completions", json={"model": "mistral/m", "prompt": "q"})
    assert r.status_code == 200 and r.content == listed


def test_les_formes_de_morceaux():
    """Mélange, liste vide, null, référence, type inconnu, réflexion en
    chaîne, plusieurs choix : en flux (delta) comme en JSON (message)."""
    cases = {
        "mélange": ([thinking("a"), text("b"), text("c")],
                    {"reasoning_content": "a", "content": "bc"}),
        "liste vide": ([], {}),
        "réflexion seule": ([thinking("a")], {"reasoning_content": "a"}),
        "réflexion en chaîne": ([{"type": "thinking", "thinking": "a"}],
                                {"reasoning_content": "a"}),
        "référence et inconnu": ([{"type": "reference", "reference_ids": [1]},
                                  {"type": "image_url"}, text("b")], {"content": "b"}),
        "réflexion mal formée": ([{"type": "thinking", "thinking": 5}, text("b")],
                                 {"content": "b"}),
    }
    for case, (content, expected) in cases.items():
        doc = {"choices": [{"delta": {"content": content}}, {"delta": {"content": "x"}}]}
        assert openai.normalize(doc, "delta"), case
        assert doc["choices"] == [{"delta": expected}, {"delta": {"content": "x"}}], case
        doc = {"choices": [{"message": {"content": content}}]}
        openai.normalize(doc, "message")
        assert doc["choices"][0]["message"] == {"content": None, **expected}, case
    for case, doc in (("null", {"choices": [{"delta": {"content": None}}]}),
                      ("chaîne", {"choices": [{"delta": {"content": "x"}}]}),
                      ("sans choix", {"error": {"content": []}})):
        assert not openai.normalize(doc, "delta"), case


def test_la_boucle_des_outils_lit_le_texte_en_morceaux(mistral):
    """Outils hébergés : le texte en morceaux du premier tour revient au
    modèle au second, sans réflexion ni champs de gabarit (backend sans
    `chat_template`) ; la réponse finale arrive au client."""
    for streamed in (True, False):
        if streamed:
            mistral.replies += [
                (200, stream(
                    chunk({"content": [thinking("Chercher."), text("Je cherche.")]}),
                    tool_call(0, "call_x", "web_search", '{"query": "llama"}'),
                    chunk(finish="tool_calls"), usage(10, 2)), SSE),
                (200, stream(chunk({"content": [text("Voilà.")]}), chunk(finish="stop"),
                             usage(20, 2)), SSE)]
        else:
            mistral.replies += [
                (200, chat_doc({"content": [thinking("Chercher."), text("Je cherche.")],
                                "tool_calls": [{"id": "call_x", "type": "function",
                                                "function": {"name": "web_search",
                                                             "arguments": '{"query": "llama"}'}}]},
                               "tool_calls", 10, 2), "application/json"),
                (200, chat_doc({"content": [text("Voilà.")]}, "stop", 20, 2),
                 "application/json")]
        r = ask(mistral, stream_=streamed, tools=[{"type": "web_search"}],
                chat_template_kwargs={"enable_thinking": True})
        assert r.status_code == 200, r.text
        answer = ("".join(d.get("content") or "" for d in deltas(r.content)) if streamed
                  else r.json()["choices"][0]["message"]["content"])
        assert answer.endswith("Voilà."), (streamed, answer)
        second = mistral.sent[-1]
        assistant = [m for m in second["messages"] if m["role"] == "assistant"]
        assert assistant[-1]["content"] == "Je cherche.", streamed
        assert "chat_template_kwargs" not in second, streamed
        assert not any(k.startswith("reasoning") for m in second["messages"] for k in m), streamed
