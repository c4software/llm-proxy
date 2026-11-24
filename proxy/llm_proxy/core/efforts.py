"""
Les niveaux de réflexion (`reasoning_effort`) d'une requête de chat : celui
que porte `chat_template_kwargs` (pi, omp, la page), et la table
`effort_map` d'un backend, du TOML ([backends.<nom>].effort_map) comme d'un
backend de compte (storage/backends.py), qui ramène chaque niveau à ceux que
le backend accepte : vérifiée ici, appliquée par upstream/relaying.py.
"""

# Les niveaux neutres, du plus bas au plus haut ; « none » coupe la réflexion.
LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


def template_effort(kwargs) -> str:
    """Le niveau de réflexion que porte `chat_template_kwargs` : son
    `reasoning_effort`, « none » quand il coupe la réflexion
    (`enable_thinking` ou `thinking` faux), sinon «»."""
    if not isinstance(kwargs, dict):
        return ""
    effort = kwargs.get("reasoning_effort")
    if isinstance(effort, str) and effort.strip():
        return effort.strip().lower()
    if kwargs.get("enable_thinking") is False or kwargs.get("thinking") is False:
        return "none"
    return ""


def effort_map(value) -> dict:
    """La table `effort_map` d'un backend, vérifiée : niveau demandé →
    niveau envoyé, parmi LEVELS ; «» : le niveau n'est pas envoyé. Absente :
    {} (niveaux tels quels). Lève ValueError, au message lisible."""
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ValueError("`effort_map` : une table niveau → niveau")
    out = {}
    for level, sent in value.items():
        level = str(level).strip().lower()
        sent = sent.strip().lower() if isinstance(sent, str) else sent
        if level not in LEVELS or (sent != "" and sent not in LEVELS):
            raise ValueError("`effort_map` : niveaux parmi " + ", ".join(LEVELS)
                             + " (« » : non envoyé)")
        out[level] = sent
    return out


def remap(payload: dict, table: dict) -> None:
    """Le `reasoning_effort` du corps, et celui de `chat_template_kwargs`
    (pi, omp, la page vers llama.cpp), ramenés par `table` (effort_map) ;
    une valeur «» le retire."""
    if not table:
        return
    kwargs = payload.get("chat_template_kwargs")
    for holder in (payload, kwargs if isinstance(kwargs, dict) else {}):
        effort = holder.get("reasoning_effort")
        sent = table.get(effort.strip().lower()) if isinstance(effort, str) else None
        if sent == "":
            holder.pop("reasoning_effort")
        elif sent is not None:
            holder["reasoning_effort"] = sent
