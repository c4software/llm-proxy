"""Le limiteur d'Albert (albert.Limiter) : l'attente du verrou guettée
comme celle du quota, et les fenêtres recalées sur l'usage réel ; puis, à
travers le proxy, une coupure upstream en plein flux et le partage
équitable du flux à pings de /v1/messages."""

import asyncio
import json
import time

import httpx2
import pytest

from fakes import FakeUpstream, chunk, everywhere, quotas, sse, usage
from llm_proxy.core import identity
from llm_proxy.access import auth, fairshare
from llm_proxy.upstream import albert


def run(coro):
    return asyncio.run(coro)


def test_a_client_gone_while_queued_behind_the_lock_leaves_the_queue():
    async def scenario():
        lim = albert.Limiter("l", "générique", rpm=10, tpm=10_000)
        await lim.lock.acquire()          # une requête devant, qui attend
        gone = asyncio.Event()
        task = asyncio.ensure_future(lim.acquire(5, gone.wait))
        await asyncio.sleep(0.05)
        gone.set()
        with pytest.raises(albert.ClientGone):
            await asyncio.wait_for(task, 1)
        lim.lock.release()
        assert not lim.lock.locked() and not lim.minute.events
        # Le verrou n'a pas été emporté : la suivante passe.
        assert await asyncio.wait_for(lim.acquire(5), 1) == 0.0
    run(scenario())


def test_the_lock_wait_counts_in_the_waited_time():
    async def scenario():
        lim = albert.Limiter("l", "générique", rpm=10, tpm=10_000)
        await lim.lock.acquire()
        task = asyncio.ensure_future(
            lim.acquire(5, asyncio.Event().wait))
        await asyncio.sleep(0.1)
        lim.lock.release()
        assert await asyncio.wait_for(task, 1) >= 0.09
    run(scenario())


def test_the_windows_are_settled_on_the_real_usage():
    async def scenario():
        lim = albert.Limiter("l", "générique", rpm=10, tpm=10_000,
                             tpd=100_000)
        ticket = []
        await lim.acquire(1000, ticket=ticket)       # image en base64
        await lim.acquire(300, ticket=ticket)        # un second tour
        assert lim.minute.tokens == 1300
        albert.settle(ticket, 260)                   # tokens réels
        assert lim.minute.tokens == lim.day.tokens == 260
        assert [e[1] for e in lim.minute.events] == [200, 60]
        # Un événement sorti de la fenêtre ne se recale plus.
        lim.minute.evict(time.monotonic() + albert.MINUTE + 1)
        lim.minute.adjust(ticket[0][1], 999)
        assert lim.minute.tokens == 0
    run(scenario())


def test_an_unlimited_window_keeps_no_ticket():
    async def scenario():
        lim = albert.Limiter("l", "générique")
        ticket = []
        await lim.acquire(10, ticket=ticket)
        assert ticket == [] and not lim.minute.events
    run(scenario())


class Broken(FakeUpstream):
    """Un upstream qui tombe après le premier morceau."""

    async def aiter_raw(self):
        yield self.body[:40]
        raise httpx2.ReadError("boom")


def test_a_stream_cut_midway_is_told_and_counted_502(proxy, monkeypatch):
    from llm_proxy.surfaces import chat_api
    everywhere(monkeypatch, chat_api, "ENABLED", False)     # relais brut
    proxy.replies.append(Broken(sse(
        chunk({"role": "assistant", "content": "bonjour à tous"}),
        chunk({}, "stop", usage=usage(100, 50)))))
    r = proxy.client.post("/v1/chat/completions", json={
        "model": "essai/qwen", "stream": True,
        "messages": [{"role": "user", "content": "q"}]})
    assert r.status_code == 200
    assert r.content.endswith(b"data: [DONE]\n\n")
    assert b"upstream stream interrupted" in r.content
    assert [line[4] for line in proxy.lines] == [502]


def test_a_messages_stream_cut_midway_ends_on_an_error_event(proxy):
    proxy.replies.append(Broken(sse(
        chunk({"role": "assistant", "content": "bonjour à tous"}),
        chunk({}, "stop", usage=usage(100, 50)))))
    r = proxy.client.post("/v1/messages", json={
        "model": "essai/qwen", "max_tokens": 10, "stream": True,
        "messages": [{"role": "user", "content": "q"}]})
    assert r.status_code == 200
    assert b"event: error" in r.content
    assert [line[4] for line in proxy.lines] == [502]


KEY = "sk-alice"


@pytest.fixture
def shared(proxy, monkeypatch):
    from llm_proxy.surfaces import chat_api
    fairshare.reset()
    monkeypatch.setattr(auth, "PROXY_API_KEYS", [KEY])
    everywhere(monkeypatch, chat_api, "ENABLED", False)
    monkeypatch.setattr(fairshare, "DEFAULT_SHARE", 10)
    monkeypatch.setattr(fairshare, "WINDOW", 0.6)
    monkeypatch.setattr(fairshare, "CHECK_EVERY", 0.05)
    proxy.client.headers["authorization"] = f"Bearer {KEY}"
    yield proxy
    fairshare.reset()


