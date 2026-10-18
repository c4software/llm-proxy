"""Le rendu d'une page par un navigateur (tools/render.py, browserless) et
son usage par web_fetch et web_grep (`render`, `screenshot`) : la requête
envoyée, les hôtes refusés (dont RENDER_BLOCKED_HOSTS, de
l'environnement), le rendu jeté s'il finit sur une adresse interne, les
pannes du service, le cache et la capture."""

import base64
import json

import httpx2
import pytest

from fakes import fake_dns, go
from llm_proxy import tools
from llm_proxy.tools import net, render, web_fetch, web_grep, webcache

PAGE = "<html><head><title>Appli</title></head><body><h1>Rendu par JS</h1><p>Contenu chargé.</p></body></html>"


@pytest.fixture(autouse=True)
def service(monkeypatch):
    monkeypatch.setattr(render, "URL", "http://browserless:3000")
    monkeypatch.setattr(render, "TOKEN", "jeton")
    monkeypatch.delenv("RENDER_BLOCKED_HOSTS", raising=False)
    monkeypatch.setattr(net, "BLOCKED_DOMAINS", [])
    monkeypatch.setattr(net, "ALLOWED_DOMAINS", [])
    monkeypatch.setattr(net, "ALLOW_PRIVATE", False)
    webcache.CACHE.clear()
    fake_dns(monkeypatch, {"appli.exemple.fr": ["93.184.216.34"],
                           "interne.exemple.fr": ["192.168.1.20"]})


def browserless(reply=None, status=200, seen=None):
    """Un faux browserless : note la requête, rend `reply` (le `data` de la fonction)."""
    def handler(request):
        if seen is not None:
            seen.append(request)
        if status != 200:
            return httpx2.Response(status, text="non")
        data = {"url": "https://appli.exemple.fr/", "status": 200, "ip": "93.184.216.34",
                "title": "Appli", "html": PAGE, "truncated": False, "screenshot": "",
                "refused": [], "error": "", **(reply or {})}
        return httpx2.Response(200, json=data)
    return httpx2.MockTransport(handler)


def test_requete_envoyee_a_browserless():
    seen = []
    r = go(render.render("https://appli.exemple.fr/", transport=browserless(seen=seen)))
    assert r.url == "https://appli.exemple.fr/" and r.title == "Appli" and "Rendu par JS" in r.html
    req = seen[0]
    assert req.url.path == "/function" and req.headers["authorization"] == "Bearer jeton"
    assert "token" not in req.url.params                 # jamais dans l'URL (journaux)
    body = json.loads(req.content)
    assert "setRequestInterception" in body["code"]
    ctx = body["context"]
    assert ctx["url"] == "https://appli.exemple.fr/" and ctx["screenshot"] is False
    assert r"^[^.]+$" in ctx["blocked"]


def test_hotes_refuses_par_le_navigateur(monkeypatch):
    monkeypatch.setenv("RENDER_BLOCKED_HOSTS", "secret.maison, https://autre.lan/x")
    for url in ("http://localhost:8123/", "http://homeassistant:8123/", "http://10.0.0.1/",
                "http://192.168.1.1/", "http://172.20.0.5/", "http://100.64.0.4/",
                "http://nas.local/", "http://ha.home.arpa/", "https://x.secret.maison/",
                "https://autre.lan/", "http://[::1]/"):
        assert render.host_blocked(url), url
    for url in ("https://exemple.fr/", "https://172.32.0.1/", "https://secretmaison.fr/"):
        assert not render.host_blocked(url), url


def test_la_page_demandee_doit_etre_publique():
    with pytest.raises(tools.ToolError) as exc:
        go(render.render("https://interne.exemple.fr/", transport=browserless()))
    assert exc.value.code == "not_allowed"


def test_rendu_jete_s_il_finit_sur_une_adresse_interne():
    for reply in ({"url": "http://192.168.1.1/admin"}, {"ip": "10.0.0.7"},
                  {"url": "http://homeassistant:8123/"}):
        with pytest.raises(tools.ToolError) as exc:
            go(render.render("https://appli.exemple.fr/", transport=browserless(reply)))
        assert exc.value.code == "not_allowed", reply


def test_pannes_du_service():
    for status, code in ((429, "too_many_requests"), (500, "unavailable"), (401, "unavailable")):
        with pytest.raises(tools.ToolError) as exc:
            go(render.render("https://appli.exemple.fr/", transport=browserless(status=status)))
        assert exc.value.code == code
    with pytest.raises(tools.ToolError) as exc:
        go(render.render("https://appli.exemple.fr/", transport=browserless({"html": "", "error": "net::ERR_NAME_NOT_RESOLVED"})))
    assert exc.value.code == "not_accessible" and "ERR_NAME_NOT_RESOLVED" in exc.value.message
    with pytest.raises(tools.ToolError) as exc:
        go(render.render("https://appli.exemple.fr/", transport=browserless({"status": 404})))
    assert exc.value.code == "not_accessible"


def test_non_configure(monkeypatch):
    monkeypatch.setattr(render, "URL", "")
    assert not render.available()
    props = web_fetch.TOOL.parameters(frozenset())["properties"]
    assert "render" not in props and "render: true" not in web_fetch.TOOL.prompt(frozenset())
    with pytest.raises(tools.ToolError) as exc:
        go(render.render("https://appli.exemple.fr/"))
    assert exc.value.code == "unavailable"


def fake_render(monkeypatch, calls, image=None):
    async def fake(url, settings=None, *, screenshot=False, transport=None):
        calls.append((url, screenshot))
        return render.Rendered("https://appli.exemple.fr/", 200, "Appli", PAGE, False,
                               image if screenshot else None, ())
    monkeypatch.setattr(render, "render", fake)


def test_web_fetch_render_et_cache(monkeypatch):
    calls = []
    fake_render(monkeypatch, calls)
    props = web_fetch.TOOL.parameters(frozenset())["properties"]
    assert {"render", "screenshot"} <= props.keys()
    assert "render: true" in web_fetch.TOOL.prompt(frozenset())
    for _ in range(2):
        out = go(web_fetch.TOOL.run({"url": "https://appli.exemple.fr/", "render": True}, tools.Call()))
        assert "Rendu par JS" in out.text and out.meta["title"] == "Appli"
    assert calls == [("https://appli.exemple.fr/", False)]          # la seconde : en cache


def test_web_fetch_capture(monkeypatch):
    calls = []
    fake_render(monkeypatch, calls, image=b"\xff\xd8jpeg")
    out = go(web_fetch.TOOL.run({"url": "https://appli.exemple.fr/", "render": True,
                                 "screenshot": True}, tools.Call()))
    assert out.files[0].name == "capture.jpg" and out.files[0].data == b"\xff\xd8jpeg"
    assert "screenshot" in out.text
    with pytest.raises(tools.ToolError) as exc:
        go(web_fetch.TOOL.run({"url": "https://appli.exemple.fr/", "screenshot": True}, tools.Call()))
    assert exc.value.code == "invalid_input"


def test_web_grep_render(monkeypatch):
    calls = []
    fake_render(monkeypatch, calls)
    assert "render" in web_grep.TOOL.parameters(frozenset())["properties"]
    out = go(web_grep.TOOL.run({"url": "https://appli.exemple.fr/", "pattern": "chargé",
                                "render": True}, tools.Call()))
    assert "Contenu chargé" in out.text and calls


def test_capture_decodee():
    img = base64.b64encode(b"\xff\xd8x").decode()
    r = go(render.render("https://appli.exemple.fr/", screenshot=True,
                         transport=browserless({"screenshot": img})))
    assert r.image == b"\xff\xd8x"
