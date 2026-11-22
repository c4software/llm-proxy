"""
Les outils du NAVIGATEUR, pour la page : leurs DÉCLARATIONS (tools.PAGE et
tools.FAMILIES, au contrat de tools/contract.py ; natifs dans tools/page/,
ou d'une extension), servies par /ui/session à la page, qui y attache leur
exécution (web/static/chat/tools/) ; et ce que la configuration en refuse
à tous : [ui].page_tools_disabled, des familles (« camera », « sql ») ou
des outils (« run_php »).

Ce qui est refusé, ou d'une extension éteinte (Configuration →
Extensions) :
  * n'est pas dans la session : ni au menu, ni présenté au modèle, ni
    exécuté par la page ;
  * est retiré par le proxy de toute requête du chat de la page (strip) :
    une page modifiée ne le réactive pas.

Les outils des familles `authored` (bacs de code, artifacts) ont un
résultat écrit par le modèle : ils ne font connaître aucun site
(provenance.hosts_in_messages, que le chat de la page appelle avec eux).
"""

from .. import tools
from ..core import config
from ..core.settings import log
from ..tools import extensions


def _resolve(entries) -> tuple[list[str], list[str]]:
    """Les entrées de [ui].page_tools_disabled → (familles entièrement
    refusées, outils refusés). Une entrée inconnue est dite au journal."""
    families = {f.key: f.tools for f in tools.FAMILIES}
    known = {t.name for t in tools.PAGE}
    out, names = [], set()
    for e in entries:
        e = str(e).strip()
        if e in families:
            out.append(e)
            names.update(families[e])
        elif e in known:
            names.add(e)
        elif e:
            log.warning("[ui].page_tools_disabled : « %s » n'est ni une famille ni un outil "
                        "natif de la page (tools/page/) ; une extension s'éteint dans "
                        "Configuration → Extensions", e)
    # Une famille dont tous les outils sont refusés l'est aussi.
    out += [k for k, ts in families.items() if k not in out and set(ts) <= names]
    return sorted(out), sorted(names)


DISABLED_FAMILIES, DISABLED_TOOLS = _resolve(config.strings("ui.page_tools_disabled"))


def served(tool) -> bool:
    """La page a-t-elle cet outil du navigateur : ni refusé par la
    configuration, ni d'une extension éteinte ?"""
    return tool.name not in DISABLED_TOOLS and tools.active(tool, tools.Offer.WEB)


def declarations() -> dict:
    """Ce que /ui/session donne à la page : les familles (dans l'ordre du
    menu) et la fonction de chaque outil, ses `settings`, et pour celui
    d'une extension le `module` JS qui l'exécute. Seulement ce qui est
    servi (served)."""
    by_name = {t.name: t for t in tools.PAGE if served(t)}
    families, declared = [], []
    for f in tools.FAMILIES:
        names = [n for n in f.tools if n in by_name]
        if f.key in DISABLED_FAMILIES or not names:
            continue
        families.append({"key": f.key, "label": f.label, "hint": f.hint, "on": f.on,
                         "fixed": f.fixed, "authored": f.authored, "guidance": f.guidance,
                         "tools": names})
        present = frozenset(names)
        for n in names:
            t = by_name[n]
            entry = {"name": n, "spec": t.spec(present), "settings": dict(t.settings)}
            if module := extensions.module_url(t):
                entry["module"] = module
            declared.append(entry)
    return {"families": families, "tools": declared}


def authored() -> frozenset:
    """Les outils du navigateur des familles `authored`."""
    return frozenset(n for f in tools.FAMILIES if f.authored for n in f.tools)


def strip(payload: dict) -> bool:
    """Retire de `payload["tools"]` les fonctions des outils du navigateur
    qui ne sont pas servis. Rend True si quelque chose a été retiré."""
    declared = payload.get("tools")
    off = {t.name for t in tools.PAGE if not served(t)}
    if not off or not isinstance(declared, list):
        return False
    kept = [t for t in declared if not (
        isinstance(t, dict) and t.get("type") == "function"
        and isinstance(t.get("function"), dict) and t["function"].get("name") in off)]
    if len(kept) == len(declared):
        return False
    payload["tools"] = kept
    if not kept:
        payload.pop("tools")
        payload.pop("tool_choice", None)
    return True
