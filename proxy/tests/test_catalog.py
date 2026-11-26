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


def serving(data, name="cat", **cfg):
    """Un backend dont /v1/models rend les entrées `data` telles quelles."""
    b = backends.Backend(name, {"url": f"http://{name}.test", "api_key": "k", **cfg})

    async def handler(request):
        return httpx.Response(200, json={"object": "list", "data": data})
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


def test_niveaux_de_reflexion_annonces_ou_declares():
    """Les niveaux de réflexion d'un modèle : ceux que son catalogue
    annonce (le bloc `metadata` de llama-swap, meta.llamaswap.
    reasoning_efforts), filtrés aux niveaux connus ; un motif de
    `model_efforts` l'emporte ; ni l'un ni l'autre : non dits. Un alias suit
    son modèle."""
    data = [
        {"id": "qwen", "aliases": ["q"], "meta": {"llamaswap": {
            "type": "model", "reasoning_efforts": ["none", "LOW", "fort", "xhigh"]}}},
        {"id": "deepseek", "meta": {"llamaswap": {"reasoning_efforts": ["none", "max"]}}},
        {"id": "brut"},
    ]
    b = serving(data, model_efforts={"deep*": ["low", "high"]})
    asyncio.run(catalog.fetch_models(b))
    assert [b.efforts_of(m) for m in ("qwen", "Q", "deepseek", "brut")] == [
        ["none", "low", "xhigh"], ["none", "low", "xhigh"], ["low", "high"], None]


def test_architecture_a_la_forme_d_openrouter():
    """Chaque entrée porte `architecture` (forme d'OpenRouter et de
    llama.cpp) : celle du backend telle quelle, sinon déduite du type —
    le cas d'Albert, qui n'a que `type` ; un motif de model_types la
    fait déduire aussi."""
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
    b = serving(data, model_types={"forcé": "text-to-image"})
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
        b = serving(zen(api), "zen", url=f"http://zen-{api}.test", api=api)
        got = asyncio.run(catalog.fetch_models(b))
        assert {e["id"].removeprefix("zen/"): e["type"] for e in got} == expected, api
        assert b.models == set(expected), api


def test_un_catalogue_mistral_est_type_par_ses_capacites():
    """Mistral met `type: "base"` (ou `fine-tuned`) sur chaque carte : c'est
    son origine, pas sa tâche, que disent les `capabilities`. Ses modèles de
    conversation restent proposés à un client de chat ; ceux d'OCR, de
    voix, de modération, d'embeddings ou de temps réel ne le sont pas."""
    def card(mid, kind="base", **caps):
        return {"id": mid, "object": "model", "type": kind, "owned_by": "mistralai",
                "capabilities": {"completion_chat": False, "vision": False, **caps}}
    cards = [
        card("mistral-small-latest", completion_chat=True, vision=True, function_calling=True),
        card("codestral-latest", completion_chat=True, completion_fim=True),
        card("ft:mistral-small:abc", kind="fine-tuned", completion_chat=True),
        card("mistral-ocr-latest", ocr=True, vision=True),
        card("voxtral-mini-latest", audio_transcription=True),
        card("voxtral-mini-tts-latest", audio_speech=True),
        card("mistral-moderation-latest", classification=True, moderation=True),
        card("mistral-embed"),
        card("voxtral-mini-realtime-latest", audio_transcription_realtime=True),
        card("forcé"),
        # Un autre catalogue, des `capabilities` à lui : pas lues comme Mistral.
        {"id": "autre", "capabilities": {"vision": {"supported": True}}},
    ]
    b = serving(cards, "mistral", model_types={"forcé": "text-generation"})
    entries = asyncio.run(catalog.fetch_models(b))
    got = {e["id"].removeprefix("mistral/"): e["type"] for e in entries}
    assert got == {
        "mistral-small-latest": "image-text-to-text",
        "codestral-latest": "text-generation",
        "ft:mistral-small:abc": "text-generation",
        "mistral-ocr-latest": "image-to-text",
        "voxtral-mini-latest": "automatic-speech-recognition",
        "voxtral-mini-tts-latest": "text-to-speech",
        "mistral-moderation-latest": "text-classification",
        "mistral-embed": "text-embeddings-inference",
        "voxtral-mini-realtime-latest": "other",
        "forcé": "text-generation",
        "autre": "text-generation",
    }
    chat = {m for m, kind in got.items() if kind in catalog.CHAT_TYPES}
    assert chat == {"mistral-small-latest", "codestral-latest", "ft:mistral-small:abc",
                    "forcé", "autre"}
    ocr = next(e for e in entries if e["id"] == "mistral/mistral-ocr-latest")
    assert ocr["architecture"]["modality"] == "image->text"
