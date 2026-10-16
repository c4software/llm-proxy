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


def test_installable_app_is_served_without_key(ui):
    """Manifeste, icônes et service worker : servis sans clé (le navigateur
    les demande sans cookie), le service worker avec la portée de /ui."""
    r = ui.client.get("/ui/static/manifest.webmanifest")
    assert r.status_code == 200 and r.json()["start_url"] == "/ui"
    assert "manifest+json" in r.headers["content-type"]
    assert ui.client.get("/ui/static/icon-192.png").status_code == 200
    r = ui.client.get("/ui/sw.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert r.headers["service-worker-allowed"] == "/ui"


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
    assert ui.client.get("/ui/session").json()["chat"] == {
        "enabled": False, "hosted_tools": False, "tools": [], "always": [],
        "image_sizes": ["512x512", "768x768", "1024x1024"],
        "image_size": "512x512"}
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


def test_chat_offers_one_switch_per_mcp_server(ui, monkeypatch):
    """Les outils d'un serveur MCP se déclarent ensemble (`mcp:<serveur>`) :
    un seul interrupteur pour eux, un par type pour les autres."""
    from types import SimpleNamespace as T
    found = [T(name="web_search", kinds=("web_search",)),
             T(name="web_fetch", kinds=("web_search",)),
             T(name="issues", kinds=("issues", "mcp:github", "mcp")),
             T(name="pulls", kinds=("pulls", "mcp:github", "mcp"))]
    monkeypatch.setattr(A.tools, "Hosted", lambda: T(tools=found))
    login(ui)
    assert ui.client.get("/ui/session").json()["chat"]["tools"] == [
        {"type": "web_search", "tools": ["web_search", "web_fetch"]},
        {"type": "mcp:github", "tools": ["issues", "pulls"]}]


def test_models_for_chat_or_the_whole_catalogue(ui, monkeypatch):
    """/ui/models : les modèles de conversation ; `?all=1` : tout le
    catalogue, chacun avec son type (le générateur de configuration)."""
    async def merged(request):
        return [{"id": "essai/qwen"}, {"id": "essai/whisper"},
                # Le type du catalogue fait foi : un modèle d'image n'est
                # pas proposé au chat.
                {"id": "essai/Qwen-Image-2.1", "type": "text-to-image"}]
    monkeypatch.setattr(A, "merged_models", merged)
    monkeypatch.setitem(A.BACKENDS["essai"].model_types, "whisper",
                        "automatic-speech-recognition")
    login(ui)
    ids = lambda q: [m["id"] for m in ui.client.get("/ui/models" + q).json()["data"]]
    assert ids("") == ["essai/qwen"]
    assert ids("?all=1") == ["essai/qwen", "essai/whisper", "essai/Qwen-Image-2.1"]
    # Le chat demande aussi les modèles d'images, qu'il fait générer.
    assert ids("?images=1") == ["essai/qwen", "essai/Qwen-Image-2.1"]
    assert [m["image"] for m in ui.client.get("/ui/models?images=1")
            .json()["data"]] == [False, True]


def test_chat_generates_an_image_with_an_image_model(ui, monkeypatch):
    """/ui/images/generations : /v1/images/generations sous le cookie,
    préfixe retiré, réponse relayée telle quelle ; fermée sans clé,
    chat fermé, ou sans corps JSON."""
    body = {"model": "essai/flux", "prompt": "un phare", "size": "512x512"}
    assert ui.client.post("/ui/images/generations", json=body).status_code == 401
    login(ui)
    image = {"data": [{"b64_json": "iVBORw0KGgo="}]}
    ui.replies = [FakeUpstream(json.dumps(image).encode())]
    r = ui.client.post("/ui/images/generations", json=body)
    assert r.status_code == 200 and r.json() == image
    assert ui.sent[-1] == {**body, "model": "flux"}
    assert ui.clients == [tools.owner(KEY)]
    assert ui.client.post("/ui/images/generations", content="{}").status_code == 415
    monkeypatch.setattr(A, "UI_CHAT", False)
    assert ui.client.post("/ui/images/generations", json=body).status_code == 404


def test_chat_passes_the_reasoning_level_through(ui):
    """Le niveau de réflexion du chat arrive au backend tel quel, avec ou
    sans outil hébergé : `reasoning_effort` (llama.cpp, vLLM), ou
    `chat_template_kwargs.enable_thinking` pour la couper."""
    login(ui)
    for extra in ({"reasoning_effort": "high"},
                  {"chat_template_kwargs": {"enable_thinking": False}},
                  {"reasoning_effort": "low", "tools": [{"type": "web_search"}]}):
        ui.replies = [FakeUpstream(stream(*ANSWER_TURN))]
        assert chat(ui, **extra).status_code == 200
        sent = ui.sent[-1]
        for key in ("reasoning_effort", "chat_template_kwargs"):
            assert sent.get(key) == extra.get(key), (key, extra)


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


def test_incognito_chat_leaves_nothing_behind(ui, monkeypatch):
    """`X-Incognito: 1` : l'échange d'outils n'est pas rangé dans la
    mémoire du chat (une requête ordinaire, si) ; la requête suivante,
    elle, n'est pas incognito."""
    login(ui)
    body = {"model": "essai/qwen", "stream": True,
            "messages": [{"role": "user", "content": "Dernière version ?"}],
            "tools": [{"type": "web_search"}]}
    ui.replies = [FakeUpstream(stream(*SEARCH_TURN)),
                  FakeUpstream(stream(*ANSWER_TURN))]
    r = ui.client.post("/ui/chat/completions", json=body,
                       headers={"x-incognito": "1"})
    assert r.status_code == 200 and ui.hosted.runs
    assert len(C.MEMORY) == 0
    ui.replies = [FakeUpstream(stream(*SEARCH_TURN)),
                  FakeUpstream(stream(*ANSWER_TURN))]
    assert ui.client.post("/ui/chat/completions", json=body).status_code == 200
    assert len(C.MEMORY) == 1


def test_incognito_chat_never_declares_memory(ui, monkeypatch):
    """L'outil `memory` (persistant) est retiré d'une requête incognito,
    même déclaré ; une requête ordinaire le garde."""
    login(ui)
    monkeypatch.setattr(C, "ENABLED", False)    # relais brut : `tools` tel quel
    for headers, kept in (({"x-incognito": "1"}, []),
                          ({}, [{"type": "memory"}])):
        ui.replies = [FakeUpstream(stream(*ANSWER_TURN))]
        chat_body = {"model": "essai/qwen", "stream": True,
                     "messages": [{"role": "user", "content": "Bonjour"}],
                     "tools": [{"type": "memory"}]}
        assert ui.client.post("/ui/chat/completions", json=chat_body,
                              headers=headers).status_code == 200
        assert ui.sent[-1]["tools"] == kept


def test_incognito_keeps_no_web_cache_and_short_lived_files():
    """Sous settings.INCOGNITO : le cache web ne range rien, et un fichier
    rendu n'est gardé que files.INCOGNITO_TTL."""
    from llm_proxy import files
    from llm_proxy.settings import INCOGNITO
    from llm_proxy.tools import webcache
    cache = webcache.Cache(60, 8, 1000)
    store = files.Store(4 * 3600, 10_000, 10_000)
    token = INCOGNITO.set(True)
    try:
        cache.put("k", "v", 1)
        short = store.put("a.png", b"\x89PNG\r\n\x1a\n" + b"0" * 20)
    finally:
        INCOGNITO.reset(token)
    long = store.put("b.png", b"\x89PNG\r\n\x1a\n" + b"0" * 20)
    assert len(cache) == 0
    assert short.expires - long.expires < -3600


def test_chat_edits_an_attached_image(ui, monkeypatch):
    """/ui/images/edits : un corps JSON (image en data URL) devient le
    formulaire de /v1/images/edits — préfixe retiré, image lue dans ses
    octets ; refusé sans image lisible."""
    import base64
    from llm_proxy import multipart
    seen = []

    async def send_upstream(call, request, path, body):
        seen.append((path, request.headers["content-type"], body))
        return FakeUpstream(json.dumps({"data": [{"b64_json": "eA=="}]}).encode())

    monkeypatch.setattr(A, "send_upstream", send_upstream)
    login(ui)
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 30
    body = {"model": "essai/flux", "prompt": "plus sombre", "size": "512x512",
            "image": "data:image/png;base64," + base64.b64encode(png).decode()}
    r = ui.client.post("/ui/images/edits", json=body)
    assert r.status_code == 200 and r.json()["data"][0]["b64_json"] == "eA=="
    path, ctype, raw = seen[-1]
    assert path == "v1/images/edits" and ctype.startswith("multipart/form-data")
    assert multipart.model_field(raw, ctype) == "flux"
    assert b"plus sombre" in raw and png in raw and b"image/png" in raw
    assert ui.client.post("/ui/images/edits", json={
        **body, "image": "data:image/png;base64,AAAA"}).status_code == 400
