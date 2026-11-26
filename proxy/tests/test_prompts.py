"""Les prompts (llm_proxy/prompts/, ui/prompt_routes.py) : le catalogue des
textes que le modèle lit, et les textes que l'admin modifie dans
Configuration → Prompts — effectifs à la requête suivante, refusés s'ils
utilisent une variable que le code ne fournit pas, jamais écrits par un
utilisateur."""

import re
import threading
from pathlib import Path

from fakes import admin_and_user
from llm_proxy import prompts, tools
from llm_proxy.storage import prompt_overrides
from llm_proxy.tools.contract import NATIVE

SRC = Path(prompts.__file__).resolve().parents[1]
DEFAULT = prompts.CATALOG["web_fetch.description"][1]


def fetch_description(present=("web_fetch", "web_search")) -> str:
    tool = next(t for t in tools.REGISTRY if t.name == "web_fetch")
    return tool.spec(frozenset(present))["function"]["description"]


# ── l'accès ───────────────────────────────────────────────────────────

def test_un_utilisateur_ne_voit_ni_ne_modifie_les_prompts(idp):
    alice, bob, _ = admin_and_user(idp)
    assert bob.get("/ui/prompts").status_code == 403
    assert bob.put("/ui/prompts/web_fetch.description", json={"text": "x"}).status_code == 403
    assert bob.delete("/ui/prompts/web_fetch.description").status_code == 403
    assert prompt_overrides.load() == {} and fetch_description().startswith(DEFAULT[:40])
    listing = alice.get("/ui/prompts").json()
    assert listing["max_chars"] == prompts.MAX_CHARS
    entry = next(p for p in listing["prompts"] if p["key"] == "web_fetch.description")
    assert entry == {"key": "web_fetch.description", "domain": "tools", "default": DEFAULT,
                     "override": None, "variables": ["search_hint", "ocr_hint"]}


# ── modifier, rétablir ────────────────────────────────────────────────

def test_un_prompt_modifie_change_ce_que_le_modele_lit_puis_se_retablit(idp):
    """Le texte modifié remplace le défaut dès l'appel suivant, ses
    variables toujours remplies par le code ; il survit dans prompts.json ;
    Rétablir rend le défaut."""
    alice, _, _ = admin_and_user(idp)
    before = fetch_description()
    r = alice.put("/ui/prompts/web_fetch.description", json={"text": "Read a page. {search_hint}"})
    assert r.status_code == 200 and r.json()["override"] == "Read a page. {search_hint}"
    assert fetch_description() == "Read a page. Use it to read a page found with web_search. "
    assert fetch_description(("web_fetch",)) == "Read a page. "
    assert prompt_overrides.load() == {"web_fetch.description": "Read a page. {search_hint}"}

    r = alice.delete("/ui/prompts/web_fetch.description")
    assert r.status_code == 200 and r.json()["override"] is None
    assert fetch_description() == before and prompt_overrides.load() == {}
    # Un texte égal au défaut n'est pas gardé comme modification.
    alice.put("/ui/prompts/web_fetch.description", json={"text": DEFAULT})
    assert prompt_overrides.load() == {}


def test_un_prompt_modifie_d_un_outil_du_navigateur_part_avec_la_session(idp):
    """La description d'un outil du navigateur (ses variables, les
    `settings`, toujours remplies) et la consigne de sa famille : la page les
    reçoit modifiées avec /ui/session, puis rétablies."""
    alice, _, _ = admin_and_user(idp)

    def page():
        got = alice.get("/ui/session").json()["chat"]["page"]
        tool = next(t for t in got["tools"] if t["name"] == "run_sql")
        family = next(f for f in got["families"] if f["key"] == "sql")
        return tool["spec"]["function"]["description"], family["guidance"]

    before = page()
    assert alice.put("/ui/prompts/run_sql.description",
                     json={"text": "Run SQL, {max_rows} rows at most."}).status_code == 200
    assert alice.put("/ui/prompts/family.sql.guidance", json={"text": "Prefer run_sql."}).status_code == 200
    assert page() == ("Run SQL, 200 rows at most.", "Prefer run_sql.")
    for key in ("run_sql.description", "family.sql.guidance"):
        assert alice.delete(f"/ui/prompts/{key}").status_code == 200
    assert page() == before


