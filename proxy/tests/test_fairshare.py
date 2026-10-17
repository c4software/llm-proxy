"""Le partage équitable (fairshare.py) : la fenêtre glissante, puis, à
travers le proxy (fixture `proxy`), l'attente d'un client au-delà de sa
part — sans 429 —, la part réglée par compte, les backends et les routes
qui n'en relèvent pas. La fenêtre est raccourcie à une fraction de seconde
pour que l'attente se mesure vite."""

import time

import pytest

from fakes import FakeUpstream, chat_doc
from llm_proxy import auth, fairshare, tools

KEY = "sk-alice"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    fairshare.reset()
    yield
    fairshare.reset()


def test_window_slides():
    fairshare.charge("a", 60, now=1000)
    fairshare.charge("a", 50, now=2000)
    assert fairshare.used("a", now=2000) == 110
    # Sous la part : rien à attendre ; au-dessus : jusqu'à ce que les plus
    # anciens tokens sortent de l'heure.
    assert fairshare.free_at("a", 200, now=2000) is None
    assert fairshare.free_at("a", 100, now=2000) == 1000 + fairshare.WINDOW
    assert fairshare.free_at("a", 40, now=2000) == 2000 + fairshare.WINDOW
    assert fairshare.used("a", now=1000 + fairshare.WINDOW + 1) == 50
    fairshare.charge(None, 500)                 # proxy ouvert : rien
    fairshare.charge("b", 0)
    assert fairshare.snapshot()["tracked"] == 1


def test_seed_refills_the_windows():
    now = time.time()
    assert fairshare.seed([(now - 10, "a", 30), (now - 5, "a", 20),
                           (now - 1, "b", 7)]) == 3
    assert fairshare.used("a") == 50 and fairshare.used("b") == 7


@pytest.fixture
def shared(proxy, monkeypatch):
    """`proxy` avec une clé, une part de 100 tokens par défaut et une
    fenêtre de 0,4 s ; chaque réponse du backend coûte 150 tokens."""
    from llm_proxy import chat_api
    monkeypatch.setattr(auth, "PROXY_API_KEYS", [KEY])
    monkeypatch.setattr(chat_api, "ENABLED", False)     # relais brut
    monkeypatch.setattr(fairshare, "DEFAULT_SHARE", 100)
    monkeypatch.setattr(fairshare, "WINDOW", 0.4)
    monkeypatch.setattr(fairshare, "CHECK_EVERY", 0.05)
    proxy.client.headers["authorization"] = f"Bearer {KEY}"
    return proxy


def ask(env, model="essai/qwen"):
    env.replies.append(FakeUpstream(chat_doc(
        {"role": "assistant", "content": "ok"}, "stop", 100, 50)))
    started = time.monotonic()
    r = env.client.post("/v1/chat/completions", json={
        "model": model, "messages": [{"role": "user", "content": "q"}]})
    return r, time.monotonic() - started


def test_over_its_share_a_client_waits_then_passes(shared):
    r, first = ask(shared)
    assert r.status_code == 200 and first < 0.3
    assert fairshare.used(tools.owner(KEY)) == 150
    # 150 > 100 : la requête suivante attend que la fenêtre se vide — et
    # passe, sans 429.
    r, second = ask(shared)
    assert r.status_code == 200
    assert second >= 0.3


def test_no_default_share_means_no_limit(shared, monkeypatch):
    monkeypatch.setattr(fairshare, "DEFAULT_SHARE", 0)
    ask(shared)
    r, second = ask(shared)
    assert r.status_code == 200 and second < 0.3


def test_an_excluded_backend_neither_counts_nor_waits(shared, monkeypatch):
    from llm_proxy.backends import BACKENDS
    monkeypatch.setattr(BACKENDS["essai"], "fair_share", False)
    ask(shared)
    assert fairshare.used(tools.owner(KEY)) == 0
    r, second = ask(shared)
    assert second < 0.3


def test_an_open_proxy_counts_nobody(proxy, monkeypatch):
    monkeypatch.setattr(fairshare, "DEFAULT_SHARE", 1)
    ask(proxy)
    r, second = ask(proxy)
    assert r.status_code == 200 and second < 0.3
    assert fairshare.snapshot()["tracked"] == 0


def test_the_session_tells_the_share_and_its_use(shared):
    ask(shared)
    s = shared.client.get("/ui/session").json()["fair_share"]
    assert s["share"] == 100 and s["used"] == 150 and s["default"] == 100
