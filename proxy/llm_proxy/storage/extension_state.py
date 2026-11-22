"""
L'interrupteur des EXTENSIONS (Configuration → Extensions, admin) : ce que
l'admin a ACTIVÉ dans la page, par extension — `true` (tous ses outils) ou
la liste des outils actifs : `{"horloge": ["local_time"]}` —, dans
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


def load(path: str | None = None) -> dict[str, bool | list[str]] | None:
    """L'état gardé : extension → `True` (tous ses outils actifs) ou la
    liste de ses outils actifs (une autre valeur : aucun). `{}` sans
    fichier ; None s'il est illisible (dit au journal)."""
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
    return {str(k): v if v is True else [str(n) for n in v]
            for k, v in doc.items()
            if v is True or (isinstance(v, list) and v and all(isinstance(n, str) for n in v))}


def active_count(state: dict) -> int:
    """Les extensions dont au moins un outil est actif."""
    return sum(1 for v in state.values() if v is True or (isinstance(v, list) and v))


def save(state: dict[str, bool | list[str]], path: str | None = None) -> None:
    """Écrit l'état entier, atomiquement. Seules les extensions qui ont un
    outil actif sont gardées (absente = tout désactivé)."""
    path = path or PATH
    on = {k: (True if v is True else sorted(v)) for k, v in sorted(state.items())
          if v is True or (isinstance(v, list) and v)}
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
