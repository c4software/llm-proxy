"""Le limiteur des backends à quotas (quotas.Limiter) : l'attente du verrou guettée
comme celle du quota, et les fenêtres recalées sur l'usage réel ; puis, à
travers le proxy, une coupure upstream en plein flux et le partage
équitable du flux à pings de /v1/messages."""

import asyncio
import json
import time
import types

import httpx2
import pytest

from fakes import FakeUpstream, chunk, everywhere, quota_limited, sse, usage
from llm_proxy.core import identity, limits
from llm_proxy.access import auth, fairshare
from llm_proxy.upstream import albert, backends, quotas, relaying


def run(coro):
    return asyncio.run(coro)


def test_a_client_gone_while_queued_behind_the_lock_leaves_the_queue():
    async def scenario():
        lim = quotas.Limiter("l", "générique", rpm=10, tpm=10_000)
        await lim.lock.acquire()          # une requête devant, qui attend
        gone = asyncio.Event()
        task = asyncio.ensure_future(lim.acquire(5, gone.wait))
        await asyncio.sleep(0.05)
        gone.set()
        with pytest.raises(quotas.ClientGone):
            await asyncio.wait_for(task, 1)
        lim.lock.release()
        assert not lim.lock.locked() and not lim.minute.events
        # Le verrou n'a pas été emporté : la suivante passe.
        assert await asyncio.wait_for(lim.acquire(5), 1) == 0.0
    run(scenario())


def test_the_lock_wait_counts_in_the_waited_time():
    async def scenario():
        lim = quotas.Limiter("l", "générique", rpm=10, tpm=10_000)
        await lim.lock.acquire()
        task = asyncio.ensure_future(
            lim.acquire(5, asyncio.Event().wait))
        await asyncio.sleep(0.1)
        lim.lock.release()
        assert await asyncio.wait_for(task, 1) >= 0.09
    run(scenario())


def test_the_windows_are_settled_on_the_real_usage():
    async def scenario():
        lim = quotas.Limiter("l", "générique", rpm=10, tpm=10_000,
                             tpd=100_000)
        ticket = []
        await lim.acquire(1000, ticket=ticket)       # image en base64
        await lim.acquire(300, ticket=ticket)        # un second tour
        assert lim.minute.tokens == 1300
        quotas.settle(ticket, 260)                   # tokens réels
        assert lim.minute.tokens == lim.day.tokens == 260
        assert [e[1] for e in lim.minute.events] == [200, 60]
        # Un événement sorti de la fenêtre ne se recale plus.
        lim.minute.evict(time.monotonic() + quotas.MINUTE + 1)
        lim.minute.adjust(ticket[0][1], 999)
        assert lim.minute.tokens == 0
    run(scenario())


def test_a_request_refused_upstream_gives_its_place_back(proxy):
    """Refusée par l'amont sans `usage` (422 d'un champ inconnu) : rien
    consommé, sa place estimée quitte la fenêtre — sinon la suivante attend
    la minute entière ; les tours d'avant de la boucle des outils, partis,
    gardent la leur. Un 429 de l'amont (il sature), un client parti ou une
    erreur 5xx (l'amont a pu générer) gardent tout."""
    async def tokens_left_after(status):
        lim = quotas.Limiter("l", "générique", rpm=10, tpm=100_000)
        call = relaying.Call(backends.BACKENDS["essai"], "essai/qwen",
                             "/v1/chat/completions")
        await lim.acquire(30_000, ticket=call.tickets)      # un tour parti
        await lim.acquire(50_000, ticket=call.tickets)      # le tour refusé
        call.done(status)
        return lim.snapshot()["minute"]["tokens"]
    for status, left in ((422, 30_000), (429, 80_000), (relaying.CLIENT_CLOSED, 80_000),
                         (500, 80_000)):
        assert run(tokens_left_after(status)) == left, status

    # Plus grosse que tout le plafond, la suivante attend une fenêtre sans
    # tokens : la place rendue (0 token) ne la retient plus.
    async def oversized_waits_after_a_refusal():
        lim = quotas.Limiter("l", "générique", rpm=10, tpm=100_000)
        call = relaying.Call(backends.BACKENDS["essai"], "essai/qwen",
                             "/v1/chat/completions")
        await lim.acquire(50_000, ticket=call.tickets)
        call.done(422)
        return lim.minute.wait_needed(time.monotonic(), 300_000)
    assert run(oversized_waits_after_a_refusal()) == 0

    # Les limites que l'amont annonce (Mistral) : la place rendue sort
    # aussi de ce qui est parti depuis l'annonce.
    async def waits_under_an_announcement_after_a_refusal():
        state = quotas.QuotaState("mistral")
        try:
            lim = state.get_limiter({"model": "m"})
            state.observe(lim, mistral(left_tok=400_000), time.monotonic())
            call = relaying.Call(backends.BACKENDS["essai"], "essai/qwen",
                                 "/v1/chat/completions")
            await lim.acquire(300_000, ticket=call.tickets)
            call.done(422)
            return lim.minute.wait_needed(time.monotonic(), 300_000)
        finally:
            state.close()
    assert run(waits_under_an_announcement_after_a_refusal()) == 0


