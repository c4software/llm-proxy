"""Le prix d'une requête : les tarifs d'un backend ([backends.<nom>.prices])
appliqués à l'usage, enregistré aux stats et rendu au client en
`usage.cost` (forme d'OpenRouter)."""

import json

import httpx2
import pytest
from fakes import everywhere, fake_backend
from fastapi.testclient import TestClient

from llm_proxy import app
from llm_proxy.access import auth
from llm_proxy.storage import stats
from llm_proxy.upstream import catalog, pricing

PRICES = {"claude-opus-*": {"input": 4.0, "output": 20.0, "cache_read": 0.2, "cache_write": 5.0},
          "haiku": {"input": 0.1, "output": 0.5,
                    "long": {"above": 100000, "input": 0.5, "output": 2.5}},
          "local": {"input": 0, "output": 0}}


def test_formule():
    """Entrée lue normalement, lue du cache, écrite dans le cache, sortie :
    chacune à son tarif ; le palier long au-delà de son seuil."""
    rates = pricing.parse("b", PRICES)
    opus = pricing.rate_of(rates, "Claude-Opus-5-5")
    # 1 000 lus, 9 000 du cache, 2 000 écrits, 500 générés.
    assert pricing.cost(opus, 12000, 500, 9000, 2000) == pytest.approx(
        (1000 * 4 + 9000 * 0.2 + 2000 * 5 + 500 * 20) / 1e6)
    haiku = pricing.rate_of(rates, "haiku")
    assert pricing.cost(haiku, 100000, 1000) == pytest.approx((100000 * 0.1 + 1000 * 0.5) / 1e6)
    assert pricing.cost(haiku, 100001, 1000) == pytest.approx((100001 * 0.5 + 1000 * 2.5) / 1e6)
    # cache sans tarif propre : celui de l'entrée.
    assert pricing.cost(haiku, 1000, 0, 400) == pytest.approx(1000 * 0.1 / 1e6)
    assert pricing.cost(pricing.rate_of(rates, "local"), 5000, 5000) == 0
    assert pricing.rate_of(rates, "inconnu") is None
    assert pricing.cost(None, 10, 10) is None
    # Par aller-retour : le palier se juge requête par requête.
    two = [{"prompt_tokens": 60000, "completion_tokens": 0}] * 2
    assert pricing.usage_cost(haiku, two) == pytest.approx(2 * 60000 * 0.1 / 1e6)


@pytest.mark.parametrize("bad", [
    {"m": 3}, {"m": {"input": 1}}, {"m": {"input": 1, "output": -1}},
    {"m": {"input": 1, "output": 1, "prix": 2}}, {"m": {"input": 1, "output": 1, "long": {"input": 2}}},
])
def test_config_illisible_arrete_le_demarrage(bad):
    with pytest.raises(SystemExit):
        pricing.parse("b", bad)


class Bytes(httpx2.AsyncByteStream):
    """Un corps lu en flux, coupé n'importe où, comme le relais le lit."""
    def __init__(self, body: bytes, cut: int = 11):
        self.body, self.cut = body, cut

    async def __aiter__(self):
        for i in range(0, len(self.body), self.cut):
            yield self.body[i:i + self.cut]


def json_reply(doc):
    return httpx2.Response(200, stream=Bytes(json.dumps(doc).encode()),
                           headers={"content-type": "application/json"})


def chat_doc(usage):
    return {"id": "c1", "object": "chat.completion", "created": 1, "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}], "usage": usage}


@pytest.fixture
def priced(monkeypatch):
    """Un backend « payant » simulé (`replies`), ses lignes de stats (`lines`)."""
    env = type("Env", (), {})()
    env.replies, env.lines = [], []

    def handler(request):
        if request.url.path == "/v1/models":
            return httpx2.Response(200, json={"data": [{"id": "claude-opus-5-5"}]})
        return env.replies.pop(0)

    fake_backend(monkeypatch, "payant", {"url": "http://payant.test", "prices": PRICES}, handler)
    monkeypatch.setattr(auth, "PROXY_API_KEYS", [])
    everywhere(monkeypatch, stats, "record", lambda *a: env.lines.append(a))
    catalog.forget()
    env.client = TestClient(app.app)
    yield env
    catalog.forget()


USAGE = {"prompt_tokens": 12000, "completion_tokens": 500,
         "prompt_tokens_details": {"cached_tokens": 9000, "cache_write_tokens": 2000}}
EXPECTED = (1000 * 4 + 9000 * 0.2 + 2000 * 5 + 500 * 20) / 1e6


def test_json_porte_le_cout(priced):
    priced.replies.append(json_reply(chat_doc(USAGE)))
    r = priced.client.post("/v1/chat/completions", json={
        "model": "payant/claude-opus-5-5", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 200
    assert r.json()["usage"]["cost"] == pytest.approx(EXPECTED)
    assert r.json()["choices"][0]["message"]["content"] == "ok"
    assert priced.lines[-1][-1] == pytest.approx(EXPECTED)   # stats.db


def test_flux_porte_le_cout(priced):
    body = b"".join(b"data: " + json.dumps(d).encode() + b"\n\n" for d in [
        {"id": "c", "choices": [{"index": 0, "delta": {"content": "o"}, "finish_reason": None}]},
        {"id": "c", "choices": [{"index": 0, "delta": {"content": "k"}, "finish_reason": "stop"}]},
        {"id": "c", "choices": [], "usage": USAGE}]) + b"data: [DONE]\n\n"
    priced.replies.append(httpx2.Response(200, stream=Bytes(body),
                                          headers={"content-type": "text/event-stream"}))
    r = priced.client.post("/v1/chat/completions", json={
        "model": "payant/claude-opus-5-5", "stream": True,
        "messages": [{"role": "user", "content": "x"}]})
    lines = [ln[6:] for ln in r.text.split("\n") if ln.startswith("data: ")]
    assert lines[-1] == "[DONE]"
    docs = [json.loads(x) for x in lines[:-1]]
    assert "".join(c["delta"].get("content", "") for d in docs for c in d["choices"]) == "ok"
    assert docs[-1]["usage"]["cost"] == pytest.approx(EXPECTED)
    assert priced.lines[-1][-1] == pytest.approx(EXPECTED)


def test_sans_tarif_rien_ne_change(priced):
    """Un modèle sans tarif : la réponse passe telle quelle, pas de prix."""
    raw = chat_doc({"prompt_tokens": 10, "completion_tokens": 2})
    priced.replies.append(json_reply(raw))
    r = priced.client.post("/v1/chat/completions", json={
        "model": "payant/autre", "messages": [{"role": "user", "content": "x"}]})
    assert "cost" not in r.json()["usage"]
    assert priced.lines[-1][-1] is None


def test_anthropic_rend_les_tokens_ecrits():
    """Le backend Anthropic fait remonter les tokens écrits dans le cache,
    sous le nom d'OpenRouter : le prix en tient compte."""
    from llm_proxy.upstream import anthropic
    u = anthropic._usage({"input_tokens": 1000, "cache_read_input_tokens": 9000,
                          "cache_creation_input_tokens": 2000, "output_tokens": 500})
    assert u["prompt_tokens"] == 12000
    assert u["prompt_tokens_details"] == {"cached_tokens": 9000, "cache_write_tokens": 2000}
    rate = pricing.rate_of(pricing.parse("c", PRICES), "claude-opus-5-5")
    assert pricing.usage_cost(rate, [u]) == pytest.approx(EXPECTED)
