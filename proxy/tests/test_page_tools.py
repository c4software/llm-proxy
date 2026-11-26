"""Les outils du navigateur pour la page (ui/page_tools.py) : leurs
déclarations, servies par /ui/session depuis le registre (tools/page/) ;
ceux que refuse [ui].page_tools_disabled, la résolution des entrées,
absents de la session et retirés de toute requête du chat de la page —
pas des autres routes."""

from fakes import FakeUpstream, chunk, stream
from test_ui import chat, login, ui  # noqa: F401 — la fixture du chat de la page
from llm_proxy.ui import page_tools

FN = lambda name: {"type": "function", "function": {"name": name, "parameters": {}}}  # noqa: E731


def test_les_declarations_viennent_du_registre():
    """Chaque famille du menu, dans l'ordre, et la fonction de chacun de
    ses outils, assemblée par le contrat (Tool.spec) avec ses `settings`."""
    page = page_tools.declarations()
    families = {f["key"]: f["tools"] for f in page["families"]}
    assert families["camera"] == ["take_photo"]
    assert families["compute"] == ["run_javascript", "check_typescript"]
    assert [t["name"] for t in page["tools"]] == [n for ts in families.values() for n in ts]
    sql = next(t for t in page["tools"] if t["name"] == "run_sql")
    # La borne que le prompt cite est celle que la page applique.
    assert sql["settings"] == {"max_rows": 200} and "200 rows" in sql["spec"]["function"]["description"]
    assert not any("module" in t for t in page["tools"])          # natifs : la page les a


def test_resolution_familles_et_outils():
    fams, names = page_tools._resolve(["camera", "run_php", "inconnu", ""])
    # run_php seul dans sa famille : la famille `php` est refusée aussi.
    assert fams == ["camera", "php"] and names == ["run_php", "take_photo"]
    fams, names = page_tools._resolve(["run_javascript"])
    assert fams == [] and names == ["run_javascript"]            # compute garde check_typescript


def refuse(monkeypatch, entries):
    fams, names = page_tools._resolve(entries)
    monkeypatch.setattr(page_tools, "DISABLED_FAMILIES", fams)
    monkeypatch.setattr(page_tools, "DISABLED_TOOLS", names)


def test_strip(monkeypatch):
    payload = {"tools": [FN("take_photo"), FN("get_location"), {"type": "web_search"}]}
    assert page_tools.strip(payload) is False                     # rien de refusé
    refuse(monkeypatch, ["camera"])
    assert page_tools.strip(payload) is True
    # Les fonctions refusées seulement ; un outil hébergé déclaré reste.
    assert payload["tools"] == [FN("get_location"), {"type": "web_search"}]


def test_retirees_de_la_requete_du_chat_de_la_page(ui, monkeypatch):
    refuse(monkeypatch, ["camera", "run_php"])
    login(ui)
    page = ui.client.get("/ui/session").json()["chat"]["page"]
    keys = {f["key"] for f in page["families"]}
    names = {t["name"] for t in page["tools"]}
    assert not {"camera", "php"} & keys and not {"run_php", "take_photo"} & names
    assert "location" in keys and "get_location" in names
    ui.replies = [FakeUpstream(stream(chunk({"role": "assistant", "content": "ok"}, "stop")))]
    r = chat(ui, tools=[FN("take_photo"), FN("get_location"), FN("run_php")], tool_choice="auto")
    assert r.status_code == 200
    sent = ui.sent[-1]
    assert [t["function"]["name"] for t in sent["tools"]] == ["get_location"]
    # Toutes refusées : ni `tools`, ni `tool_choice`.
    ui.replies = [FakeUpstream(stream(chunk({"role": "assistant", "content": "ok"}, "stop")))]
    chat(ui, tools=[FN("take_photo")], tool_choice="auto")
    assert "tools" not in ui.sent[-1] and "tool_choice" not in ui.sent[-1]


def test_les_autres_routes_ne_sont_pas_touchees(proxy, monkeypatch):
    refuse(monkeypatch, ["camera"])
    proxy.replies = [FakeUpstream(stream(chunk({"role": "assistant", "content": "ok"}, "stop")))]
    body = {"model": "essai/qwen", "stream": True, "messages": [{"role": "user", "content": "x"}],
            "tools": [FN("take_photo")]}
    proxy.client.post("/v1/chat/completions", json=body)
    assert [t["function"]["name"] for t in proxy.sent[-1]["tools"]] == ["take_photo"]