def test_an_unlimited_window_keeps_no_ticket():
    async def scenario():
        lim = quotas.Limiter("l", "générique")
        ticket = []
        await lim.acquire(10, ticket=ticket)
        assert ticket == [] and not lim.minute.events
    run(scenario())


def mistral(left_req=999, left_tok=499_983, **day):
    """Ce que Mistral annonce : 1 000 requêtes et 500 000 tokens la minute,
    et ce qu'il en reste ; `day` : ses fenêtres du jour."""
    return {"x-ratelimit-limit-req-minute": "1000", "x-ratelimit-remaining-req-minute": str(left_req),
            "x-ratelimit-limit-tokens-minute": "500000",
            "x-ratelimit-remaining-tokens-minute": str(left_tok), **day}


def test_the_caps_announced_in_the_headers_replace_a_model_s_generic_ones(monkeypatch):
    """Mistral, OpenAI, Groq (its `requests` count the day: a reset beyond a
    minute says so, and the backend stays counted per day) and Anthropic
    announce their caps: a model's own generic bucket takes them (margin
    applied), never a family, the shared overflow bucket nor a backend rule.
    Unreadable, non-ASCII or oversized values change nothing."""
    monkeypatch.setattr(quotas, "FAMILY_LIMITS",
                        {"qwen": {"rpm": 50, "tpm": 1000, "models": ["qwen"]}})
    monkeypatch.setattr(quotas, "PREFIX_MAP", [("qwen", "qwen")])
    generic = {"rpm": 27, "tpm": 115200, "rpd": 0, "tpd": 0}
    cases = [
        ("mistral", mistral(**{"x-ratelimit-limit-tokens-day": "9000000",
                               "x-ratelimit-remaining-tokens-day": "9000000"}),
         {"rpm": 900, "tpm": 450000, "rpd": 0, "tpd": 8100000}),
        ("openai", {"x-ratelimit-limit-requests": "500", "x-ratelimit-remaining-requests": "499",
                    "x-ratelimit-reset-requests": "120ms", "x-ratelimit-limit-tokens": "30000",
                    "x-ratelimit-remaining-tokens": "29000", "x-ratelimit-reset-tokens": "2s"},
         {"rpm": 450, "tpm": 27000, "rpd": 0, "tpd": 0}),
        ("groq", {"x-ratelimit-limit-requests": "1000", "x-ratelimit-remaining-requests": "999",
                  "x-ratelimit-reset-requests": "1m26.4s", "x-ratelimit-limit-tokens": "8000",
                  "x-ratelimit-remaining-tokens": "7900", "x-ratelimit-reset-tokens": "750ms"},
         {"rpm": 27, "tpm": 7200, "rpd": 900, "tpd": 0}),
        ("anthropic", {"anthropic-ratelimit-requests-limit": "1",
                       "anthropic-ratelimit-requests-remaining": "1",
                       "anthropic-ratelimit-tokens-limit": "40000",
                       "anthropic-ratelimit-tokens-remaining": "39000"},
         {"rpm": 1, "tpm": 36000, "rpd": 0, "tpd": 0}),       # 1 × 0.9 : 1, pas illimité
        ("illisible", {"x-ratelimit-limit-req-minute": "1" + "0" * 400,
                       "x-ratelimit-remaining-req-minute": "1",
                       "x-ratelimit-limit-tokens-minute": "1_000",
                       "x-ratelimit-remaining-tokens-minute": "１０"}, generic),
    ]
    for case, headers, expected in cases:
        state = quotas.QuotaState(case, limits.rules([{"models": ["regle"], "rpm": 10}]))
        try:
            get = lambda m: state.get_limiter({"model": m})
            own, rule, family = get("m"), get("regle"), get("qwen-x")
            for lim in (own, rule, family):
                state.observe(lim, headers, time.monotonic())
            caps = {"rpm": own.minute.max_req, "tpm": own.minute.max_tok,
                    "rpd": own.day.max_req, "tpd": own.day.max_tok}
            assert caps == expected, case
            assert own.source == ("générique" if case == "illisible" else "en-têtes"), case
            assert (rule.source, rule.minute.max_req) == ("backend", 9), case
            assert (family.source, family.minute.max_req) == ("famille", 45), case
        finally:
            state.close()
    # Groq ensuite : un reset court ne fait pas revenir ses `requests` à la minute.
    state = quotas.QuotaState("groq")
    try:
        lim, other = state.get_limiter({"model": "m"}), state.get_limiter({"model": "n"})
        for bucket, reset in ((other, "6s"), (lim, "6s"), (lim, "14m24s"), (lim, "6s")):
            state.observe(bucket, {"x-ratelimit-limit-requests": "1000",
                                   "x-ratelimit-remaining-requests": "990",
                                   "x-ratelimit-reset-requests": reset}, time.monotonic())
        # Le rpm des réponses à 6 s ôté, de tous les buckets.
        assert (lim.minute.max_req, lim.day.max_req, other.minute.max_req) == (27, 900, 27)
    finally:
        state.close()
    # Le générique commun (au-delà de MAX_BUCKETS) ne prend les plafonds d'aucun modèle.
    monkeypatch.setattr(quotas, "MAX_BUCKETS", 0)
    state = quotas.QuotaState("plein")
    try:
        shared = state.get_limiter({"model": "a"})
        state.observe(shared, mistral(), time.monotonic())
        assert (shared.name, shared.source, shared.minute.max_req) == ("*", "générique", 27)
    finally:
        state.close()


