"""
La DÉCLARATION des outils hébergés dans une requête : ce que le client
déclare, ce que [chat].always présente d'office, et la requête préparée
pour le backend.
"""

import fnmatch
from ...tools.contract import ToolName

from .common import ALWAYS, ALWAYS_EXCEPT, SERVER, Context, Refused


def declares(payload: dict, kinds) -> bool:
    """La requête déclare-t-elle un outil hébergé ? `kinds` : tous les
    types que le paquet tools/ connaît, actifs ou non."""
    if "web_search_options" in payload:
        return True
    # Un `tool_choice` à la forme Responses (`{"type": "web_search"}`)
    # vise un outil hébergé : aucun backend ne le lirait, c'est à
    # prepare() de le traduire ou de le refuser.
    choice = payload.get("tool_choice")
    if isinstance(choice, dict) and choice.get("type") in kinds:
        return True
    tools = payload.get("tools")
    return isinstance(tools, list) and any(
        isinstance(t, dict) and t.get("type") in kinds for t in tools)


def unasked(payload: dict) -> bool:
    """Présenter à cette requête les outils de [chat].always ? Non si
    `tool_choice` vaut `none` ou si `n` > 1. Le MODÈLE est l'affaire de la
    route."""
    n = payload.get("n")
    return bool(ALWAYS) and payload.get("tool_choice") != "none" \
        and not (isinstance(n, int) and n > 1)


def offered(active) -> list[str]:
    """Les NOMS des outils que [chat].always présente à l'instant, parmi
    `active`, dans l'ordre de la liste. `mcp:<serveur>` désigne les outils
    dont c'est un type de déclaration (Tool.kinds)."""
    names: dict[str, None] = {}
    for entry in ALWAYS:
        for tool in active:
            if (entry in tool.kinds) if entry.startswith(SERVER) \
                    else tool.name == entry:
                names[tool.name] = None
    return list(names)


def excepted(model: str) -> bool:
    """Ce modèle (nom préfixé `<backend>/<modèle>`) est-il de ceux à qui
    rien n'est présenté d'office ([chat].always_except) ?"""
    model = model.lower()
    return any(fnmatch.fnmatchcase(model, p) for p in ALWAYS_EXCEPT)


def prepare(payload: dict, hosted, kinds, always=()) -> Context:
    """Remplace, DANS `payload`, chaque déclaration d'outil hébergé par
    les fonctions du paquet tools/ et rend le contexte de la réponse.
    `hosted` : l'annuaire tools.Hosted (vide si aucun outil n'est actif).
    `always` : les noms des outils à présenter d'office (offered), à la
    suite de ceux du client ; ceux que l'annuaire n'a pas, ou dont le nom
    est pris, sont passés.

    Un outil déclaré que le proxy connaît mais n'a pas activé est REFUSÉ
    (400) : le retirer en silence ferait répondre le modèle sans recherche,
    le laisser passer rendrait une erreur de backend muette sur la cause.
    (La surface Responses l'ignore : Codex le déclare d'office.) Un type
    inconnu du paquet est laissé au backend.

    `tool_choice` à la forme Responses : traduit vers la première fonction
    présentée pour ce type, au premier tour seulement (tool_loop.hosted_loop
    ramène un choix forcé à `auto` ensuite). S'il vise un outil désactivé
    ou non déclaré : 400."""
    if isinstance(payload.get("n"), int) and payload["n"] > 1:
        raise Refused("`n` > 1 n'est pas pris en charge avec un outil "
                      "hébergé : une réponse, une boucle")
    ctx = Context()
    tools = payload.get("tools")
    tools = list(tools) if isinstance(tools, list) else []
    if "web_search_options" in payload:
        del payload["web_search_options"]
        if not any(isinstance(t, dict) and ToolName.WEB_SEARCH == t.get("type")
                   for t in tools):
            tools.append({"type": ToolName.WEB_SEARCH})
    # Une fonction du client garde son nom : l'outil hébergé homonyme
    # n'est alors pas présenté (même règle que la surface Responses).
    taken = {t["function"].get("name") for t in tools
             if isinstance(t, dict) and isinstance(t.get("function"), dict)}
    out: list = []
    slots: list[int] = []       # places des outils hébergés dans `out`
    for t in tools:
        kind = t.get("type") if isinstance(t, dict) else None
        if kind not in kinds:
            out.append(t)
            continue
        found = hosted.for_kind(kind) if hosted else []
        if not found:
            raise Refused(
                f"outil hébergé «{kind}» déclaré mais désactivé sur ce "
                f"proxy ([tools.<nom>].enabled dans config.toml ; pour un "
                f"serveur MCP : désactivé, ou pas encore joint)")
        for tool in found:
            if tool.name in taken:
                continue
            taken.add(tool.name)
            ctx.hosted[tool.name] = tool
            slots.append(len(out))
            out.append(tool)
    for name in always:
        tool = hosted.by_name.get(name) if hosted else None
        if tool is None or name in taken:
            continue
        taken.add(name)
        ctx.hosted[name] = tool
        slots.append(len(out))
        out.append(tool)
    # Chaque outil sait avec qui il est présenté : une description ne
    # renvoie pas à un outil absent (désactivé, ou nom pris par le client).
    present = frozenset(ctx.hosted)
    for at in slots:
        out[at] = out[at].spec(present)
    if not slots and len(out) == len(tools):
        pass        # rien de remplacé, rien d'ajouté : `tools` reste le sien
    elif out:
        payload["tools"] = out
    else:
        payload.pop("tools", None)
    choice = payload.get("tool_choice")
    kind = choice.get("type") if isinstance(choice, dict) else None
    if kind in kinds:
        found = hosted.for_kind(kind) if hosted else []
        if not found:
            raise Refused(
                f"`tool_choice` vise l'outil hébergé «{kind}», désactivé sur "
                f"ce proxy ([tools.<nom>].enabled dans config.toml)")
        name = next((t.name for t in found if t.name in ctx.hosted), None)
        if name is None:
            raise Refused(
                f"`tool_choice` vise l'outil hébergé «{kind}», que `tools` "
                f"ne déclare pas")
        payload["tool_choice"] = {"type": "function",
                                  "function": {"name": name}}
    if payload.get("stream") and ctx.hosted:
        options = payload.get("stream_options")
        options = dict(options) if isinstance(options, dict) else {}
        ctx.include_usage = bool(options.get("include_usage"))
        # Sans lui un flux ne porte aucun `usage` : la ligne de stats de
        # la réponse, cumulée sur les tours, retomberait sur l'estimation.
        payload["stream_options"] = {**options, "include_usage": True}
    return ctx
