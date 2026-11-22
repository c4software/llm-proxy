"""Les extensions (llm_proxy/extension.py, tools/extensions.py) : des
outils au contrat de tous les outils, un sous-dossier chacune
(<nom>/<nom>.py, et <nom>.js pour ceux que le navigateur exécute), chargés
d'un dossier hors d'atteinte du proxy ; celles qui sont en faute écartées
entières sans arrêter les autres ; l'interrupteur de la page ; les
exemples d'examples/extensions servis là où ils sont offerts. Chaque test
charge dans une copie du registre (tools.REGISTRY, PAGE, FAMILIES
remplacés). Le montage en lecture seule est simulé à sa frontière,
os.statvfs."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from fakes import admin_and_user, bearer, connect
from llm_proxy import extension, tools
from llm_proxy.core import config
from llm_proxy.storage import extension_state
from llm_proxy.tools import extensions
from llm_proxy.ui import page_tools

# Le vrai annuaire : la fixture `proxy` le remplace par des doublures.
REAL_HOSTED = tools.Hosted
EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "extensions"

SAIN = '''
from llm_proxy import extension as ext

class Sain(ext.Tool):
    name = "sain"

    def prompt(self, present):
        return "A healthy tool."

    async def run(self, args, call):
        return ext.Result("ok", meta={"client": call.client})

TOOL = Sain()
'''

# Un outil du navigateur : sa déclaration (.py) et son exécution (.js).
PAGE_PY = '''
from llm_proxy import extension as ext

TOOL = ext.BrowserTool("vibrer", description="Fait vibrer l'appareil.",
                       params={"ms": {"type": "integer"}}, settings={"max_ms": 500})
'''
PAGE_JS = "export default (ext) => ({ tools: [] });\n"


def tool_file(name: str, members: str = "", head: str = "") -> str:
    """Le texte d'une extension d'un outil `name`, avec `members` en plus
    dans sa classe et `head` en tête de module."""
    return (f"{head}\nfrom llm_proxy import extension as ext\n\n"
            f"class T(ext.Tool):\n    name = {name!r}\n{members}\n"
            f"    def prompt(self, present):\n        return 'x'\n\n"
            f"    async def run(self, args, call):\n        return ext.Result('x')\n\n"
            f"TOOL = T()\n")


@pytest.fixture(autouse=True)
def registry(monkeypatch, tmp_path):
    """Une copie du registre (outils du proxy, du navigateur, familles),
    rien sous [extensions], l'interrupteur de la page dans un fichier
    neuf : chaque test part des seuls outils natifs."""
    for attr in ("REGISTRY", "PAGE", "FAMILIES"):
        monkeypatch.setattr(tools, attr, list(getattr(tools, attr)))
    monkeypatch.setitem(config.CONFIG, "extensions", {})
    monkeypatch.setattr(extension_state, "PATH", str(tmp_path / "extensions.json"))
    yield tools.REGISTRY
    # Rien ne reste pour le test suivant : ni l'état du chargement, ni les
    # modules importés.
    extensions.load(tools, "")
    for name in [m for m in sys.modules if m.startswith("llm_proxy_ext.")]:
        del sys.modules[name]


@pytest.fixture
def mounted(tmp_path, monkeypatch):
    """`mounted(files, writable)` : un dossier d'extensions sur un montage
    en LECTURE SEULE (os.statvfs le dit pour ce qui est dessous, sauf les
    chemins de `writable`). `files` : chemin relatif → texte."""
    root = tmp_path / "ext"
    real = os.statvfs
    open_ = set()

    class Ro:
        f_flag = os.ST_RDONLY

    def statvfs(path):
        path = Path(path).resolve()
        if path.is_relative_to(root.resolve()) \
                and str(path.relative_to(root.resolve())) not in open_:
            return Ro()
        return real(path)
    monkeypatch.setattr(os, "statvfs", statvfs)

    def make(files: dict[str, str], writable=()) -> Path:
        root.mkdir(exist_ok=True)
        open_.update(writable)
        for name, text in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text, encoding="utf-8")
        return root
    return make


def load(root) -> tuple[list, list]:
    return extensions.load(tools, str(root))


# ── le chargement ───────────────────────────────────────────────────────

def test_sans_dossier_rien_n_est_charge(registry):
    before = [t.name for t in registry]
    assert extensions.load(tools, "") == ([], [])
    assert [t.name for t in registry] == before


def test_extension_en_faute_ecartee_entiere_les_autres_chargees(mounted, tmp_path, monkeypatch,
                                                                registry):
    """Chaque extension fautive est écartée entière, avec sa raison, sans
    rester dans sys.modules ; la saine du même dossier est chargée, et le
    démarrage ne s'arrête pas (pas même par SystemExit)."""
    monkeypatch.setitem(config.CONFIG, "extensions", {"eteinte": {"enabled": False}})
    (tmp_path / "ailleurs").mkdir()
    (tmp_path / "ailleurs" / "ailleurs.py").write_text(SAIN, encoding="utf-8")
    cases = {
        "a_plante": ({"a_plante.py": "raise RuntimeError('boum')"}, "boum"),
        "a_sortie": ({"a_sortie.py": "raise SystemExit(3)"}, "SystemExit"),
        "b_vide": ({"b_vide.py": "X = 1"}, "ni TOOL"),
        "b_sans_py": ({"autre.py": SAIN}, "b_sans_py.py manquant"),
        "c_natif": ({"c_natif.py": tool_file("web_search")}, "nom d'outil natif"),
        "c_natif_page": ({"c_natif_page.py": tool_file("ask_user")}, "nom d'outil natif"),
        "d_double": ({"d_double.py": tool_file("sain")}, "déjà pris"),
        "d_fonction": ({"d_fonction.py": tool_file("function")}, "nom invalide"),
        "d_espace": ({"d_espace.py": tool_file("a b")}, "nom invalide"),
        "e_membre": ({"e_membre.py": tool_file("e", members="    bonus = 1\n")},
                     "membre public inconnu"),
        "e_extra": ({"e_extra.py": tool_file("x", members="    extra = frozenset({'bonus'})\n"
                                                           "    bonus = 1\n")}, "`extra`"),
        "f_futur": ({"f_futur.py": tool_file("f", head="REQUIRES = 99")}, "contrat 99"),
        "f_bool": ({"f_bool.py": tool_file("g", head="REQUIRES = True")}, "contrat True"),
        "g_sans_js": ({"g_sans_js.py": PAGE_PY}, "g_sans_js.js manquant"),
        "g_js_seul": ({"g_js_seul.py": tool_file("h"), "g_js_seul.js": PAGE_JS},
                      "sans outil du navigateur"),
        "g_web_api": ({"g_web_api.py": PAGE_PY.replace("TOOL = ", "T = ") + (
            "\nclass Api(ext.BrowserTool):\n    offered = frozenset({ext.Offer.API})\n"
            "TOOL = Api('vib', description='x')\n"), "g_web_api.js": PAGE_JS},
                      "qu'au web"),
        "g_famille": ({"g_famille.py": PAGE_PY + "FAMILY = ext.Family('g', 'G', tools=('autre',))\n",
                       "g_famille.js": PAGE_JS}, "outils du navigateur de l'extension"),
        "g_sans_libelle": ({"g_sans_libelle.py": PAGE_PY.replace("vibrer", "vib2")
                            + "FAMILY = ext.Family('g2', '', tools=('vib2',))\n",
                            "g_sans_libelle.js": PAGE_JS}, "sans libellé"),
        "eteinte": ({"eteinte.py": tool_file("eteinte")}, "désactivée"),
        "Maj": ({"Maj.py": SAIN}, "nom : lettres"),
        "path": ({"path.py": SAIN}, "nom : lettres"),
        "racine.py": ({}, "une extension est un sous-dossier"),
    }
    files = {"_aide/_aide.py": "raise SystemExit", "c0_sain/c0_sain.py": SAIN,
             "racine.py": SAIN}
    for name, (parts, _) in cases.items():
        files.update({f"{name}/{f}": text for f, text in parts.items()})
    root = mounted(files)
    (root / "lien").symlink_to(tmp_path / "ailleurs")
    cases["lien"] = ({}, "pas un lien vers ailleurs")
    loaded, refused = load(root)
    assert loaded == [("c0_sain", ["sain"])]
    why = dict(refused)
    for name, (_, reason) in cases.items():
        assert reason in why.get(name, ""), (name, why.get(name))
    assert "_aide" not in why                     # un dossier en _ n'est pas lu
    assert not (root / "c0_sain" / "__pycache__").exists()   # le source compilé, pas de cache
    names = [t.name for t in (*registry, *tools.PAGE)]
    assert names.count("sain") == 1 and not {"e", "f", "g", "h", "x", "vibrer", "vib", "vib2",
                                             "eteinte"} & set(names)
    assert not {"g", "g2"} & {f.key for f in tools.FAMILIES}
    assert not [m for m in sys.modules if m.startswith("llm_proxy_ext.") and m != "llm_proxy_ext.c0_sain"]