def test_what_the_upstream_says_is_left_holds_the_requests_back(monkeypatch):
    """Another client of the same key (Vibe CLI on a Mistral key): once the
    upstream says nothing is left (margin kept), requests wait — whatever
    the bucket, a backend rule included, capped window or not — rather than
    get its 429; with no time to wait, a local 429. Each dimension waits
    for its own reset, a minute at most, even for a day window; then a
    single request goes out to probe the upstream, the next ones wait for
    its answer. An announcement older than the current one is ignored."""
    monkeypatch.setattr(quotas, "MAX_QUEUE_SECONDS", 0)
    tell = lambda state, lim, headers: state.observe(lim, headers, time.monotonic())

    async def scenario():
        state = quotas.QuotaState("mistral", limits.rules([{"models": ["regle"], "rpm": 5},
                                                           {"models": ["jour"], "rpd": 900}]))
        try:
            for model in ("m", "regle", "jour"):            # « jour » : minute sans plafond
                lim = state.get_limiter({"model": model})
                tell(state, lim, mistral(left_req=102))     # marge : 100 de réserve
                await lim.acquire(10)
                await lim.acquire(10)
                with pytest.raises(quotas.QuotaWaitTooLong):
                    await lim.acquire(10)
                tell(state, lim, mistral(left_req=500, left_tok=50_010))
                with pytest.raises(quotas.QuotaWaitTooLong):
                    await lim.acquire(20)                   # 10 tokens de place
                await lim.acquire(10)
                assert lim.left == {"rpm": 500, "tpm": 50_010}, model
                # Plus ancienne que celle en cours : sans effet.
                tell(state, lim, mistral())
                state.observe(lim, mistral(left_req=0), time.monotonic() - 5)
                await lim.acquire(1)
            lim = state.get_limiter({"model": "m"})
            # Tokens épuisés pour 30 s, requêtes libres dans 0,1 s : 30 s.
            now = time.monotonic() + 1
            state.observe(lim, {"x-ratelimit-limit-requests": "500",
                              "x-ratelimit-remaining-requests": "400",
                              "x-ratelimit-reset-requests": "100ms",
                              "x-ratelimit-limit-tokens": "30000",
                              "x-ratelimit-remaining-tokens": "0",
                                "x-ratelimit-reset-tokens": "30s"}, now)
            assert lim.minute.wait_needed(now, 1) == 30
            # Le jour épuisé ailleurs : une minute d'attente, pas une journée ;
            # puis une sonde, et la suivante attend sa réponse.
            now += 1
            state.observe(lim, mistral(**{"x-ratelimit-limit-tokens-day": "9000000",
                                          "x-ratelimit-remaining-tokens-day": "0"}), now)
            assert lim.day.wait_needed(now, 1) == quotas.MINUTE
            later = now + quotas.MINUTE
            # Une sonde retenue par autre chose n'est pas partie : rien ne bloque.
            assert lim.day.wait_needed(later, 1) == lim.day.wait_needed(later + 1, 1) == 0
            lim.day.record(later + 1, 1)                    # la sonde part
            assert lim.day.wait_needed(later + 2, 1) == quotas.MINUTE - 1
            # La réponse de la sonde, datée de sa place : la place revient.
            state.observe(lim, mistral(**{"x-ratelimit-limit-tokens-day": "9000000",
                                          "x-ratelimit-remaining-tokens-day": "9000000"}),
                          later + 1)
            assert lim.day.wait_needed(later + 2, 1) == 0
        finally:
            state.close()
    run(scenario())


