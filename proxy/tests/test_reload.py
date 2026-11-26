"""Le rechargement à chaud (core/reload.py) : les réglages de SAFE
appliqués sans redémarrer — dans leur module, dans chaque copie qu'en fait
un `from … import`, et dans ce qui en dérive —, le reste signalé ; une
configuration invalide ne change rien. Chaque test repart de la
configuration d'exemple."""

import pytest

from llm_proxy.core import config, reload

ORIGINAL = config.CONFIG_PATH


@pytest.fixture
def edited(tmp_path):
    """Écrit une copie modifiée de la configuration en vigueur ; la
    configuration d'origine est rechargée après le test."""
    text = open(ORIGINAL, encoding="utf-8").read()

    def write(*pairs, name="config.toml"):
        out = text
        for old, new in pairs:
            assert old in out, old
            out = out.replace(old, new, 1)
        path = tmp_path / name
        path.write_text(out, encoding="utf-8")
        return str(path)
    yield write
    reload.reload(ORIGINAL)


def test_safe_settings_apply_everywhere_they_are_copied(edited):
    from llm_proxy import tools
    from llm_proxy.access import auth
    from llm_proxy.surfaces import anthropic_api, tool_loop
    from llm_proxy.surfaces.anthropic_api import response
    from llm_proxy.tools import code_execution
    path = edited(("max_calls = 8\n", "max_calls = 3\n"),
                  ("reasoning_as_thinking = true", "reasoning_as_thinking = false"),
                  ("[anthropic.model_map]\n", '[anthropic.model_map]\n"zz-essai" = "albert/x"\n'),
                  ("upstream_timeout = 600", "upstream_timeout = 601"))
    result = reload.reload(path)
    assert result["applied"] == ["anthropic.model_map",
                                 "anthropic.reasoning_as_thinking",
                                 "tools.max_calls"]
    assert result["restart"] == ["proxy.upstream_timeout"]
    assert tools.MAX_CALLS == 3
    # Ce qui en dérive, et les copies (`from .common import …`).
    assert tool_loop.HOSTED_HARD_LIMIT == 3 + tool_loop.HOSTED_EXTRA_CALLS
    assert anthropic_api.REASONING_AS_THINKING is False
    assert response.REASONING_AS_THINKING is False
    assert anthropic_api.MODEL_MAP["zz-essai"] == "albert/x"
    assert anthropic_api.resolve_model("zz-essai", {"albert": None}) == "albert/x"
    # Une autre constante du même nom, d'une autre clé : intacte.
    assert code_execution.MAX_CALLS == 8
    # Non rechargeable : la configuration en vigueur ne bouge pas.
    assert config.get("proxy.upstream_timeout") != 601
    assert auth.PROXY_API_KEYS is not None


def test_la_limite_de_tours_du_chat_rechargee_part_avec_la_session(edited, proxy):
    """[ui].chat_max_rounds : la page la reçoit avec /ui/session (pas de
    borne en dur côté page) ; rechargée à chaud, la session suivante donne
    la nouvelle ; une valeur sous 1 est refusée."""
    assert proxy.client.get("/ui/session").json()["chat"]["max_rounds"] == 50
    result = reload.reload(edited(("chat_max_rounds = 50", "chat_max_rounds = 7")))
    assert result["applied"] == ["ui.chat_max_rounds"] and result["restart"] == []
    assert proxy.client.get("/ui/session").json()["chat"]["max_rounds"] == 7
    # Sous 1 : refusée au rechargement comme au démarrage, rien ne change.
    with pytest.raises(reload.ReloadError):
        reload.reload(edited(("chat_max_rounds = 50", "chat_max_rounds = 0"), name="zero.toml"))
    assert proxy.client.get("/ui/session").json()["chat"]["max_rounds"] == 7


def test_an_invalid_value_changes_nothing(edited):
    from llm_proxy import tools
    from llm_proxy.surfaces import tool_loop
    before = (tools.MAX_CALLS, tool_loop.HOSTED_HARD_LIMIT, config.CONFIG)
    path = edited(("max_calls = 8\n", 'max_calls = "trois"\n'),
                  ("reasoning_as_thinking = true", "reasoning_as_thinking = false"))
    with pytest.raises(reload.ReloadError, match="entier"):
        reload.reload(path)
    assert (tools.MAX_CALLS, tool_loop.HOSTED_HARD_LIMIT, config.CONFIG) == before
    from llm_proxy.surfaces import anthropic_api
    assert anthropic_api.REASONING_AS_THINKING is True


def test_unreadable_toml_is_refused(edited):
    path = edited(("max_calls = 8\n", "max_calls = = 8\n"))
    with pytest.raises(reload.ReloadError, match="TOML invalide"):
        reload.reload(path)


def test_keys_reload_closes_an_open_proxy(proxy, edited, monkeypatch):
    from llm_proxy.access import auth
    assert proxy.client.get("/v1/tools").status_code == 200
    monkeypatch.setattr(config, "CONFIG_PATH", edited(
        ("api_keys = []\n", 'api_keys = ["sk-recharge"]\n')))
    r = proxy.client.post("/ui/config/reload", json={})
    assert r.status_code == 200 and r.json()["applied"] == ["proxy.api_keys"]
    assert auth.PROXY_API_KEYS == frozenset({"sk-recharge"})
    assert proxy.client.get("/v1/tools").status_code == 401
    assert proxy.client.get("/v1/tools", headers={
        "authorization": "Bearer sk-recharge"}).status_code == 200


def test_the_route_wants_json_and_reports_errors(proxy, edited, monkeypatch):
    assert proxy.client.post("/ui/config/reload").status_code == 415
    monkeypatch.setattr(config, "CONFIG_PATH", edited(("[proxy]\n", "[proxy\n")))
    r = proxy.client.post("/ui/config/reload", json={})
    assert r.status_code == 400 and "TOML" in r.json()["error"]["message"]


def test_sighup_reloads(edited, monkeypatch, caplog):
    from llm_proxy import startup, tools
    monkeypatch.setattr(config, "CONFIG_PATH",
                        edited(("max_calls = 8\n", "max_calls = 5\n")))
    startup.on_sighup()
    assert tools.MAX_CALLS == 5
    monkeypatch.setattr(config, "CONFIG_PATH", edited(("[proxy]\n", "[proxy\n"),
                                                      name="bad.toml"))
    startup.on_sighup()                       # refusé, journalisé, rien ne casse
    assert tools.MAX_CALLS == 5
    assert "rechargement refusé" in caplog.text
