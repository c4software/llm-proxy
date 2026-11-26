"""Configuration → Proxy (config_view.py) : la configuration effective —
fichier et défauts —, secrets masqués, vérifiée ; puis ENREGISTRÉE : le
fichier mis à jour valeur par valeur (core/config_file.py), commentaires,
«${VAR}» et secrets gardés, et rechargé."""

import difflib
import logging
import os
import tomllib
from pathlib import Path

import pytest

from fakes import admin_and_user, connect, everywhere
from llm_proxy.core import config, config_file, reload
from llm_proxy.tools import webcache
from llm_proxy.ui import dashboard
from llm_proxy.ui import config_view as V

# Ce qu'un admin a écrit à la main, ajouté à la configuration en vigueur :
# un commentaire, un secret en clair, un en-tête secret en ligne.
HAND = """
[backends.maison]
# la clé en clair, à ne jamais perdre
url = "http://maison:8080"
api_key = "sk-maison-en-clair"
timeout = 30
headers = { "X-Jeton" = "jeton-en-clair", "X-Equipe" = "visible" }
"""


@pytest.fixture
def written(idp):
    """Un admin OIDC connecté (seul à pouvoir écrire config.toml), la
    configuration en vigueur (sa copie, conftest) complétée à la main :
    HAND et un commentaire de tête. Rend le client et le fichier."""
    path = Path(config.CONFIG_PATH)
    path.write_text("# Mon fichier, commenté à la main\n" + path.read_text(encoding="utf-8") + HAND,
                    encoding="utf-8")
    config.CONFIG = config.load(str(path))          # le proxy a démarré dessus
    alice = connect(idp)
    yield alice, path
    # Ce qu'un enregistrement a rechargé à chaud (des constantes de module)
    # revient à la configuration d'exemple pour le test suivant.
    reload.reload(config.EXAMPLE_PATH)


def masked(value) -> bool:
    """Une valeur montrée masquée (son condensé, pas elle)."""
    return isinstance(value, str) and bool(V.MASK_RE.fullmatch(value))


def page(client) -> tuple[dict, str]:
    """Ce que la page lit : l'arbre effectif (masqué) et l'empreinte."""
    r = client.get("/ui/config").json()
    return tomllib.loads(r["toml"]), r["base"]


def save(client, tree: dict, base: str):
    """Ce que la page enregistre : le fichier complet qu'elle affiche."""
    return client.post("/ui/config/save", json={"toml": V.dumps(tree, set()), "base": base})


def test_the_effective_configuration(conf):
    r = conf.client.get("/ui/config").json()
    tree = tomllib.loads(r["toml"])
    # Le fichier tel qu'il est écrit : les renvois à l'environnement restent.
    assert tree["proxy"]["log_level"] == "DEBUG"
    assert tree["backends"]["albert"]["api_key"] == "${ALBERT_API_KEY}"
    # Les secrets en clair, masqués — où qu'ils soient.
    assert masked(tree["proxy"]["api_keys"][0]) and tree["proxy"]["api_keys"][1:] == ["${PROXY_API_KEY}"]
    assert masked(tree["backends"]["local"]["api_key"])
    assert masked(tree["tools"]["mcp"]["notes"]["headers"]["Authorization"])
    assert masked(tree["backends"]["local"]["headers"]["X-Equipe"])
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
        "toml": f'[proxy]\napi_keys = ["{config_file.mask("x")}"]\n'}).json()
    assert masked["masked"] == 1
    assert conf.client.post("/ui/config/check", json={}).status_code == 400
    assert (tmp_path / "config.toml").read_text() == before


def test_the_page_needs_its_session(ui):
    assert ui.client.get("/ui/config").status_code == 401


def test_the_chat_model_is_preselected(conf, monkeypatch):
    assert conf.client.get("/ui/session").json()["chat"]["model"] is None
    everywhere(monkeypatch, dashboard, "UI_CHAT_MODEL", "essai/qwen")
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
    assert masked(tree["proxy"]["api_keys"][0]) and tree["proxy"]["api_keys"][1:] == ["${PROXY_API_KEY}"]
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
    une qu'il règle et omet (api_key, case décochée ; headers, champ vidé)
    disparaît ; ses en-têtes écrits remplacent les anciens."""
    r = conf.client.post("/ui/config/merge", json={"toml": """
