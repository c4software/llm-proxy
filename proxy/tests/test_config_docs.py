"""Le rôle de chaque réglage (config_docs.py), lu dans les commentaires de
data/config.example.toml et montré par Configuration → Proxy, Avancé."""

from llm_proxy import app  # noqa: F401 — charge chaque module : config.READ complet
from llm_proxy.core import config, config_docs as D

import test_config_view
from test_config_view import conf, ui  # noqa: F401 — les fixtures


def test_chaque_reglage_lu_par_le_proxy_a_une_description():
    """Un réglage ajouté sans commentaire dans config.example.toml
    arriverait muet dans la page : refusé ici."""
    missing = [p for p, d in sorted(config.READ.items())
               if not isinstance(d, dict) and not D.describe(p)]
    assert missing == [], ("sans description dans data/config.example.toml : "
                           + ", ".join(missing))


def test_regles_de_lecture():
    docs = D.parse("""\
# ── Section ──
# La table entière.
[t]
enabled = true
# Bornes : a et b.
a = 1
b = 2  # et b, en fin de ligne
# Un exemple commenté ne coupe pas le bloc :
# c = ["x"]
c = []
d = [
  "z",
]
e = 3

# Champs (un par ligne) :
#   url — l'adresse ;
#   tool_choice — absent ou
#     false = rien ;
[backends.un]
url = "http://x"
""".replace("[backends.un]", "# Le premier backend.\n[backends.un]"))
    assert docs["t"] == "La table entière."
    assert docs["t.enabled"] == "La table entière."
    assert docs["t.a"] == "Bornes : a et b."
    assert docs["t.b"] == "Bornes : a et b. et b, en fin de ligne"
    assert docs["t.c"] == "Un exemple commenté ne coupe pas le bloc"
    # Sans ligne vide ni commentaire à elle, la clé suivante reprend le bloc,
    # même après une valeur sur plusieurs lignes.
    assert docs["t.e"] == docs["t.c"]
    assert docs["backends.*.url"] == "l'adresse"
    assert docs["backends.*.tool_choice"] == "absent ou false = rien"
    # Le commentaire d'UNE instance décrit la table, pas ses champs.
    assert docs["backends"] == "Le premier backend."


def test_noms_libres_et_alias():
    assert D.describe("backends.nimporte.url").startswith("l'adresse du backend")
    assert D.describe("backends.nimporte.api").startswith("son protocole")
    assert D.describe("tools.mcp.monserveur.url").startswith("Adresse de configuration")
    assert D.describe("tools.mcp.timeout").startswith("Délai")
    assert D.describe("tools.web_fetch.allow_private").startswith(
        "Ancien emplacement, lu à défaut de [tools.net].allow_private")
    assert "Relais réseau de la VM Linux" in D.describe("ui.linux_relay")


def test_la_page_recoit_les_descriptions(conf):
    docs = conf.client.get("/ui/config").json()["docs"]
    assert docs["backends.local.timeout"].startswith("délai (s) d'une requête")
    assert docs["tools.mcp.notes.url"].startswith("Adresse de configuration")
    assert "ui.linux_relay" in docs


def test_les_renvois_a_la_documentation_existent():
    """« docs/linux.md, « Le réseau » » devient dans la page un lien vers
    GitHub, à l'ancre de la section : le fichier et le titre doivent
    exister."""
    import os
    import re
    root = os.path.dirname(os.path.dirname(config.EXAMPLE_PATH))
    with open(config.EXAMPLE_PATH, encoding="utf-8") as fh:
        text = fh.read()
    refs = re.findall(r"((?:docs|bench)/[\w.-]+\.md|README\.md)`?"
                      r"(?:,\s*«\s*([^»]+?)\s*»)?", text)
    assert refs
    broken = []
    for name, section in refs:
        path = os.path.join(root, name)
        if not os.path.exists(path):
            broken.append(name)
            continue
        with open(path, encoding="utf-8") as fh:
            titles = {re.sub(r"^#+\s*", "", line).strip() for line in fh if line.startswith("#")}
        if section and section not in titles:
            broken.append(f"{name}, « {section} »")
    assert broken == []