class Broken(FakeUpstream):
    """Un upstream qui tombe après le premier morceau."""

    async def aiter_raw(self):
        yield self.body[:40]
        raise httpx2.ReadError("boom")


def test_a_failed_reread_keeps_what_was_read():
    """A later /v1/limits that is not the expected JSON changes nothing; a
    /v1/models that fails keeps the known aliases on their router."""
    replies = iter([
        (200, {"data": [{"router_id": 1, "model": "big", "limits": [
            {"type": "rpm", "value": 50, "remaining": 50}]}]}, {"data": [{"id": "big", "aliases": ["b"]}]}),
        (200, "<html>maintenance</html>", {"data": []}),
        (200, {"data": [{"router_id": 1, "model": "big", "limits": [
            {"type": "rpm", "value": 40, "remaining": 40}]}]}, None),
    ])

    def handler(request):
        if request.url.path == "/v1/limits":
            handler.current = next(replies)
            status, body, _ = handler.current
            return (httpx2.Response(status, text=body) if isinstance(body, str)
                    else httpx2.Response(status, json=body))
        models = handler.current[2]
        return httpx2.Response(500) if models is None else httpx2.Response(200, json=models)

    async def go():
        client = httpx2.AsyncClient(base_url="http://albert.invalid",
                                    transport=httpx2.MockTransport(handler))
        backend = types.SimpleNamespace(client=client, api_key="k", meta_timeout=1,
                                        auth_headers=lambda: {})
        state = quotas.QuotaState("test")
        try:
            got = [await albert.refresh(state, backend)]
            got.append(await albert.refresh(state, backend))
            kept = state.get_limiter({"model": "b"}).minute.max_req
            got.append(await albert.refresh(state, backend))
            return got, kept, state.get_limiter({"model": "b"})
        finally:
            state.close()
            await client.aclose()
    got, kept, alias = asyncio.run(go())
    assert got == [True, False, True] and kept == 45
    assert (alias.source, alias.minute.max_req) == ("compte", 36)


def test_account_limits_are_read_per_model_from_v1_limits():
    """/v1/limits: each model gets its router's caps (null = unlimited),
    shared with its aliases and the other models of that router; what Albert
    says is left is kept for the view; a model it does not list falls back.
    Without a key there is no account to read."""
    def caps(rpm, tpm):
        return [{"type": "rpm", "value": rpm, "remaining": rpm - 1},
                {"type": "tpm", "value": tpm, "remaining": 0},
                {"type": "rpd", "value": None, "remaining": 0}]

    def handler(request):
        if request.url.path == "/v1/limits":
            return httpx2.Response(200, json={"data": [
                {"router_id": 1, "model": "Big-Model", "limits": caps(50, None)},
                {"router_id": 1, "model": "big-model-2", "limits": caps(50, None)},
                {"router_id": 2, "model": "small", "limits": caps(10, 1000)}]})
        return httpx2.Response(200, json={"data": [{"id": "big-model", "aliases": ["big"]}]})

    async def go(key):
        client = httpx2.AsyncClient(base_url="http://albert.invalid",
                                    transport=httpx2.MockTransport(handler))
        backend = types.SimpleNamespace(client=client, api_key=key, meta_timeout=1,
                                        auth_headers=lambda: {})
        state = quotas.QuotaState("test")
        try:
            got = await albert.refresh(state, backend)
            get = lambda m: state.get_limiter({"model": m})
            return got, get, state.snapshot()
        finally:
            state.close()
            await client.aclose()
    got, get, snap = asyncio.run(go("k"))
    assert got is True
    assert get("big") is get("BIG-MODEL") is get("big-model-2")
    big = get("big")
    assert (big.source, big.minute.max_req, big.minute.max_tok, big.day.max_req) == \
        ("compte", 45, 0, 0)
    assert get("small").minute.max_tok == 900 and get("small") is not big
    assert get("absent").source == "générique"
    assert snap["routers"]["1"]["remaining"] == {"rpm": 49}
    assert asyncio.run(go(""))[0] is None