def test_un_dossier_que_le_proxy_peut_ecrire_est_refuse(tmp_path, registry):
    """Une faille d'écriture ne doit pas devenir du code exécuté au
    démarrage : hors d'un montage en lecture seule, un dossier ou un
    fichier à l'uid du proxy est refusé, même en 0555 ou 0444 (il pourrait
    en changer les droits)."""
    before = [t.name for t in registry]
    root = tmp_path / "ext"
    (root / "sain").mkdir(parents=True)
    (root / "sain" / "sain.py").write_text(SAIN, encoding="utf-8")
    (root / "sain" / "sain.py").chmod(0o444)
    for d in (root / "sain", root):
        d.chmod(0o555)
    try:
        loaded, refused = load(root)
    finally:
        for d in (root, root / "sain"):
            d.chmod(0o755)
    assert loaded == [] and "peut écrire" in refused[0][1]
    assert [t.name for t in registry] == before


def test_un_fichier_que_le_proxy_peut_ecrire_est_refuse(mounted, registry):
    """Le dossier hors d'atteinte ne suffit pas : chaque sous-dossier et
    chaque fichier, .js compris, l'est aussi."""
    root = mounted({"sain/sain.py": SAIN, "ouvert/ouvert.py": tool_file("ouvert"),
                    "page/page.py": PAGE_PY, "page/page.js": PAGE_JS,
                    "dossier/dossier.py": tool_file("dossier")},
                   writable={"ouvert/ouvert.py", "page/page.js", "dossier"})
    loaded, refused = load(root)
    assert loaded == [("sain", ["sain"])]
    assert {name: why.split(" : ")[0] for name, why in refused} == {
        "dossier": "dossier que le proxy peut écrire", "ouvert": "ouvert.py",
        "page": "page.js"}


