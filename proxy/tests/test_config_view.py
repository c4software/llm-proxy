"""Configuration → Paramètres (config_view.py) : la configuration
effective — fichier et défauts —, secrets masqués, vérifiée sans jamais
rien écrire."""

import tomllib

import pytest

from llm_proxy import config, ui_routes
from llm_proxy import config_view as V
import test_ui
from test_ui import login


TOML = """\
[proxy]
api_keys = ["sk-en-clair", "${PROXY_API_KEY}"]
log_level = "DEBUG"

[backends.albert]
url = "https://albert.example"
api_key = "${ALBERT_API_KEY}"

[backends.local]
url = "http://local:8080"
api_key = "sk-local-en-clair"
timeout = 30

[tools.mcp.notes]
url = "http://notes/mcp"
headers = { Authorization = "Bearer jeton-en-clair" }
"""


ui = test_ui.ui                    # la fixture de test_ui


@pytest.fixture
def conf(ui, tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(TOML)
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))
    login(ui)
    return ui


def test_the_effective_configuration(conf):
    r = conf.client.get("/ui/config").json()
    tree = tomllib.loads(r["toml"])
    # Le fichier tel qu'il est écrit : les renvois à l'environnement restent.
    assert tree["proxy"]["log_level"] == "DEBUG"
    assert tree["backends"]["albert"]["api_key"] == "${ALBERT_API_KEY}"
    # Les secrets en clair, masqués — où qu'ils soient.
    assert tree["proxy"]["api_keys"] == [V.MASK, "${PROXY_API_KEY}"]
    assert tree["backends"]["local"]["api_key"] == V.MASK
    assert tree["tools"]["mcp"]["notes"]["headers"]["Authorization"] == V.MASK
    assert "en-clair" not in r["toml"]
    # Les défauts : ce que le proxy lit sans que le fichier le dise.
    assert r["defaults"] > 0
    assert tree["fair_share"]["tokens_per_hour"] == 0
    assert "tokens_per_hour = 0  # défaut" in r["toml"]
    assert "log_level = \"DEBUG\"\n" in r["toml"]


def test_a_modified_file_is_checked_never_written(conf, tmp_path):
    before = (tmp_path / "config.toml").read_text()
    ok = conf.client.post("/ui/config/check", json={
        "toml": "[fair_share]\ntokens_per_hour = 1000\n"
                "[proxy]\nlog_levle = \"INFO\"\n"
                "[backends.neuf]\nurl = \"http://x\"\nquoi = 1\n"}).json()
    # Une faute de frappe est signalée ; sous [backends.*], tout est admis.
    assert ok["ok"] and ok["unknown"] == ["proxy.log_levle"]
    bad = conf.client.post("/ui/config/check", json={"toml": "[proxy\n"}).json()
    assert not bad["ok"] and bad["error"]
    masked = conf.client.post("/ui/config/check", json={
        "toml": f'[proxy]\napi_keys = ["{V.MASK}"]\n'}).json()
    assert masked["masked"] == 1
    assert conf.client.post("/ui/config/check", json={}).status_code == 400
    assert (tmp_path / "config.toml").read_text() == before


def test_the_page_needs_its_session(ui):
    assert ui.client.get("/ui/config").status_code == 401


def test_the_chat_model_is_preselected(conf, monkeypatch):
    assert conf.client.get("/ui/session").json()["chat"]["model"] is None
    monkeypatch.setattr(ui_routes, "UI_CHAT_MODEL", "essai/qwen")
    assert conf.client.get("/ui/session").json()["chat"]["model"] == "essai/qwen"


def test_the_form_applies_onto_the_whole_file(conf, tmp_path):
    """POST /ui/config/merge : le fichier partiel du formulaire sur la
    configuration en vigueur — ce qu'il écrit remplace, ce qu'il omet de
    ce qu'il règle disparaît, le reste est gardé ; rien n'est écrit."""
    before = (tmp_path / "config.toml").read_text()
    values = conf.client.get("/ui/config").json()["values"]
    assert values["backends"]["local"]["url"] == "http://local:8080"
    r = conf.client.post("/ui/config/merge", json={"toml": """
[proxy]
log_level = "INFO"
[fair_share]
tokens_per_hour = 5000
[backends.albert]
url = "https://albert.example"
api_key = "${ALBERT_API_KEY}"
fair_share = false
"""}).json()
    tree = tomllib.loads(r["toml"])
    assert tree["proxy"]["log_level"] == "INFO"
    # Hors du formulaire : gardé tel quel (masqué s'il est secret).
    assert tree["proxy"]["api_keys"] == [V.MASK, "${PROXY_API_KEY}"]
    assert tree["tools"]["mcp"]["notes"]["url"] == "http://notes/mcp"
    # Un backend absent du formulaire est retiré ; une valeur posée n'est
    # plus un défaut.
    assert list(tree["backends"]) == ["albert"]
    assert tree["backends"]["albert"]["fair_share"] is False
    assert "tokens_per_hour = 5000\n" in r["toml"]
    # [oidc] omis par le formulaire, mais rien que des défauts : gardé, inactif.
    assert tree["oidc"]["issuer"] == "" and "issuer = \"\"  # défaut" in r["toml"]
    bad = conf.client.post("/ui/config/merge", json={"toml": "[proxy"})
    assert bad.status_code == 400
    assert (tmp_path / "config.toml").read_text() == before


def test_a_backend_keeps_what_the_form_does_not_set(conf):
    """Une clé de backend que le formulaire ne règle pas (timeout) reste ;
    une qu'il règle et omet (api_key, case décochée) disparaît."""
    r = conf.client.post("/ui/config/merge", json={"toml": """
[backends.albert]
url = "https://albert.example"
[backends.local]
url = "http://local:8080"
"""}).json()
    tree = tomllib.loads(r["toml"])
    assert "api_key" not in tree["backends"]["albert"]
    assert tree["backends"]["local"] == {"url": "http://local:8080", "timeout": 30}


def test_the_advanced_section_sets_any_setting(conf):
    """Section « Avancé » : tout réglage hors du formulaire, posé par son
    chemin — un nom de table peut contenir un point."""
    values = conf.client.get("/ui/config").json()
    assert "fair_share.tokens_per_hour" in values["default_paths"]
    r = conf.client.post("/ui/config/merge", json={
        "toml": '[backends.local]\nurl = "http://local:8080"\n',
        "overrides": [{"path": ["backends", "local", "timeout"], "value": 90},
                      {"path": ["anthropic", "model_map", "claude-3.5"], "value": "local/m"},
                      {"path": ["stats", "retention_days"], "value": 30}]}).json()
    tree = tomllib.loads(r["toml"])
    assert tree["backends"]["local"]["timeout"] == 90
    assert tree["anthropic"]["model_map"]["claude-3.5"] == "local/m"
    assert "retention_days = 30\n" in r["toml"]
    assert conf.client.post("/ui/config/merge", json={
        "toml": "", "overrides": [{"path": "stats.x", "value": 1}]}).status_code == 400
