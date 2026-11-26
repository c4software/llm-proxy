"""
Les PROMPTS MODIFIÉS par l'admin (Configuration → Prompts) : clé du
catalogue → texte, dans prompts.json à côté de config.toml. Une clé absente
garde le texte par défaut (prompts/*.toml). Le catalogue valide ce qui est
écrit (prompts/__init__.py) ; ce magasin ne fait que garder.

Lu et écrit par jsonfile.py (atomique ; un fichier illisible laisse les
défauts et n'est jamais écrasé par une lecture).
"""

from ..core import config
from ..core.settings import log
from . import jsonfile

PATH = config.resolve("prompts.json")


def load(path: str | None = None) -> dict[str, str] | None:
    """Clé → texte modifié (les valeurs qui ne sont pas du texte sont
    ignorées). `{}` sans fichier ; None s'il est illisible."""
    doc = jsonfile.load(path or PATH, "prompts")
    if doc is None:
        return None
    if dropped := [str(k) for k, v in doc.items() if not isinstance(v, str)]:
        log.warning("prompts : %s ignoré(s), pas du texte", ", ".join(dropped))
    return {str(k): v for k, v in doc.items() if isinstance(v, str)}


def save(overrides: dict[str, str], path: str | None = None) -> None:
    """Écrit toutes les modifications, atomiquement, triées par clé."""
    jsonfile.save(path or PATH, dict(sorted(overrides.items())))
