"""
Un objet JSON gardé dans un FICHIER à côté de config.toml, écrit par
l'admin depuis la page : l'interrupteur des extensions (extension_state.py),
les prompts modifiés (prompt_overrides.py).

Écriture atomique (fichier temporaire, puis os.replace). Un fichier
illisible est dit au journal et n'est jamais écrasé par une lecture : load
rend None, et l'appelant s'en tient à ses défauts.
"""

import json
import os
import tempfile

from ..core.settings import log


def load(path: str, what: str) -> dict | None:
    """L'objet du fichier : `{}` sans fichier ; None s'il est illisible ou
    n'est pas un objet (dit au journal, avec `what` : ce qu'il garde)."""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("%s : %s illisible (%s) — fichier laissé tel quel", what, path, exc)
        return None
    if not isinstance(doc, dict):
        log.warning("%s : %s n'est pas un objet JSON — ignoré", what, path)
        return None
    return doc


def save(path: str, doc: dict) -> None:
    """Écrit `doc` entier, atomiquement."""
    folder = os.path.dirname(os.path.abspath(path))
    name = os.path.splitext(os.path.basename(path))[0]
    fd, tmp = tempfile.mkstemp(prefix=f".{name}-", suffix=".json", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
