"""L'écriture de config.toml (core/config_file.py) hors des routes : ce
qu'elle refuse, ce qu'elle protège (remise d'un texte, rechargement
concurrent), et la migration, une fois au démarrage, des anciens fichiers
à part (prompts.json, extensions.json). Les enregistrements de la page
sont vérifiés par leur route (test_config_view.py)."""

import json
import threading
import tomllib
from pathlib import Path

import pytest

from llm_proxy import prompts
from llm_proxy.core import atomic, config, config_file, reload


# ── écrire ────────────────────────────────────────────────────────────

def test_une_valeur_sous_une_valeur_qui_n_est_pas_une_table_est_refusee(config_copy):
    """Poser ui.chat.x quand ui.chat est un booléen : refusé, rien n'est
    écrasé."""
    before = config_copy.read_text(encoding="utf-8")
    with pytest.raises(config_file.Invalid):
        config_file.update({("ui", "chat", "x"): 1})
    assert config_copy.read_text(encoding="utf-8") == before


def test_remettre_un_texte_n_ecrase_pas_une_ecriture_venue_entre_temps(config_copy):
    """La remise du texte d'avant (un enregistrement que le rechargement a
    refusé) n'écrit que si le fichier est encore celui qu'on a écrit."""
    before = config_copy.read_text(encoding="utf-8")
    replaced, ours = config_file.update({("ui", "chat_max_rounds"): 7})
    assert replaced == before
    config_file.update({("ui", "chat_max_rounds"): 8})            # une autre page
    after = config_copy.read_text(encoding="utf-8")
    with pytest.raises(config_file.Stale):
        config_file.restore(replaced, expected=ours)
    assert config_copy.read_text(encoding="utf-8") == after
    _, current = config_file.read()
    config_file.restore(replaced, expected=current)
    assert config_copy.read_text(encoding="utf-8") == before


def test_le_rechargement_attend_une_ecriture_en_cours(config_copy, monkeypatch):
    """Une écriture de la page (fichier puis configuration en mémoire) et
    un rechargement ne se croisent pas : le rechargement attend."""
    real, started, go = atomic.write_text, threading.Event(), threading.Event()

    def slow(path, text):
        if path == config.CONFIG_PATH and not go.is_set():
            started.set()
            go.wait(5)
        real(path, text)

    monkeypatch.setattr(atomic, "write_text", slow)
    writer = threading.Thread(target=prompts.set_override, args=("compact.focus", "Focus: {focus}"))
    writer.start()
    assert started.wait(5)
    reloader = threading.Thread(target=reload.reload)
    reloader.start()
    reloader.join(0.3)
    waited = reloader.is_alive()
    go.set()
    writer.join(5)
    reloader.join(5)
    assert waited
    assert config.CONFIG["prompts"] == {"compact.focus": "Focus: {focus}"}


# ── la migration des anciens fichiers ─────────────────────────────────

def test_les_anciens_fichiers_sont_reportes_une_fois(config_copy):
    """prompts.json et extensions.json, à côté du fichier : reportés dans
    [prompts] et [extensions.<nom>].active, en vigueur tout de suite, puis
    renommés ; un second démarrage ne change rien. Un fichier illisible
    est laissé en place."""
    folder = Path(config.CONFIG_PATH).parent
    (folder / "prompts.json").write_text(json.dumps(
        {"web_fetch.description": "Migrated.", "pas_un_texte": 3}), encoding="utf-8")
    (folder / "extensions.json").write_text(json.dumps(
        {"horloge": True, "pull_request": ["pull_request"], "eteinte": False}), encoding="utf-8")
    assert config_file.migrate() == 3
    with open(config.CONFIG_PATH, "rb") as fh:
        doc = tomllib.load(fh)
    assert doc["prompts"] == {"web_fetch.description": "Migrated."}
    assert doc["extensions"] == {"horloge": {"active": True},
                                 "pull_request": {"active": ["pull_request"]}}
    assert prompts.text("web_fetch.description", search_hint="", ocr_hint="") == "Migrated."
    assert sorted(p.name.split(".migre-")[0] for p in folder.glob("*.migre-*")) == [
        "extensions.json", "prompts.json"]
    after = Path(config.CONFIG_PATH).read_text(encoding="utf-8")
    assert config_file.migrate() == 0
    assert Path(config.CONFIG_PATH).read_text(encoding="utf-8") == after

    (folder / "prompts.json").write_text("{abîmé", encoding="utf-8")
    assert config_file.migrate() == 0
    assert (folder / "prompts.json").read_text(encoding="utf-8") == "{abîmé"
