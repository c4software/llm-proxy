"""
Une expression régulière ÉCRITE PAR LE MODÈLE, lue dans le proxy (web_grep ;
celle de `repository` tourne dans un bac, repository_bac.py) : par
la bibliothèque `regex` et non `re`, parce que `regex` a un délai
(`timeout`). Une expression à retour arrière catastrophique — `(a+)+$` —
est coupée au bout de TIMEOUT au lieu de bloquer le proxy ; la recherche
tourne dans un fil (`concurrent=True` rend le GIL) : la boucle asyncio ne
l'attend pas.

  compile(source, ignore_case)   le motif compilé ; ToolError s'il est
                                 vide, trop long ou invalide
  TIMEOUT                        le délai d'UNE recherche
                                 ([tools.web_grep].regex_timeout)
  clip(line, start, end)         une ligne trouvée, coupée autour de la
                                 correspondance
"""

from ..core import config
from .contract import ToolError

TIMEOUT = config.num("tools.web_grep.regex_timeout", 1.0)
MAX_PATTERN = 500
LINE_CHARS = 300


def clip(line: str, start: int = 0, end: int = 0) -> str:
    """La ligne coupée à LINE_CHARS, autour de [start, end) s'il est donné
    (la correspondance), depuis son début sinon."""
    width = LINE_CHARS
    if len(line) <= width:
        return line
    if end <= start:
        return line[:width] + "…"
    margin = max((width - (end - start)) // 2, 0)
    a = max(start - margin, 0)
    b = min(a + width, len(line))
    a = max(b - width, 0)
    return ("…" if a else "") + line[a:b] + ("…" if b < len(line) else "")


def compile(source, ignore_case: bool = True):
    # Importé ici : le proxy démarre sans `regex`.
    import regex

    if not isinstance(source, str) or not source.strip():
        raise ToolError("invalid_input", "`pattern` is required.")
    if len(source) > MAX_PATTERN:
        raise ToolError("invalid_input",
                        f"`pattern` is longer than {MAX_PATTERN} characters.")
    flags = regex.MULTILINE | regex.V0 | (regex.IGNORECASE if ignore_case else 0)
    try:
        return regex.compile(source, flags)
    except regex.error as exc:
        raise ToolError("invalid_input", f"invalid regular expression ({exc}).")
