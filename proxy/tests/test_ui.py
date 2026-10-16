"""Les routes de la page /ui autour du chat : l'entrée par clé
(/ui/login, /ui/logout, /ui/session), les statistiques de la seule clé
connectée, et le chat lui-même (/ui/chat/completions, [ui].chat) — par
la fixture `proxy` de conftest.py. Aucun réseau."""

import json

import pytest

from fakes import ANSWER_TURN, SEARCH_TURN, FakeUpstream, stream
from llm_proxy import app as A
from llm_proxy import chat_api as C
from llm_proxy import stats, tools

KEY = "sk-alice"
JSON = {"content-type": "application/json"}


@pytest.fixture
def ui(proxy, monkeypatch):
    """`proxy`, l'auth active (deux clés), le chat de la page ouvert, les
    outils hébergés de chat/completions actifs ; `clients` note le client
    de chaque ligne de statistiques (stats.CLIENT au moment d'écrire)."""
    monkeypatch.setattr(A, "PROXY_API_KEYS", [KEY, "sk-bob"])
    monkeypatch.setattr(A, "UI_CHAT", True)
    monkeypatch.setattr(C, "ENABLED", True)
    monkeypatch.setattr(C, "MEMORY", C.Memory(8, 60, 100_000))
    proxy.clients = []
    monkeypatch.setattr(A.stats, "record",
                        lambda *a: proxy.clients.append(stats.CLIENT.get()))
    monkeypatch.setattr(A.stats, "record_tool", lambda *a: None)
    return proxy


def login(proxy, key=KEY):
    return proxy.client.post("/ui/login", json={"key": key})


def chat(proxy, **extra):
    body = {"model": "essai/qwen", "stream": True,
            "messages": [{"role": "user", "content": "Dernière version ?"}],
            **extra}
    return proxy.client.post("/ui/chat/completions", json=body)


def blocks(raw: bytes) -> list[dict]:
    return [json.loads(line[6:]) for line in raw.decode().split("\n\n")
            if line.startswith("data: ") and line != "data: [DONE]"]


def test_page_opens_without_key_and_session_asks_for_one(ui):
    """La page et ses fichiers se servent sans clé (c'est elle qui la
    demande) ; ce qu'elle lit, non."""
    assert ui.client.get("/ui").status_code == 200
    assert ui.client.get("/ui/static/dashboard.css").status_code == 200
    assert ui.client.get("/ui/session").status_code == 401
    assert ui.client.get("/ui/usage?start_time=0").status_code == 401


def test_login_checks_the_key_and_sets_the_cookie(ui):
    r = login(ui, "sk-mallory")
    assert r.status_code == 401 and A.UI_COOKIE not in r.cookies
    r = login(ui)
    assert r.status_code == 200 and r.cookies[A.UI_COOKIE] == KEY
    session = ui.client.get("/ui/session").json()
    assert session["auth_required"] is True
    assert session["client"] == tools.owner(KEY)[:8]
    assert session["chat"]["enabled"] is True
    ui.client.post("/ui/logout", json={})
    assert ui.client.get("/ui/session").status_code == 401


def test_query_key_with_a_wrong_value_sets_no_cookie(ui):
    """La page est ouverte sans clé : un «?key=» faux ne doit rien poser."""
    assert A.UI_COOKIE not in ui.client.get("/ui?key=faux").cookies
    assert ui.client.get(f"/ui?key={KEY}").cookies[A.UI_COOKIE] == KEY


def test_writing_routes_want_a_json_body(ui):
    """Un formulaire d'une page tierce ne passe pas : ni l'entrée, ni le
    chat."""
    r = ui.client.post("/ui/login", content=f"key={KEY}",
                       headers={"content-type":
                                "application/x-www-form-urlencoded"})
    assert r.status_code == 415
    login(ui)
    r = ui.client.post("/ui/chat/completions", content="{}",
                       headers={"content-type": "text/plain"})
    assert r.status_code == 415 and not ui.sent


