"""
Les EXTENSIONS : des outils écrits par l'admin, au contrat de tous les
outils (contract.py), déposés dans [extensions].path et chargés AU
DÉMARRAGE (startup.py, avant les serveurs MCP ; pas à chaud : un
redémarrage relit le dossier). Une extension est un SOUS-DOSSIER :

  <path>/<nom>/<nom>.py   obligatoire : il importe le contrat public
                          (llm_proxy/extension.py) et expose `TOOL` ou
                          `TOOLS` — des outils du proxy (Tool, son `run`)
                          ou du navigateur (BrowserTool) —, et pour ces
                          derniers `FAMILY` ou `FAMILIES` (sinon, une
                          famille au nom de l'extension)
  <path>/<nom>/<nom>.js   l'exécution de ses outils du navigateur, servie
                          à la page (ui/extension_routes.py) : obligatoire
                          avec eux, refusé sans eux

Chaque outil entre au registre par register(…, "extension"), qui vérifie
le contrat (faults), chaque famille par register_family ; il est alors un
outil comme un autre — surfaces, /v1/tools, /mcp, droits des clés, menu
Outils de la page, webhooks — là où son `offered` le dit. Une extension en
faute est écartée ENTIÈRE, dite au journal ; elle n'arrête ni le
démarrage ni les autres.

L'INTERRUPTEUR : une extension chargée s'éteint et se rallume À CHAUD dans
la page (Configuration → Extensions), état gardé par
storage/extension_state.py ; active() le lit, tools.active() avec, et le
service de son .js aussi. `[extensions.<nom>].enabled = false` est l'arrêt
de l'opérateur : elle n'est pas importée, et la page ne peut pas la
rallumer.

Une extension a la CONFIANCE TOTALE : son .py tourne dans le processus,
son .js dans la page, avec la session de l'utilisateur. Son dossier et
chaque fichier doivent être hors d'atteinte du proxy : sur un montage en
lecture seule, ou à un autre propriétaire et sans droit d'écriture — un
fichier à lui, même en 0444, il pourrait le rendre inscriptible. Une
faille d'écriture ne doit pas devenir du code exécuté au démarrage
suivant. docs/extensions.md.
"""

import hashlib
import importlib.util
import os
import re
import sys
import threading
import traceback
from pathlib import Path

from ..core import config
from ..core.settings import log
from ..storage import extension_state
from .contract import Family, Runs, Tool, faults

# La version du contrat public (llm_proxy/extension.py la réexporte).
API = 1
PROVIDER = "extension"
PATH = config.text("extensions.path").strip()
# Le nom d'une extension (son sous-dossier, et ses fichiers <nom>.py,
# <nom>.js) : celui de sa table [extensions.<nom>]. `path` est un réglage
# de [extensions], pas un nom d'extension.
STEM = re.compile(r"[a-z0-9][a-z0-9_]*\Z")
RESERVED_STEMS = {"path"}
STOPPED = "désactivée par la configuration"



class Loaded:
    """Une extension chargée : ses outils, ses familles, son .js (et le
    condensé de son contenu, pour l'adresse que la page importe)."""

    def __init__(self, name: str, tools: list, families: list, js: Path | None):
        self.name, self.tools, self.families, self.js = name, tools, families, js
        self.digest = hashlib.sha256(js.read_bytes()).hexdigest()[:12] if js else ""


# L'état du dernier load() : nom d'outil → (extension, outil) ; extension
# → Loaded ; les écartées (nom, raison) ; les extensions éteintes dans la
# page.
_OF: dict[str, tuple[str, Tool]] = {}
_LOADED: dict[str, Loaded] = {}
_REFUSED: list[tuple[str, str]] = []
# Remplacé d'un bloc (jamais vidé puis rempli : active() le lit sans
# verrou) ; _SWITCH sérialise les bascules et leur écriture.
_OFF: frozenset[str] = frozenset()
_SWITCH = threading.Lock()


def out_of_reach(path: Path) -> bool:
    """Le proxy ne peut-il pas écrire `path`, ni en changer les droits ?"""
    try:
        if os.statvfs(path).f_flag & os.ST_RDONLY:
            return True
        st = os.stat(path)
    except OSError:
        return False
    return st.st_uid != os.geteuid() and not os.access(path, os.W_OK)


def _file(path: Path, folder: Path) -> str:
    """Pourquoi ce fichier de l'extension n'est pas lu («» : il l'est)."""
    real = path.resolve()
    if real.parent != folder.resolve() or not real.is_file():
        return f"{path.name} : un fichier du dossier est attendu (pas un lien vers ailleurs)"
    if not out_of_reach(real):
        return f"{path.name} : fichier que le proxy peut écrire : montage en lecture seule attendu"
    return ""