[backends.albert]
url = "https://albert.example"
[backends.local]
url = "http://local:8080"
"""}).json()
    tree = tomllib.loads(r["toml"])
    assert "api_key" not in tree["backends"]["albert"]
    assert tree["backends"]["local"] == {"url": "http://local:8080", "timeout": 30}
    r = conf.client.post("/ui/config/merge", json={"toml": """
[backends.local]
url = "http://local:8080"
[backends.local.headers]
"X-Autre" = "${LOCAL_HEADER_X_AUTRE}"
"""}).json()
    assert tomllib.loads(r["toml"])["backends"]["local"]["headers"] == {
        "X-Autre": "${LOCAL_HEADER_X_AUTRE}"}


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


def test_the_protocol_goes_back_to_openai(conf, tmp_path, monkeypatch):
    """Un backend repassé d'Anthropic à OpenAI dans le formulaire (qui
    n'écrit `api` que pour Anthropic) perd son `api = "anthropic"`."""
    path = tmp_path / "anthropic.toml"
    path.write_text('[backends.claude]\nurl = "https://api.anthropic.com"\napi = "anthropic"\n')
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))
    r = conf.client.post("/ui/config/merge", json={"toml": """
[backends.claude]
url = "https://api.anthropic.com"
"""}).json()
    assert "api" not in tomllib.loads(r["toml"])["backends"]["claude"]


# ── enregistrer ───────────────────────────────────────────────────────

def test_enregistrer_met_a_jour_le_fichier_valeur_par_valeur(written):
    """Seul ce qui change est écrit : commentaires, «${VAR}», secrets en
    clair (masqués dans la page) et défauts affichés restent tels quels ;
    une valeur retirée disparaît. Puis le rechargement : un réglage
    rechargeable s'applique, les autres attendent un redémarrage. L'ancien
    fichier est gardé dans config-history/."""
    client, path = written
    before = path.read_text(encoding="utf-8")
    tree, base = page(client)
    assert masked(tree["backends"]["maison"]["api_key"])
    tree["ui"]["chat_max_rounds"] = 7                    # rechargeable
    tree["backends"]["maison"]["timeout"] = 60           # au redémarrage
    del tree["backends"]["maison"]["headers"]["X-Equipe"]
    r = save(client, tree, base)
    assert r.status_code == 200, r.text
    assert r.json()["applied"] == ["ui.chat_max_rounds"]
    assert r.json()["restart"] == ["backends.maison.headers.X-Equipe", "backends.maison.timeout"]
    assert client.get("/ui/session").json()["chat"]["max_rounds"] == 7

    after = path.read_text(encoding="utf-8")
    # Les valeurs : celles posées, les secrets en clair gardés.
    maison = tomllib.loads(after)["backends"]["maison"]
    assert (maison["timeout"], maison["api_key"], maison["headers"]) == (
        60, "sk-maison-en-clair", {"X-Jeton": "jeton-en-clair"})
    assert tomllib.loads(after)["ui"]["chat_max_rounds"] == 7
    # Les lignes : seules celles des trois valeurs changent ; commentaires
    # et «${VAR}» restent mot pour mot.
    changed = [line[2:] for line in difflib.ndiff(before.splitlines(), after.splitlines())
               if line[:2] in ("- ", "+ ")]
    assert {line.split("=")[0].strip() for line in changed} == {
        "chat_max_rounds", "timeout", "headers"}, changed
    kept = [line for line in before.splitlines() if line.lstrip().startswith("#") or "${" in line]
    assert kept and all(line in after.splitlines() for line in kept)
    assert "masqué" not in after and after.count("# défaut") == before.count("# défaut")
    history = Path(path.parent, config_file.HISTORY)
    assert [p.read_text(encoding="utf-8") for p in history.iterdir()] == [before]
    # Rien ne change : rien n'est écrit, aucune copie de plus.
    tree, base = page(client)
    assert save(client, tree, base).json()["changed"] == 0
    assert path.read_text(encoding="utf-8") == after and len(os.listdir(history)) == 1


def test_valider_un_enregistrement_ne_touche_a_rien(written, caplog):
    """L'évaluation à blanc d'un enregistrement ne lit que la
    configuration : elle ne construit rien (aucun cache de plus à purger)
    et n'écrit rien au journal."""
    client, _ = written
    count = len(webcache.CACHES)
    for n in (11, 12):
        tree, base = page(client)
        tree["ui"]["chat_max_rounds"] = n
        caplog.clear()
        assert save(client, tree, base).status_code == 200
        assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert len(webcache.CACHES) == count