def test_un_prompt_refuse_ne_change_rien(idp):
    alice, _, _ = admin_and_user(idp)
    cases = {
        "variable que le code ne fournit pas": ({"text": "{secret}"}, 400, "invalid_prompt"),
        "accolade seule": ({"text": "a { b"}, 400, "invalid_prompt"),
        "attribut": ({"text": "{search_hint.__class__}"}, 400, "invalid_prompt"),
        "format": ({"text": "{search_hint!r}"}, 400, "invalid_prompt"),
        "trop long": ({"text": "x" * (prompts.MAX_CHARS + 1)}, 400, "invalid_prompt"),
        "vide": ({"text": "  \n "}, 400, "invalid_prompt"),
        "pas un texte": ({"text": 3}, 400, "invalid_request_error"),
    }
    for case, (body, status, kind) in cases.items():
        r = alice.put("/ui/prompts/web_fetch.description", json=body)
        assert (r.status_code, r.json()["error"]["type"]) == (status, kind), case
    for r in (alice.put("/ui/prompts/inconnu.description", json={"text": "x"}),
              alice.delete("/ui/prompts/inconnu.description")):
        assert r.status_code == 404 and r.json()["error"]["type"] == "not_found"
    # Des accolades doublées écrivent une accolade : permis.
    assert alice.put("/ui/prompts/web_fetch.description", json={"text": "{{x}}"}).status_code == 200
    assert fetch_description() == "{x}"
    assert prompt_overrides.load() == {"web_fetch.description": "{{x}}"}


def test_un_fichier_illisible_laisse_les_defauts_et_n_est_pas_ecrase(idp):
    alice, _, _ = admin_and_user(idp)
    before = fetch_description()
    Path(prompt_overrides.PATH).write_text("{pas du json", encoding="utf-8")
    assert fetch_description() == before
    assert alice.put("/ui/prompts/web_fetch.description", json={"text": "x"}).status_code == 409
    assert alice.delete("/ui/prompts/web_fetch.description").status_code == 409
    assert Path(prompt_overrides.PATH).read_text(encoding="utf-8") == "{pas du json"


def test_un_fichier_change_hors_de_la_page_s_applique_sans_redemarrer(idp):
    """Une restauration de sauvegarde, une autre instance : prompts.json
    réécrit après une première lecture est relu. Une modification devenue
    invalide (une mise à jour a retiré la variable du défaut, ou la clé)
    est ignorée, pas une erreur à l'appel."""
    before = fetch_description()
    prompt_overrides.save({"web_fetch.description": "Restored."})
    assert fetch_description() == "Restored."
    prompt_overrides.save({"web_fetch.description": "{disparue}", "inconnu.cle": "x"})
    assert fetch_description() == before


def test_deux_modifications_simultanees_sont_gardees_toutes_les_deux(idp, monkeypatch):
    """Deux PUT sur deux clés à la fois (les routes écrivent depuis des
    fils) : aucune ne perd l'autre. L'écriture de la première est retenue
    le temps que la seconde arrive."""
    alice, _, _ = admin_and_user(idp)
    real_save, started, go = prompt_overrides.save, threading.Event(), threading.Event()

    def slow_save(doc, path=None):
        if "web_fetch.param.url" in doc and not go.is_set():
            started.set()
            go.wait(5)
        real_save(doc, path)

    monkeypatch.setattr(prompt_overrides, "save", slow_save)
    first = threading.Thread(target=lambda: alice.put("/ui/prompts/web_fetch.param.url", json={"text": "A"}))
    first.start()
    assert started.wait(5)
    second = threading.Thread(target=lambda: alice.put("/ui/prompts/web_fetch.param.offset", json={"text": "B"}))
    second.start()
    second.join(0.3)              # bloquée par la première, si le verrou tient
    go.set()
    first.join(5)
    second.join(5)
    assert prompt_overrides.load() == {"web_fetch.param.url": "A", "web_fetch.param.offset": "B"}


# ── le catalogue et le code ───────────────────────────────────────────

def test_chaque_cle_nommee_dans_le_code_est_au_catalogue_et_inversement():
    """Les clés écrites dans le code, et celles qu'il DÉRIVE : la description
    de chaque outil natif du navigateur (`<nom>.description`, exigée), la
    consigne d'une famille native (`family.<clé>.guidance`, si elle en a
    une)."""
    named = set()
    for path in SRC.rglob("*.py"):
        named |= set(re.findall(r'prompts\.(?:text|ref|template)\(\s*"([^"]+)"', path.read_text("utf-8")))
    assert named, "aucune clé trouvée : le motif ne correspond plus au code"
    named |= {f"{t.name}.description" for t in tools.PAGE if t.name in NATIVE}
    families = {f"family.{f.key}.guidance" for f in tools.FAMILIES}
    assert named - set(prompts.CATALOG) == set()
    assert set(prompts.CATALOG) - named - families == set()