def _refusal(folder: Path, root: Path, tables: dict) -> str:
    """Pourquoi cette extension n'est pas importée («» : elle l'est)."""
    name = folder.name
    if not STEM.match(name) or name in RESERVED_STEMS:
        return "nom : lettres minuscules, chiffres, « _ » (et pas « path »)"
    table = tables.get(name)
    if isinstance(table, dict) and table.get("enabled") is False:
        return f"{STOPPED} ([extensions.{name}].enabled)"
    real = folder.resolve()
    if real.parent != root.resolve() or not real.is_dir():
        return "un sous-dossier du dossier est attendu (pas un lien vers ailleurs)"
    if not out_of_reach(real):
        return "dossier que le proxy peut écrire : montage en lecture seule attendu"
    if not (folder / f"{name}.py").exists():
        return f"{name}.py manquant (une extension : {name}/{name}.py, et {name}.js au besoin)"
    for path in (folder / f"{name}.py", folder / f"{name}.js"):
        if path.exists() or path.is_symlink():
            if why := _file(path, folder):
                return why
    return ""


def _declared(module, name: str) -> tuple[list, list, str]:
    """Les outils et les familles que le module expose → (outils,
    familles, raison d'un refus)."""
    requires = getattr(module, "REQUIRES", 1)
    if type(requires) is not int or requires > API:
        return [], [], f"demande le contrat {requires!r}, le proxy offre {API}"
    found = getattr(module, "TOOLS", None)
    if found is None and getattr(module, "TOOL", None) is not None:
        found = [module.TOOL]
    if not isinstance(found, (list, tuple)) or not found \
            or not all(isinstance(t, Tool) for t in found):
        return [], [], "ni TOOL (un Tool) ni TOOLS (une liste de Tool)"
    families = getattr(module, "FAMILIES", None)
    if families is None and getattr(module, "FAMILY", None) is not None:
        families = [module.FAMILY]
    browser = [t.name for t in found if t.runs == Runs.BROWSER]
    if families is None:
        # Sans famille déclarée : une, au nom de l'extension.
        families = [Family(name, name, tuple(browser))] if browser else []
    if not isinstance(families, (list, tuple)) or not all(isinstance(f, Family) for f in families):
        return [], [], "FAMILY (une Family) ou FAMILIES (une liste de Family) attendu"
    return list(found), list(families), ""


def load(tools, path: str = PATH) -> tuple[list, list]:
    """Charge les extensions de `path` dans le registre de `tools` (le
    paquet tools/ : register, register_family, REGISTRY, FAMILIES), et
    relit l'interrupteur de la page. → (chargées [(nom, [outils])],
    écartées [(nom, raison)]), dites au journal. Sans `path`, rien."""
    global _OFF
    loaded, refused = [], []
    _OF.clear()
    _LOADED.clear()
    _REFUSED.clear()
    if path:
        root = Path(path)
        if not root.is_dir():
            refused.append((path, "dossier introuvable"))
        elif not out_of_reach(root):
            refused.append((path, "dossier que le proxy peut écrire : montage en lecture "
                                  "seule attendu"))
        else:
            tables = config.get("extensions") or {}
            for entry in sorted(root.iterdir()):
                if entry.name.startswith(("_", ".")):
                    continue                      # ignoré : aide, cache, fichier caché
                if not entry.is_dir():
                    if entry.suffix == ".py":
                        refused.append((entry.name, f"une extension est un sous-dossier : "
                                                    f"{entry.stem}/{entry.name}"))
                    continue
                if why := _refusal(entry, root, tables):
                    refused.append((entry.name, why))
                    continue
                ext, why = _import(entry, tools)
                if ext is None:
                    refused.append((entry.name, why))
                    continue
                loaded.append((ext.name, [t.name for t in ext.tools]))
                _LOADED[ext.name] = ext
                _OF.update({t.name: (ext.name, t) for t in ext.tools})
    _REFUSED.extend(refused)
    # Un état illisible est dit au journal (extension_state.load) et ne
    # change rien au fichier : les extensions chargées sont allumées.
    _OFF = frozenset(f for f, on in (extension_state.load() or {}).items() if on is False)
    for name, why in refused:
        log.warning("extension %s écartée : %s", name, why)
    for name, names in loaded:
        log.info("extension %s : %s%s", name, ", ".join(names),
                 " (éteinte dans la page)" if name in _OFF else "")
    return loaded, refused


def active(tool: Tool) -> bool:
    """Faux pour l'outil d'une extension que la page a éteinte ; vrai pour
    tout autre outil."""
    name, known = _OF.get(tool.name, ("", None))
    return known is not tool or name not in _OFF


def module_url(tool: Tool) -> str:
    """L'adresse du .js qui exécute cet outil du navigateur d'une
    extension («» : un outil natif). Le condensé change avec le fichier :
    la page ne garde pas une version périmée."""
    name, known = _OF.get(tool.name, ("", None))
    ext = _LOADED.get(name)
    if known is not tool or ext is None or not ext.js or tool.runs != Runs.BROWSER:
        return ""
    return f"/ui/extensions/{name}/{name}.js?v={ext.digest}"


