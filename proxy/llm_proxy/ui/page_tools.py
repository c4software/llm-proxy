"""
Les outils de la PAGE (ceux que le navigateur exécute : chat/tools/ de
web/static) que la configuration refuse à tous : [ui].page_tools_disabled,
des familles (« camera », « sql ») ou des outils (« run_php »).

Le proxy ne connaît pas ces outils par lui-même : il lit leur manifeste,
page_tools.json (famille → outils), tiré du registre de la page par
proxy/wasm/check-tools.mjs, qui échoue — et pytest avec lui — quand il
est périmé.

Ce qui est refusé :
  * la page le reçoit par /ui/session (`chat.page_tools_disabled`) et le
    retire de son registre : ni dans le menu, ni présenté au modèle, ni
    exécuté ;
  * le proxy retire lui-même ces outils de toute requête du chat de la
    page (strip) : une page modifiée ne les réactive pas.
"""

import json
import os

from ..core import config
from ..core.settings import log

MANIFEST = os.path.join(os.path.dirname(__file__), "page_tools.json")


def _manifest() -> dict[str, list[str]]:
    try:
        with open(MANIFEST, encoding="utf-8") as f:
            return {k: list(v.get("tools") or []) for k, v in json.load(f)["families"].items()}
    except (OSError, ValueError, KeyError, AttributeError) as exc:
        log.warning("outils de la page : manifeste illisible (%s) : %s", MANIFEST, exc)
        return {}


FAMILIES = _manifest()
TOOLS = {t for names in FAMILIES.values() for t in names}


def _resolve(entries) -> tuple[list[str], list[str]]:
    """Les entrées de [ui].page_tools_disabled → (familles entièrement
    refusées, outils refusés). Une entrée inconnue est dite au journal."""
    families, names = [], set()
    for e in entries:
        e = str(e).strip()
        if e in FAMILIES:
            families.append(e)
            names.update(FAMILIES[e])
        elif e in TOOLS:
            names.add(e)
        elif e:
            log.warning("[ui].page_tools_disabled : « %s » n'est ni une famille ni un outil "
                        "de la page (ui/page_tools.json)", e)
    # Une famille dont tous les outils sont refusés l'est aussi.
    families += [f for f, ts in FAMILIES.items() if f not in families and ts and set(ts) <= names]
    return sorted(families), sorted(names)


DISABLED_FAMILIES, DISABLED_TOOLS = _resolve(config.strings("ui.page_tools_disabled"))


def disabled() -> dict:
    """Ce que /ui/session dit à la page."""
    return {"families": DISABLED_FAMILIES, "tools": DISABLED_TOOLS}


def strip(payload: dict) -> bool:
    """Retire de `payload["tools"]` les fonctions refusées. Rend True si
    quelque chose a été retiré."""
    declared = payload.get("tools")
    if not DISABLED_TOOLS or not isinstance(declared, list):
        return False
    off = set(DISABLED_TOOLS)
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
