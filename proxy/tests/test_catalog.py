"""Le catalogue d'un backend (catalog.fetch_models) : gardé CATALOG_TTL,
sa réponse comme son silence, puis relu en arrière-plan ; une seule
lecture pour des appels simultanés."""

import asyncio

import httpx

from llm_proxy.upstream import backends, catalog


def backend(replies):
    """Un backend dont /v1/models rend `replies` dans l'ordre (une
    exception : injoignable) ; `.calls` : le nombre de lectures."""
    b = backends.Backend("cat", {"url": "http://cat.test", "api_key": "k"})
    b.calls = 0

    async def handler(request):
        b.calls += 1
        await asyncio.sleep(0.01)
        reply = replies[min(b.calls, len(replies)) - 1]
        if isinstance(reply, Exception):
            raise reply
        return httpx.Response(200, json={"data": [{"id": m} for m in reply]})

    b.client = httpx.AsyncClient(base_url=b.url, transport=httpx.MockTransport(handler))
    return b


def test_shared_and_kept():
    b = backend([["m1"], ["m2"]])

    async def go():
        first = await asyncio.gather(*(catalog.fetch_models(b) for _ in range(4)))
        again = await catalog.fetch_models(b)
        return first, again
    first, again = asyncio.run(go())
    assert b.calls == 1
    assert [[e["id"] for e in r] for r in first] == [["cat/m1"]] * 4
    assert [e["id"] for e in again] == ["cat/m1"]
    assert b.models == {"m1"}


def test_silence_kept_then_retried(monkeypatch):
    b = backend([httpx.ConnectError("down"), ["m1"]])
    assert asyncio.run(catalog.fetch_models(b)) is None
    assert asyncio.run(catalog.fetch_models(b)) is None
    assert b.calls == 1
    monkeypatch.setattr(catalog, "CATALOG_TTL", 0.0)

    async def stale_then_fresh():
        stale = await catalog.fetch_models(b)      # l'ancien, relu derrière
        await asyncio.sleep(0.05)
        return stale, catalog._cache[next(iter(catalog._cache))][1]
    stale, fresh = asyncio.run(stale_then_fresh())
    assert stale is None
    assert [e["id"] for e in fresh] == ["cat/m1"]
    assert b.calls == 2


def test_architecture_a_la_forme_d_openrouter():
    """Chaque entrée porte `architecture` (forme d'OpenRouter et de
    llama.cpp) : celle du backend telle quelle, sinon déduite du type —
    le cas d'Albert, qui n'a que `type` ; un motif de model_types la
    fait déduire aussi."""
    b = backends.Backend("cat", {"url": "http://cat.test", "api_key": "k",
                                 "model_types": {"forcé": "text-to-image"}})
    data = [
        {"id": "vl", "type": "image-text-to-text"},                       # Albert
        {"id": "emb", "type": "text-embeddings-inference"},
        {"id": "whisper", "type": "automatic-speech-recognition"},
        {"id": "brut"},                                                    # llama-swap
        {"id": "lcpp", "architecture": {"input_modalities": ["text", "image", "audio"],
                                        "output_modalities": ["text"]}},  # llama.cpp
        {"id": "llama-server", "architecture": {"input_modalities": ["text", "image"]}},
        {"id": "forcé", "architecture": {"input_modalities": ["text"],
                                         "output_modalities": ["text"]}},
    ]

    async def handler(request):
        return httpx.Response(200, json={"data": data})
    b.client = httpx.AsyncClient(base_url=b.url, transport=httpx.MockTransport(handler))
    got = {e["id"]: e["architecture"] for e in asyncio.run(catalog.fetch_models(b))}
    assert got["cat/vl"] == {"modality": "text+image->text",
                             "input_modalities": ["text", "image"], "output_modalities": ["text"]}
    assert got["cat/emb"]["output_modalities"] == ["embeddings"]
    assert got["cat/whisper"]["input_modalities"] == ["audio"]
    assert got["cat/brut"]["modality"] == "text->text"
    assert got["cat/lcpp"]["input_modalities"] == ["text", "image", "audio"]
    # llama-server : l'entrée seule, la sortie suit le type déduit.
    assert got["cat/llama-server"] == {"modality": "text+image->text",
                                       "input_modalities": ["text", "image"],
                                       "output_modalities": ["text"]}
    assert got["cat/forcé"] == {"modality": "text->image",
                                "input_modalities": ["text"], "output_modalities": ["image"]}


def test_un_catalogue_multi_protocoles_ne_garde_que_ce_que_le_backend_sert():
    """OpenCode Zen dit par modèle les API qui le servent (`api`) et des
    types à lui (`chat`, `embedding`, `image`) : un backend `openai` garde
    chat-completions, embeddings et images, un backend `anthropic` messages ;
    les types sont ramenés à ceux d'Albert."""
    def zen(api):
        return [
            {"id": "glm", "type": "chat", "api": {"chat-completions": {}},
             "modalities": {"input": ["text"], "output": ["text"]}},
            {"id": "kimi", "type": "chat", "api": {"chat-completions": {}},
             "modalities": {"input": ["text", "image/jpeg"], "output": ["text"]}},
            {"id": "gpt", "type": "chat", "api": {"responses": {}}},
            {"id": "gemini", "type": "chat", "api": {"generate-content": {}}},
            {"id": "claude", "type": "chat", "api": {"messages": {}},
             "modalities": {"input": ["text", "image/png"], "output": ["text"]}},
            {"id": "emb", "type": "embedding", "api": {"embeddings": {}}},
            {"id": "img", "type": "image", "api": {"images": {}}},
        ]
    for api, expected in (
            ("openai", {"glm": "text-generation", "kimi": "image-text-to-text",
                        "emb": "text-embeddings-inference", "img": "text-to-image"}),
            ("anthropic", {"claude": "image-text-to-text"})):
        b = backends.Backend("zen", {"url": f"http://zen-{api}.test", "api_key": "k", "api": api})

        async def handler(request):
            return httpx.Response(200, json={"data": zen(api)})
        b.client = httpx.AsyncClient(base_url=b.url, transport=httpx.MockTransport(handler))
        got = asyncio.run(catalog.fetch_models(b))
        assert {e["id"].removeprefix("zen/"): e["type"] for e in got} == expected, api
        assert b.models == set(expected), api