def script(name: str) -> Path | None:
    """Le .js d'une extension chargée et allumée, ou None."""
    ext = _LOADED.get(name)
    return ext.js if ext is not None and ext.js and name not in _OFF else None


def listing() -> list[dict]:
    """Pour la page : chaque extension, chargée (ses outils et où chacun
    s'exécute, allumée ou non) ou écartée (sa raison ; `stopped` : arrêtée
    par la configuration)."""
    out = [{"name": name, "enabled": name not in _OFF,
            "tools": [{"name": t.name, "runs": str(t.runs), "offered": sorted(t.offered),
                       "configured": bool(t.enabled)} for t in ext.tools]}
           for name, ext in _LOADED.items()]
    out += [{"name": name, "refused": why, "stopped": why.startswith(STOPPED)}
            for name, why in _REFUSED]
    return out


def switch(name: str, on: bool) -> None:
    """Allume ou éteint une extension CHARGÉE, à chaud, et le garde.
    KeyError : extension non chargée (inconnue, écartée, arrêtée par la
    configuration)."""
    global _OFF
    if name not in _LOADED:
        raise KeyError(name)
    with _SWITCH:
        off = (_OFF | {name}) if not on else (_OFF - {name})
        extension_state.save({n: False for n in off})
        _OFF = off
    log.info("extension %s %s dans la page", name, "allumée" if on else "éteinte")


def _import(folder: Path, tools) -> tuple[Loaded | None, str]:
    """Importe <nom>.py et inscrit ses outils et ses familles, TOUS ou
    aucun : tout est vérifié avant la première inscription. → (l'extension,
    «») ou (None, raison). Un module écarté ne reste pas dans
    sys.modules."""
    name = folder.name
    file, js = folder / f"{name}.py", folder / f"{name}.js"
    module_name = f"llm_proxy_ext.{name}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, file)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        # Le source lui-même, compilé ici : pas le cache de l'import
        # (__pycache__/*.pyc), qui pourrait être à la portée du proxy et
        # remplacerait le code du fichier vérifié. SystemExit aussi : une
        # extension n'arrête pas le proxy.
        exec(compile(file.resolve().read_bytes(), str(file), "exec"), module.__dict__)
    except (Exception, SystemExit):
        sys.modules.pop(module_name, None)
        return None, "erreur à l'import :\n" + traceback.format_exc(limit=5)
    found, families, why = _declared(module, name)
    errors = [why] if why else []
    taken = {k for t in tools.REGISTRY for k in t.kinds}
    names = {t.name for t in (*tools.REGISTRY, *tools.PAGE)}
    for tool in found:
        if tool.extra:
            errors.append(f"outil {tool.name!r} : `extra` est réservé aux outils MCP")
        elif fault := faults(tool, PROVIDER):
            errors.append("; ".join(fault))
        elif tool.name in names:
            errors.append(f"outil {tool.name!r} : nom déjà pris")
        elif tool.runs == Runs.PROXY and (clash := set(tool.kinds) & taken):
            errors.append(f"outil {tool.name!r} : type déjà déclaré par un autre outil "
                          f"({', '.join(sorted(clash))})")
        names.add(tool.name)
        if tool.runs == Runs.PROXY:
            taken |= set(tool.kinds)
    browser = {t.name for t in found if t.runs == Runs.BROWSER}
    if browser and not js.exists():
        errors.append(f"{name}.js manquant : il exécute ses outils du navigateur "
                      f"({', '.join(sorted(browser))})")
    if js.exists() and not browser:
        errors.append(f"{name}.js sans outil du navigateur (BrowserTool) à exécuter")
    # Les familles, vérifiées comme à leur inscription (family_faults),
    # sur le registre tel qu'il sera : ses outils du navigateur à elle.
    known = list(tools.FAMILIES)
    for family in families:
        if not isinstance(family.key, str) or not STEM.match(family.key):
            errors.append(f"famille {family.key!r} : clé invalide")
        elif not set(family.tools) <= browser:
            errors.append(f"famille {family.key!r} : ses outils doivent être des outils du "
                          f"navigateur de l'extension")
        else:
            errors += tools.family_faults(family, PROVIDER, page_names=browser, families=known)
        known.append(family)
    grouped = [n for f in families for n in f.tools]
    if browser and sorted(grouped) != sorted(browser):
        errors.append("chaque outil du navigateur est dans une famille, et une seule")
    if errors:
        sys.modules.pop(module_name, None)
        return None, "; ".join(errors)
    done = []
    try:
        for tool in found:
            done.append(tools.register(tool, PROVIDER))
        for family in families:
            tools.register_family(family, PROVIDER)
    except ValueError as exc:
        # Ce que les vérifications n'ont pas vu : rien ne reste inscrit.
        for tool in done:
            (tools.PAGE if tool.runs == Runs.BROWSER else tools.REGISTRY).remove(tool)
        tools.FAMILIES[:] = [f for f in tools.FAMILIES if f not in families]
        sys.modules.pop(module_name, None)
        return None, str(exc)
    return Loaded(name, found, families, js if js.exists() else None), ""