def test_le_contrat_public_s_importe_en_premier(mounted, tmp_path):
    """Un script (le test d'un admin pour son extension) qui importe la
    façade AVANT le reste du proxy : pas d'import circulaire."""
    root = mounted({"sain/sain.py": SAIN})
    out = subprocess.run(
        [sys.executable, "-c", "from llm_proxy import extension as ext; "
                               f"exec(open({str(root / 'sain' / 'sain.py')!r}).read()); "
                               "print(ext.API, TOOL.name)"],
        capture_output=True, text=True, cwd=Path(__file__).resolve().parents[1])
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["1", "sain"]


# ── les exemples, servis là où ils sont offerts ─────────────────────────

def test_les_exemples_sont_servis_la_ou_ils_sont_offerts(proxy, mounted, monkeypatch):
    """examples/extensions, chargés tels quels. horloge : son outil du
    proxy présenté avec son prompt, appelé par /v1/tools, son réglage
    [extensions.horloge] lu ; son outil du navigateur déclaré à la page
    (session), avec sa famille et l'adresse de son module, servi.
    pull_request, offert à la page seule, n'est pas sur /v1/tools."""
    monkeypatch.setitem(config.CONFIG, "extensions", {
        "horloge": {"timezone": "Europe/Paris"},
        "pull_request": {"forge": "gitlab", "url": "https://forge.exemple.fr",
                         "token": "t", "projects": ["equipe/*"]}})
    root = mounted({str(f.relative_to(EXAMPLES)): f.read_text(encoding="utf-8")
                    for f in EXAMPLES.glob("*/*.*") if f.suffix in (".py", ".js")})
    assert load(root) == ([("horloge", ["horloge", "local_time"]),
                           ("pull_request", ["pull_request"])], [])
    monkeypatch.setattr(tools, "Hosted", REAL_HOSTED)
    listed = {t["name"]: t for t in proxy.client.get("/v1/tools").json()["data"]}
    assert "pull_request" not in listed and "local_time" not in listed
    tool = next(t for t in tools.REGISTRY if t.name == "horloge")
    assert listed["horloge"]["description"] == tool.prompt(frozenset())
    r = proxy.client.post("/v1/tools/horloge", json={}).json()
    assert r["is_error"] is False and r["result"].endswith("(Europe/Paris)")
    r = proxy.client.post("/v1/tools/horloge", json={"timezone": "Nulle/Part"}).json()
    assert (r["is_error"], r["error"]) == (True, "invalid_input")
    assert extension.settings("horloge") == {"timezone": "Europe/Paris"}
    assert extension.settings("autre") == {}

    page = proxy.client.get("/ui/session").json()["chat"]["page"]
    family = next(f for f in page["families"] if f["key"] == "horloge")
    assert family["tools"] == ["local_time"] and family["on"] is True
    local = next(t for t in page["tools"] if t["name"] == "local_time")
    assert local["spec"]["function"]["description"].startswith("Donne la date")
    r = proxy.client.get(local["module"])
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/javascript")
    assert r.text == (EXAMPLES / "horloge" / "horloge.js").read_text(encoding="utf-8")
    assert proxy.client.get("/ui/extensions/pull_request/pull_request.js").status_code == 404