def test_the_pinged_messages_stream_waits_for_the_share(shared, monkeypatch):
    """Claude Code sur un backend à quotas : le flux à pings passait outre
    le partage équitable."""
    from llm_proxy.upstream.backends import BACKENDS
    quotas(monkeypatch, BACKENDS["essai"], accepted=10)

    def ask():
        shared.replies.append(FakeUpstream(sse(
            chunk({"role": "assistant", "content": "ok"}),
            chunk({}, "stop", usage=usage(100, 50)))))
        started = time.monotonic()
        r = shared.client.post("/v1/messages", json={
            "model": "essai/qwen", "max_tokens": 10, "stream": True,
            "messages": [{"role": "user", "content": "q"}]})
        return r, time.monotonic() - started

    r, first = ask()
    assert r.status_code == 200 and first < 0.3
    assert fairshare.used(identity.owner(KEY)) > 10      # part dépassée
    r, second = ask()
    assert r.status_code == 200 and b"message_stop" in r.content
    assert second >= 0.3


def test_a_client_gone_during_a_json_tool_loop_stops_it(proxy):
    """En JSON rien ne part avant la fin de la boucle des outils : un
    client qui raccroche l'arrête (relaying.drain) au lieu de la laisser
    aller au bout pour personne."""
    from types import SimpleNamespace
    from llm_proxy.upstream import relaying
    from llm_proxy.upstream.backends import BACKENDS

    stopped = []

    async def loop():
        try:
            yield b"{"
            await asyncio.sleep(5)          # une recherche, un tour suivant
            yield b"}"
        finally:
            stopped.append(True)

    async def receive():
        await asyncio.sleep(0.05)
        return {"type": "http.disconnect"}

    counted = []
    call = relaying.Call(BACKENDS["essai"], "essai/qwen", "/v1/messages")
    call.done = lambda status, *a, **k: counted.append(status)
    request = SimpleNamespace(receive=receive)
    started = time.monotonic()
    assert asyncio.run(relaying.drain(loop(), call, request)) is None
    assert time.monotonic() - started < 1
    assert stopped == [True] and counted == [relaying.CLIENT_CLOSED]


def test_drain_returns_the_whole_body(proxy):
    from types import SimpleNamespace
    from llm_proxy.upstream import relaying
    from llm_proxy.upstream.backends import BACKENDS

    async def loop():
        yield b'{"a":'
        yield b"1}"

    async def receive():
        await asyncio.Event().wait()

    call = relaying.Call(BACKENDS["essai"], "essai/qwen", "/v1/messages")
    out = asyncio.run(relaying.drain(loop(), call,
                                     SimpleNamespace(receive=receive)))
    assert json.loads(out) == {"a": 1}


def test_a_second_process_on_the_same_data_is_refused(tmp_path, monkeypatch):
    """Tout l'état des quotas et du partage est en mémoire : un second
    processus (--workers) sur les mêmes données s'arrête au démarrage."""
    import fcntl
    from llm_proxy import startup
    from llm_proxy.storage import stats
    monkeypatch.setattr(stats, "DB_PATH", str(tmp_path / "stats.db"))
    monkeypatch.setattr(startup, "_HOLD", None)
    other = open(tmp_path / "stats.db.lock", "a+")        # l'autre worker
    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with pytest.raises(SystemExit, match="un seul processus"):
        startup.single_process()
    other.close()
    startup.single_process()                              # libre : passe
    assert startup._HOLD is not None
    startup.single_process()                              # déjà tenu : rien
    startup._HOLD.close()


def test_a_client_gone_midstream_is_counted_499_with_what_it_used(proxy):
    """Raccrocher en plein flux : 499, mais ce qui a été généré compte —
    sinon couper juste avant la fin passerait outre quotas et partage."""
    from llm_proxy.upstream import relaying
    from llm_proxy.upstream.backends import BACKENDS

    up = FakeUpstream(sse(chunk({"role": "assistant", "content": "x" * 400}),
                          chunk({}, "stop", usage=usage(100, 50))))
    call = relaying.Call(BACKENDS["essai"], "essai/qwen",
                         "/v1/chat/completions")

    async def scenario():
        gen = relaying.relay(call, up, relaying.default_tap(
            200, "text/event-stream"), 30)
        await gen.__anext__()
        await gen.aclose()
    asyncio.run(scenario())
    assert up.closed
    (line,) = proxy.lines
    assert line[4] == 499 and line[6] > 0


def test_direct_tool_calls_are_capped_per_client(monkeypatch):
    from llm_proxy.exposure import tool_routes as tr
    monkeypatch.setattr(tr, "DIRECT_PER_MINUTE", 2)
    assert tr.direct_wait("a", now=100) == 0
    assert tr.direct_wait("a", now=110) == 0
    assert tr.direct_wait("a", now=120) == pytest.approx(40)   # 100 + 60
    assert tr.direct_wait("b", now=120) == 0                   # autre client
    assert tr.direct_wait("a", 2, now=161) == pytest.approx(9)  # 110 + 60
    assert tr.direct_wait("a", 3, now=200) == tr.WINDOW        # lot trop gros
    assert tr.direct_wait("a", 2, now=200) == 0
    monkeypatch.setattr(tr, "DIRECT_PER_MINUTE", 0)
    assert tr.direct_wait("a", 50, now=200) == 0


def test_the_routes_answer_429_beyond_the_cap(proxy, monkeypatch):
    from llm_proxy.exposure import mcp_server, tool_routes as tr
    monkeypatch.setattr(tr, "DIRECT_PER_MINUTE", 1)
    monkeypatch.setattr(tr, "direct_wait", lambda client, calls=1: 12.5)
    r = proxy.client.post("/v1/tools/web_search", json={"query": "q"})
    if r.status_code == 404:            # outil inactif dans cette config
        pytest.skip("web_search inactif")
    assert r.status_code == 429 and r.headers["retry-after"] == "13"
    monkeypatch.setattr(mcp_server, "ENABLED", True)
    r = proxy.client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": "web_search", "arguments": {"query": "q"}}})
    assert r.status_code == 429 and r.json()["id"] == 7