def test_backend_rules_come_before_the_account_and_the_families(monkeypatch):
    """A `limits` rule shares one bucket among the models it names and wins
    over what the account publishes and over the families; new rules restart
    only their own buckets."""
    monkeypatch.setattr(quotas, "FAMILY_LIMITS",
                        {"qwen": {"rpm": 50, "tpm": 1000, "models": ["qwen"]}})
    monkeypatch.setattr(quotas, "PREFIX_MAP", [("qwen", "qwen")])
    state = quotas.QuotaState("test", [{"models": ["claude-*", "qwen-big"], "rpm": 10,
                                        "tpm": 0, "rpd": 0, "tpd": 5000}])
    try:
        get = lambda m: state.get_limiter({"model": m})
        assert get("claude-a") is get("claude-b") is get("org/qwen-big")
        assert (get("claude-a").source, get("claude-a").day.max_tok) == ("backend", 4500)
        assert get("qwen-small").source == "famille"
        assert get("autre").source == "générique"
        state.set_account({7: {"rpm": 5, "tpm": 0, "rpd": 0, "tpd": 0}},
                          {"claude-a": "router:7", "gpt-x": "router:7"},
                          {"router:7": "gpt-x"}, {}, 0.0)
        assert get("claude-a").source == "backend" and get("gpt-x").source == "compte"
        account, family, generic = get("gpt-x"), get("qwen-small"), get("autre")
        before = get("claude-b")
        state.set_rules(list(state.rules))           # mêmes règles : rien ne bouge
        assert get("claude-b") is before
        state.set_rules([{"models": ["qwen-*"], "rpm": 1, "tpm": 0, "rpd": 0, "tpd": 0}])
        assert get("qwen-small").source == "backend" and get("claude-b").source == "générique"
        # Le compte, les familles et les génériques gardent leurs fenêtres.
        assert get("gpt-x") is account and get("autre") is generic
        assert get("claude-a").source == "compte"   # sans règle : le compte
        assert get("qwen-tiny") is not family and get("qwen-small") is get("qwen-tiny")
    finally:
        state.close()


def test_a_malformed_limits_rule_is_refused_even_in_the_toml():
    for bad in ([{"rpm": 1}], [{"models": ["x"]}], [{"models": ["x"], "rpm": -1}],
                [{"models": ["x"], "rpm": True}], [{"models": [], "rpm": 1}],
                [{"models": ["x"], "rpm": 1, "rpx": 2}], {"models": ["x"]}):
        with pytest.raises(ValueError):
            limits.rules(bad)
    assert limits.rules([{"models": "Claude-*", "tpd": 9}]) == [
        {"models": ["claude-*"], "rpm": 0, "tpm": 0, "rpd": 0, "tpd": 9}]
    with pytest.raises(ValueError):
        limits.rules([{"models": [f"m{i}" for i in range(21)], "rpm": 1}])
    # Une table [[backends.<nom>.limits]] : lue, ou le démarrage s'arrête.
    b = backends.Backend("zen", {"url": "http://zen.invalid", "quotas": True,
                                 "limits": [{"models": ["claude-*"], "rpm": 5}]})
    try:
        assert b.quota_state.get_limiter({"model": "claude-x"}).source == "backend"
    finally:
        b.quota_state.close()
    with pytest.raises(SystemExit):
        backends.Backend("zen", {"url": "http://zen.invalid", "limits": [{"rpm": 5}]})


def test_no_account_limits_stops_the_background_refresh(monkeypatch):
    """/v1/limits → 404 (not Albert) or 401 (credentials that read no
    account, e.g. headers without a key): said once, never asked again."""
    for status, cfg in ((404, {"api_key": "k"}), (401, {"headers": {"X-Equipe": "ia"}})):
        asked = []

        def handler(request):
            asked.append(request.url.path)
            return httpx2.Response(status, json={"detail": "no"})

        async def go():
            b = backends.Backend("zen", {"url": "http://zen.invalid", "quotas": True, **cfg})
            b.client = httpx2.AsyncClient(base_url=b.url, transport=httpx2.MockTransport(handler))
            monkeypatch.setattr(quotas, "RETRY_UNLOADED", 0.0)
            try:
                await asyncio.wait_for(backends.watch_quotas(b), 2)
            finally:
                b.quota_state.close()
                await b.client.aclose()
        asyncio.run(go())
        assert asked == ["/v1/limits"], status


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
    quota_limited(monkeypatch, BACKENDS["essai"], accepted=10)

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
