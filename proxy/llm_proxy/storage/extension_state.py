"""
L'interrupteur des EXTENSIONS (Configuration → Extensions, admin) : les
extensions que l'admin a ALLUMÉES dans la page, `{"<nom>": true}`, dans
extensions.json à côté de config.toml. Une extension absente du fichier
est désactivée : déposée dans le dossier, elle paraît dans la page au
démarrage suivant, et n'est en service qu'une fois activée. Le fichier ne
fait que CHOISIR parmi du code déjà monté en lecture seule : il n'en
ajoute pas (tools/extensions.py).

Écriture atomique (fichier temporaire, puis os.replace). Un fichier
illisible est dit au journal et n'est jamais écrasé par une lecture : load
rend None, et l'appelant n'active rien.
"""

import json
import os
import tempfile

from ..core import config
from ..core.settings import log

PATH = config.resolve("extensions.json")


def load(path: str | None = None) -> dict[str, bool] | None:
    """L'état gardé : fichier → activée ? `{}` sans fichier ; None s'il est
    illisible (dit au journal)."""
    path = path or PATH
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("extensions : %s illisible (%s) — aucune extension activée, "
                    "fichier laissé tel quel", path, exc)
        return None
    if not isinstance(doc, dict):
        log.warning("extensions : %s n'est pas un objet JSON — ignoré", path)
        return None
    return {str(k): v for k, v in doc.items() if isinstance(v, bool)}


def save(state: dict[str, bool], path: str | None = None) -> None:
    """Écrit l'état entier, atomiquement. Seules les extensions activées
    sont gardées (absente = désactivée)."""
    path = path or PATH
    on = {k: True for k, v in sorted(state.items()) if v is True}
    folder = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".extensions-", suffix=".json", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(on, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
