"""Le catalogue d'un backend (catalog.fetch_models) : gardé CATALOG_TTL,
sa réponse comme son silence, puis relu en arrière-plan ; une seule
lecture pour des appels simultanés."""

import asyncio

import httpx

from llm_proxy import backends, catalog


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