def test_une_cle_sans_ce_droit_ne_voit_pas_l_extension(idp, mounted, monkeypatch):
    """Les droits d'une clé valent pour un outil d'extension comme pour un
    natif : non nommé, il n'est ni listé ni exécutable (403) ; l'outil
    reçoit le compte de la clé qui l'appelle."""
    assert load(mounted({"sain/sain.py": SAIN}))[0] == [("sain", ["sain"])]
    monkeypatch.setattr(tools, "Hosted", REAL_HOSTED)
    alice = connect(idp)
    other = next(t.name for t in tools.enabled() if t.name != "sain")
    limited = alice.post("/ui/keys", json={"label": "ci", "tools": [other]}).json()["key"]
    names = {t["name"] for t in idp.client.get("/v1/tools", headers=bearer(limited)).json()["data"]}
    assert "sain" not in names
    r = idp.client.post("/v1/tools/sain", json={}, headers=bearer(limited))
    assert r.status_code == 403 and r.json()["error"]["type"] == "tool_not_permitted"
    full = alice.post("/ui/keys", json={"label": "tout"}).json()["key"]
    r = idp.client.post("/v1/tools/sain", json={}, headers=bearer(full)).json()
    assert r["result"] == "ok" and r["meta"]["client"]


# ── l'interrupteur de la page ───────────────────────────────────────────