def test_chat_is_closed_unless_enabled(ui, monkeypatch):
    login(ui)
    monkeypatch.setattr(A, "UI_CHAT", False)
    assert chat(ui).status_code == 404
    assert ui.client.get("/ui/models").status_code == 404
    assert ui.client.get("/ui/session").json()["chat"] == {
        "enabled": False, "hosted_tools": False, "tools": [], "always": []}
    assert not ui.sent


def test_chat_counts_to_the_connected_key(ui):
    """Les requêtes du chat sont celles de la personne connectée — et
    celles de l'API, de la clé de leur en-tête."""
    login(ui)
    ui.replies = [FakeUpstream(stream(*ANSWER_TURN))]
    assert chat(ui).status_code == 200
    ui.replies = [FakeUpstream(stream(*ANSWER_TURN))]
    ui.client.post("/v1/chat/completions",
                   json={"model": "essai/qwen", "messages": []},
                   headers={"Authorization": "Bearer sk-bob"})
    assert ui.clients == [tools.owner(KEY), tools.owner("sk-bob")]


def test_chat_announces_hosted_tool_runs(ui):
    """Le chat de la page reçoit `x_hosted_tool` (lancé, puis fini, avec
    le début du résultat) ; un client de l'API, jamais."""
    login(ui)
    ui.replies = [FakeUpstream(stream(*SEARCH_TURN)),
                  FakeUpstream(stream(*ANSWER_TURN))]
    r = chat(ui, tools=[{"type": "web_search"}])
    events = [c["delta"]["x_hosted_tool"] for d in blocks(r.content)
              for c in d["choices"] if "x_hosted_tool" in c["delta"]]
    assert [e["status"] for e in events] == ["running", "done"]
    assert events[0]["name"] == events[1]["name"] == "web_search"
    assert events[1]["chars"] > 0 and events[1]["preview"]
    ui.replies = [FakeUpstream(stream(*SEARCH_TURN)),
                  FakeUpstream(stream(*ANSWER_TURN))]
    r = ui.client.post("/v1/chat/completions",
                       json={"model": "essai/qwen", "stream": True,
                             "tools": [{"type": "web_search"}],
                             "messages": [{"role": "user", "content": "?"}]},
                       headers={"Authorization": f"Bearer {KEY}"})
    assert b"x_hosted_tool" not in r.content


def test_chat_ignores_always(ui, monkeypatch):
    """Les interrupteurs de la page font foi : [chat].always ne s'ajoute
    pas à une requête qui ne déclare rien."""
    login(ui)
    monkeypatch.setattr(C, "ALWAYS", ["web_search"])
    ui.replies = [FakeUpstream(stream(*ANSWER_TURN))]
    chat(ui)
    assert "tools" not in ui.sent[-1]


def test_dashboard_reads_only_the_connected_key(ui, monkeypatch):
    """/ui/usage ne compte que la clé connectée, quoi que demande la
    page ; l'Usage API, elle, voit tout et filtre sur demande."""
    seen = []

    async def read(fn, **kw):
        seen.append(kw["keys"])
        return {}

    monkeypatch.setattr(A, "run_in_threadpool", read)
    login(ui)
    ui.client.get("/ui/usage?start_time=0&api_key_ids=autre")
    ui.client.get("/v1/organization/usage/completions?start_time=0",
                  headers={"Authorization": f"Bearer {KEY}"})
    ui.client.get("/v1/organization/usage/completions?start_time=0"
                  "&api_key_ids=a,b", headers={"Authorization": f"Bearer {KEY}"})
    assert seen == [[tools.owner(KEY)], None, ["a", "b"]]


def test_cookie_is_secure_behind_a_tls_reverse_proxy(ui):
    """NPM termine le TLS et le dit par X-Forwarded-Proto : le cookie est
    `Secure`. En HTTP direct, non — sinon il ne servirait plus."""
    r = ui.client.post("/ui/login", json={"key": KEY},
                       headers={"x-forwarded-proto": "https"})
    assert "secure" in r.headers["set-cookie"].lower()
    r = ui.client.post("/ui/login", json={"key": KEY})
    assert "secure" not in r.headers["set-cookie"].lower()