def test_la_remise_n_ecrase_pas_une_ecriture_venue_entre_temps(written, monkeypatch):
    """Un enregistrement que le rechargement refuse : l'ancien fichier
    n'est remis que si personne n'a écrit entre-temps ; sinon le fichier
    reste tel quel, et la réponse le dit."""
    client, path = written
    tree, base = page(client)
    tree["ui"]["chat_max_rounds"] = 9
    meanwhile = []

    def refused(*a, **k):
        # Entre l'écriture et le rechargement, une autre écriture arrive.
        config_file.update({("ui", "chat_model"): "albert/autre"})
        meanwhile.append(path.read_text(encoding="utf-8"))
        raise reload.ReloadError("valeur refusée")

    with monkeypatch.context() as m:
        m.setattr(reload, "reload", refused)
        r = save(client, tree, base)
    assert (r.status_code, r.json()["error"]["type"]) == (400, "invalid_config")
    assert "laissé tel quel" in r.json()["error"]["message"]
    assert path.read_text(encoding="utf-8") == meanwhile[0]


def test_un_enregistrement_refuse_laisse_le_fichier_tel_quel(written):
    """TOML illisible, clé inconnue, valeur que le rechargement refuse
    (le fichier est remis), fichier modifié depuis la lecture (deux
    enregistrements croisés)."""
    client, path = written
    tree, base = page(client)
    before = path.read_text(encoding="utf-8")
    zero = {**tree, "ui": {**tree["ui"], "chat_max_rounds": 0}}
    typo = {**tree, "ui": {**tree["ui"], "chat_max_round": 9}}
    cases = {
        "TOML illisible": (client.post("/ui/config/save", json={"toml": "[ui", "base": base}),
                           400, "invalid_config"),
        "clé inconnue": (save(client, typo, base), 400, "invalid_config"),
        "refusée au rechargement": (save(client, zero, base), 400, "invalid_config"),
        "sans base": (client.post("/ui/config/save", json={"toml": ""}), 400,
                      "invalid_request_error"),
    }
    for case, (r, status, kind) in cases.items():
        assert (r.status_code, r.json()["error"]["type"]) == (status, kind), case
        assert path.read_text(encoding="utf-8") == before, case
    assert client.get("/ui/session").json()["chat"]["max_rounds"] == 50
    # Deux pages ouvertes sur la même version : la seconde est refusée.
    first = {**tree, "ui": {**tree["ui"], "chat_max_rounds": 11}}
    assert save(client, first, base).status_code == 200
    written_once = path.read_text(encoding="utf-8")
    second = {**tree, "ui": {**tree["ui"], "chat_max_rounds": 12}}
    r = save(client, second, base)
    assert r.status_code == 409 and r.json()["error"]["type"] == "conflict"
    assert path.read_text(encoding="utf-8") == written_once


def writers(client, base: str) -> dict:
    """Les trois écrivains de config.toml, appelés une fois chacun."""
    return {"config": client.post("/ui/config/save", json={"toml": "", "base": base}),
            "prompt": client.put("/ui/prompts/web_fetch.description", json={"text": "x"}),
            "reset": client.delete("/ui/prompts/web_fetch.description"),
            "extension": client.post("/ui/extensions/horloge", json={"enabled": True})}


def test_seul_un_admin_oidc_ecrit_config_toml(idp):
    """Avec les comptes, un utilisateur ne lit ni n'écrit la configuration.
    L'admin lit, et la session lui dit qu'il peut écrire."""
    alice, bob, _ = admin_and_user(idp)
    path = Path(config.CONFIG_PATH)
    before = path.read_text(encoding="utf-8")
    base = alice.get("/ui/config").json()["base"]
    assert alice.get("/ui/config").json()["writable"] is True
    assert bob.get("/ui/config").status_code == 403
    for case, r in writers(bob, base).items():
        assert r.status_code == 403, case
    assert path.read_text(encoding="utf-8") == before


def test_sans_comptes_la_page_n_ecrit_pas_config_toml(conf):
    """Sans OIDC, la page connectée lit la configuration mais ne l'écrit
    pas : les trois écrivains répondent 403 (le fichier se modifie à la
    main), et la configuration lue le dit (`writable`)."""
    path = Path(config.CONFIG_PATH)
    before = path.read_text(encoding="utf-8")
    got = conf.client.get("/ui/config").json()
    assert got["writable"] is False
    for case, r in writers(conf.client, got["base"]).items():
        assert (r.status_code, r.json()["error"]["type"]) == (403, "forbidden"), case
        assert "à la main" in r.json()["error"]["message"], case
    assert path.read_text(encoding="utf-8") == before