def test_l_admin_eteint_une_extension_a_chaud_et_c_est_garde(idp, mounted, monkeypatch):
    """Configuration → Extensions : éteinte, son outil du proxy n'est plus
    ni listé ni exécutable (404), et son outil du navigateur n'est plus ni
    déclaré à la page, ni servi (son .js : 404), ni laissé dans une
    requête de la page (strip) ; l'état survit à un redémarrage ; un
    utilisateur ne bascule rien ; une extension arrêtée par la
    configuration ne se rallume pas de la page."""
    monkeypatch.setitem(config.CONFIG, "extensions", {"eteinte": {"enabled": False}})
    root = mounted({"sain/sain.py": SAIN + PAGE_PY.replace("TOOL = ", "PAGE = ")
                    + "TOOLS = [TOOL, PAGE]\n", "sain/sain.js": PAGE_JS,
                    "eteinte/eteinte.py": tool_file("eteinte")})
    load(root)
    monkeypatch.setattr(tools, "Hosted", REAL_HOSTED)
    alice, bob, _ = admin_and_user(idp)
    key = alice.post("/ui/keys", json={"label": "tout"}).json()["key"]

    def listed():
        return {t["name"] for t in idp.client.get("/v1/tools", headers=bearer(key)).json()["data"]}

    def page():
        return {t["name"]: t for t in alice.get("/ui/session").json()["chat"]["page"]["tools"]}

    entries = {e["name"]: e for e in alice.get("/ui/extensions").json()["extensions"]}
    assert entries["sain"]["enabled"] is True and entries["eteinte"]["stopped"] is True
    assert entries["sain"]["tools"] == [
        {"name": "sain", "runs": "proxy", "offered": ["api", "web"], "configured": True},
        {"name": "vibrer", "runs": "browser", "offered": ["web"], "configured": True}]
    assert bob.get("/ui/extensions").status_code == 403
    assert bob.post("/ui/extensions/sain", json={"enabled": False}).status_code == 403
    module = page()["vibrer"]["module"]
    assert page()["vibrer"]["settings"] == {"max_ms": 500}
    assert "sain" in listed() and alice.get(module).status_code == 200

    r = alice.post("/ui/extensions/sain", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert "sain" not in listed() and "vibrer" not in page()
    assert alice.get(module).status_code == 404
    r = idp.client.post("/v1/tools/sain", json={}, headers=bearer(key))
    assert r.status_code == 404 and r.json()["error"]["type"] == "unknown_tool"
    sent = {"tools": [{"type": "function", "function": {"name": "vibrer"}}]}
    assert page_tools.strip(sent) and "tools" not in sent
    assert extension_state.load() == {"sain": False}

    for t in [t for t in (*tools.REGISTRY, *tools.PAGE) if t.name in ("sain", "vibrer")]:
        (tools.REGISTRY if t in tools.REGISTRY else tools.PAGE).remove(t)
    tools.FAMILIES[:] = [f for f in tools.FAMILIES if f.key != "sain"]
    load(root)                                     # un redémarrage : registre neuf, l'état relu
    assert "sain" not in listed()
    assert alice.post("/ui/extensions/sain", json={"enabled": True}).json()["enabled"]
    assert "sain" in listed() and "vibrer" in page()
    assert alice.post("/ui/extensions/eteinte", json={"enabled": True}).status_code == 409
    assert alice.post("/ui/extensions/inconnue", json={"enabled": True}).status_code == 404
    assert alice.post("/ui/extensions/sain", json={"enabled": "non"}).status_code == 400


def test_un_etat_illisible_n_eteint_rien_et_reste_tel_quel(mounted, registry):
    """Un extensions.json abîmé : dit au journal, les extensions chargées
    restent allumées, et le fichier n'est pas écrasé par la lecture."""
    Path(extension_state.PATH).write_text("{abîmé", encoding="utf-8")
    load(mounted({"sain/sain.py": SAIN}))
    sain = next(t for t in registry if t.name == "sain")
    assert tools.active(sain)
    assert extension_state.load() is None          # illisible, pas « rien d'éteint »
    assert Path(extension_state.PATH).read_text(encoding="utf-8") == "{abîmé"