def test_l_historique_garde_les_dernieres_versions(written, monkeypatch):
    """config-history/ garde les KEEP dernières versions remplacées, dans
    l'ordre où elles l'ont été — même enregistrées dans la même seconde
    (le dixième suffixe passe après le deuxième)."""
    client, path = written
    monkeypatch.setattr(config_file.time, "strftime", lambda fmt, *a: "20261126-120000")
    monkeypatch.setattr(config_file, "KEEP", 2)
    for n in range(11, 24):
        tree, base = page(client)
        tree["ui"]["chat_max_rounds"] = n
        assert save(client, tree, base).status_code == 200
    history = Path(path.parent, config_file.HISTORY)
    kept = sorted(tomllib.loads(p.read_text(encoding="utf-8"))["ui"]["chat_max_rounds"]
                  for p in history.iterdir())
    assert kept == [21, 22]


def test_un_secret_masque_est_retrouve_par_sa_valeur(written):
    """Le masque d'un secret désigne SA valeur (un condensé, sans la
    révéler) : retirer, réordonner ou déplacer une valeur masquée garde le
    bon secret ; un masque qui ne correspond à aucune valeur du fichier
    est refusé, et aucun masque n'est jamais écrit."""
    client, path = written
    text = path.read_text(encoding="utf-8").replace(
        'api_key = "sk-maison-en-clair"', 'api_key = "sk-maison-en-clair"\napi_keys = ["sk-a", "sk-b", "sk-c"]')
    path.write_text(text, encoding="utf-8")
    config.CONFIG = config.load(str(path))
    tree, base = page(client)
    masks = tree["backends"]["maison"]["api_keys"]
    assert len(set(masks)) == 3 and not any("sk-" in m for m in masks)
    tree["backends"]["maison"]["api_keys"] = [masks[2], masks[0]]          # b retiré, ordre changé
    tree["backends"]["ailleurs"] = {"url": "http://x", "api_key": tree["backends"]["maison"]["api_key"]}
    r = client.post("/ui/config/save", json={"toml": V.dumps(tree, set()), "base": base})
    assert r.status_code == 200, r.text
    after = tomllib.loads(path.read_text(encoding="utf-8"))["backends"]
    assert after["maison"]["api_keys"] == ["sk-c", "sk-a"]
    assert after["ailleurs"]["api_key"] == "sk-maison-en-clair"
    before = path.read_text(encoding="utf-8")
    tree, base = page(client)
    for case, value in {"masque inconnu": V.MASK_FORM.format("0000000000"),
                        "masque dans une liste neuve": [V.MASK_FORM.format("ffffffffff")]}.items():
        tree["backends"]["maison"]["api_key"] = value
        r = client.post("/ui/config/save", json={"toml": V.dumps(tree, set()), "base": base})
        assert (r.status_code, r.json()["error"]["type"]) == (400, "invalid_config"), case
        assert path.read_text(encoding="utf-8") == before, case
    assert "masqué" not in before


def test_une_valeur_invalide_hors_du_rechargement_est_refusee(written):
    """Une valeur que le proxy refuserait au démarrage (un entier qui n'en
    est pas un) dans un réglage qui n'est pas rechargeable à chaud : refusée
    avant d'être écrite — le proxy redémarrerait sinon sur un fichier qu'il
    ne lit pas."""
    client, path = written
    before = path.read_text(encoding="utf-8")
    tree, base = page(client)
    tree["tools"]["web_grep"]["max_matches"] = "beaucoup"
    r = save(client, tree, base)
    assert (r.status_code, r.json()["error"]["type"]) == (400, "invalid_config")
    assert path.read_text(encoding="utf-8") == before


def test_sans_fichier_ou_illisible_un_refus_clair(written):
    """Plus de config.toml (supprimé pendant que la page était ouverte) :
    409 ; un fichier que tomlkit ne lit pas : 400 — jamais une erreur 500."""
    client, path = written
    tree, base = page(client)
    path.write_text("[ui\n", encoding="utf-8")
    r = save(client, tree, base)
    assert r.status_code in (400, 409) and r.json()["error"]["type"] in ("invalid_config", "conflict")
    path.unlink()
    r = save(client, tree, base)
    assert (r.status_code, r.json()["error"]["type"]) == (409, "conflict")
    r = client.put("/ui/prompts/web_fetch.description", json={"text": "x"})
    assert (r.status_code, r.json()["error"]["type"]) == (409, "conflict")
