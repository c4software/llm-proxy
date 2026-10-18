"""
La REQUÊTE : Responses → chat/completions (outils, contenu, éléments
rejoués, appels du client et leurs résultats).
"""

import json

from .common import SHELL_FIELDS, Context, Refused, _id


# ── Requête : Responses → OpenAI chat ───────────────────────────────────

def _function_tool(t: dict) -> dict:
    name = t.get("name")
    if not isinstance(name, str) or not name:
        raise Refused("outil `function` sans `name`")
    fn = {
        "name": name,
        "description": t.get("description") or "",
        "parameters": t.get("parameters")
        or {"type": "object", "properties": {}},
    }
    # `strict: false` est le défaut de chat/completions : ne l'envoyer
    # que vrai évite un champ de plus à un backend qui ne le connaît pas.
    if t.get("strict") is True:
        fn["strict"] = True
    return {"type": "function", "function": fn}


# Ajouté à la description d'un outil `custom` : la sienne dit souvent de
# NE PAS emballer l'entrée dans du JSON (« This is a FREEFORM tool » chez
# Codex), ce qui n'a pas de sens pour un modèle qui ne sait appeler que
# des fonctions.
CUSTOM_NOTE = ("Call this function with a single JSON argument, `input`: "
               "the raw text the tool expects, as one string.")


def _custom_tool(t: dict, function: str) -> dict:
    """Outil `custom` → fonction à un champ texte. La grammaire (`format`
    de type `grammar`) n'a pas d'équivalent chat/completions : elle part
    dans la description, où elle dit au moins au modèle quoi écrire."""
    description = [str(t.get("description") or ""), CUSTOM_NOTE]
    fmt = t.get("format")
    if isinstance(fmt, dict) and fmt.get("type") == "grammar" \
            and isinstance(fmt.get("definition"), str) and fmt["definition"]:
        syntax = str(fmt.get("syntax") or "").strip()
        description.append(
            f"The text must follow this {syntax + ' ' if syntax else ''}"
            f"grammar:\n{fmt['definition']}")
    return {"type": "function", "function": {
        "name": function,
        "description": "\n\n".join(d for d in description if d),
        "parameters": {"type": "object", "properties": {"input": {
            "type": "string",
            "description": "The raw text input of the tool."}},
            "required": ["input"]},
    }}


def _shell_tool(function: str) -> dict:
    """Outil `local_shell` → fonction. Ses champs sont ceux de l'action
    `exec` de l'élément `local_shell_call` (SDK openai-python,
    LocalShellCallAction ; Codex, LocalShellExecAction)."""
    return {"type": "function", "function": {
        "name": function,
        "description": "Run a command on the user's machine and return "
                       "its output.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "array", "items": {"type": "string"},
                        "description": "The command and its arguments, e.g. "
                                       "[\"bash\", \"-lc\", \"ls -la\"]."},
            "working_directory": {"type": "string",
                                  "description": "Directory to run it in."},
            "timeout_ms": {"type": "integer",
                           "description": "Timeout in milliseconds."},
            "env": {"type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "Environment variables to set."},
        }, "required": ["command"]},
    }}


def _client_names(tools) -> set[str]:
    names = set()
    for t in tools if isinstance(tools, list) else []:
        if not isinstance(t, dict):
            continue
        nested = t.get("tools") if t.get("type") == "namespace" else [t]
        for sub in nested if isinstance(nested, list) else []:
            if isinstance(sub, dict) and sub.get("type") == "function" \
                    and isinstance(sub.get("name"), str):
                names.add(sub["name"])
    return names


def _tools(tools, ctx: Context, hosted=None) -> list[dict]:
    """Outils Responses → outils chat/completions, à plat. Remplit
    ctx.namespaces (nom de fonction → namespace), ctx.hosted (fonctions
    que le proxy exécute) et ctx.ignored."""
    out: list = []
    seen: set[str] = set()
    slots: list[int] = []       # places des outils hébergés dans `out`
    # Une fonction du client garde son nom : l'outil hébergé homonyme
    # n'est alors pas présenté.
    client = _client_names(tools)

    def add(t: dict, namespace: str | None) -> None:
        tool = _function_tool(t)
        name = tool["function"]["name"]
        if name in seen:
            raise Refused(
                f"fonction «{name}» déclarée deux fois (namespaces aplatis) : "
                f"les noms doivent être uniques")
        seen.add(name)
        if namespace:
            ctx.namespaces[name] = namespace
        out.append(tool)

    def free(name: str) -> str:
        """Un nom de fonction libre pour un outil qui n'en est pas une :
        le sien, ou suffixé si une fonction du client (où qu'elle soit
        dans la liste) ou une fonction déjà posée le porte."""
        function, n = name, 1
        while function in client or function in seen:
            n += 1
            function = f"{name}_{n}"
        seen.add(function)
        return function

    def add_custom(t: dict, namespace: str | None) -> None:
        name = t.get("name")
        if not isinstance(name, str) or not name:
            raise Refused("outil `custom` sans `name`")
        if any(known == name for known, _ in ctx.custom.values()):
            raise Refused(
                f"outil `custom` «{name}» déclaré deux fois : les noms "
                f"doivent être uniques")
        function = free(name)
        ctx.custom[function] = (name, namespace)
        out.append(_custom_tool(t, function))

    for t in tools if isinstance(tools, list) else []:
        if not isinstance(t, dict):
            continue
        kind = t.get("type")
        if kind == "function":
            add(t, None)
        elif kind == "custom":
            add_custom(t, None)
        elif kind == "local_shell":
            # Sans nom ni réglage : déclaré deux fois, c'est le même outil.
            if ctx.shell is None:
                ctx.shell = free("local_shell")
                out.append(_shell_tool(ctx.shell))
        elif kind == "namespace":
            ns, nested = t.get("name"), t.get("tools")
            if not isinstance(ns, str) or not ns or not isinstance(nested, list):
                raise Refused("`namespace` mal formé : `name` et `tools` attendus")
            for sub in nested:
                # Codex y range aussi ses outils `custom` (mode « responses
                # lite » : ResponsesApiNamespaceTool::Custom).
                if isinstance(sub, dict) and sub.get("type") == "custom":
                    add_custom(sub, ns)
                    continue
                if not isinstance(sub, dict) or sub.get("type") != "function":
                    raise Refused(
                        f"namespace «{ns}» : seuls des outils `function` "
                        f"ou `custom` peuvent y être groupés")
                add(sub, ns)
        else:
            found = [t for t in (hosted.for_responses(kind) if hosted else [])
                     if t.name not in client and t.name not in seen]
            if found:
                # Outil que le proxy héberge : ses fonctions à la place —
                # une place réservée, remplie une fois tous connus.
                for t in found:
                    seen.add(t.name)
                    ctx.hosted[t.name] = t
                    slots.append(len(out))
                    out.append(t)
                continue
            # Outil hébergé par OpenAI seul (file_search, code_interpreter,
            # mcp…) : rien à traduire.
            ctx.ignored.append(str(kind))
    # Chaque outil sait avec qui il est présenté : une description ne
    # renvoie pas à un outil absent (désactivé, ou nom pris par le client).
    present = frozenset(ctx.hosted)
    for at in slots:
        out[at] = out[at].spec(present)
    return out


def _tool_choice(value, out: dict, ctx: Context) -> None:
    kind = value.get("type") if isinstance(value, dict) else None
    name = None
    if value in ("auto", "none", "required"):
        out["tool_choice"] = value
    elif kind == "function" and value.get("name"):
        name = value["name"]
    elif kind == "custom" and value.get("name"):
        name = ctx.custom_function(str(value["name"]))
    elif kind == "local_shell":
        name = ctx.shell
    if name:
        out["tool_choice"] = {"type": "function", "function": {"name": name}}


    # Tout autre choix (outil hébergé, allowed_tools…) : laissé au défaut.


def _image_part(block: dict) -> dict | None:
    url = block.get("image_url")
    if isinstance(url, dict):
        url = url.get("url")
    if isinstance(url, str) and url:
        return {"type": "image_url", "image_url": {"url": url}}
    return None


def _parts(content, images: bool) -> list[dict]:
    """Contenu d'un élément (chaîne, ou liste de parties input_text /
    output_text / input_image / input_file / refusal) → parties OpenAI.
    `images` = le backend accepte les `image_url` ; sinon un texte de
    remplacement, comme pour un fichier."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    parts = []
    for b in content if isinstance(content, list) else []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t in ("input_text", "output_text", "text"):
            parts.append({"type": "text", "text": str(b.get("text", ""))})
        elif t == "refusal":
            parts.append({"type": "text", "text": str(b.get("refusal", ""))})
        elif t == "input_image":
            part = _image_part(b) if images else None
            parts.append(part or {"type": "text", "text": "[image ignorée]"})
        elif t == "input_file":
            parts.append({"type": "text", "text": "[fichier ignoré]"})
    return parts


def _content_of(parts: list[dict]):
    """Une chaîne si tout est texte — la forme que tous les backends
    acceptent —, la liste de parties sinon."""
    if all(p["type"] == "text" for p in parts):
        return "".join(p["text"] for p in parts)
    return parts


def _text_of(parts: list[dict]) -> str:
    return "".join(p["text"] for p in parts if p["type"] == "text")


def _prepend_text(msg: dict, text: str) -> None:
    content = msg.get("content")
    if isinstance(content, list):
        content.insert(0, {"type": "text", "text": text + "\n\n"})
    elif isinstance(content, str) and content:
        msg["content"] = text + "\n\n" + content
    else:
        msg["content"] = text


def has_images(p: dict) -> bool:
    """Y a-t-il au moins une image dans la requête (messages, ou sortie
    d'outil) ? Évite de charger un catalogue pour rien."""
    items = p.get("input")
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        for key in ("content", "output"):
            blocks = it.get(key)
            if isinstance(blocks, list) and any(
                    isinstance(b, dict) and b.get("type") == "input_image"
                    for b in blocks):
                return True
    return False


def _replayed_call(it: dict, hosted, client: str = "") -> tuple[str, str, str] | None:
    """Élément d'outil hébergé rejoué par le client (web_search_call…)
    → (nom, arguments, résultat), ou None s'il n'est pas des nôtres. La
    mémoire rend l'appel tel qu'il a eu lieu ; si elle l'a perdu, il est
    reconstruit depuis son action, avec un résultat qui le dit. `client` :
    à qui la mémoire doit l'entrée — celle d'un autre client n'existe pas
    pour celui-ci, l'appel est reconstruit comme s'il était perdu."""
    entry = hosted.memory.recall(str(it.get("id") or ""), client)
    if entry is not None:
        return entry["name"], entry["arguments"], entry["result"]
    tool = hosted.for_item(it)
    if tool is None:
        return None
    args = {k: v for k, v in it["action"].items() if k != "type"}
    return tool.name, json.dumps(args, ensure_ascii=False), hosted.expired


def _custom_arguments(text) -> str:
    """L'entrée texte d'un `custom_tool_call` rejoué → arguments de la
    fonction. Toujours la même sérialisation : le préfixe ne bouge pas
    d'un tour à l'autre."""
    return json.dumps({"input": text if isinstance(text, str) else ""},
                      ensure_ascii=False)


def _shell_arguments(action) -> str:
    """L'action d'un `local_shell_call` rejoué → arguments de la fonction :
    ses champs renseignés, dans un ordre fixe."""
    action = action if isinstance(action, dict) else {}
    return json.dumps({k: action[k] for k in SHELL_FIELDS
                       if action.get(k) not in (None, {}, [])},
                      ensure_ascii=False)


# Sortie d'un appel exécuté par le client, quelle que soit la forme de
# l'appel : toutes deviennent un message `tool`.
OUTPUT_ITEMS = ("function_call_output", "custom_tool_call_output",
                "local_shell_call_output")


def _messages(p: dict, images: bool, ctx: Context, hosted=None) -> list[dict]:
    """`instructions` + `input` → messages chat/completions.

    Les messages `developer` / `system` QUI OUVRENT la conversation
    (Codex y met ses consignes de bac à sable et ses skills) rejoignent
    `instructions` dans l'unique message system de tête : beaucoup de
    gabarits de chat (Qwen, Mistral…) n'en acceptent qu'un, en tête.
    Ceux qui arrivent EN COURS de conversation suivent la règle de la
    surface Anthropic (voir anthropic_api.to_openai) : fondus en tête du
    message user qui suit, ou posés en message user à leur place quand
    c'est un assistant qui suit — même position d'un tour à l'autre, le
    préfixe rendu reste stable pour un backend à cache de préfixe."""
    system: list[str] = []
    if isinstance(p.get("instructions"), str) and p["instructions"]:
        system.append(p["instructions"])
    messages: list[dict] = []
    pending: list[str] = []      # system/developer en cours de conversation
    media: list[dict] = []       # images de sorties d'outil, après les `tool`
    opened = False               # un élément non system a-t-il été vu ?
    shell_ids: dict[str, str] = {}   # id d'un local_shell_call → son call_id

    def flush_media() -> None:
        if media:
            messages.append({"role": "user", "content": list(media)})
            media.clear()

    def flush_pending() -> None:
        if pending:
            messages.append({"role": "user", "content": "\n\n".join(pending)})
            pending.clear()

    def call(call_id: str, name: str, arguments: str) -> None:
        """Un appel d'outil de l'assistant. Texte puis appels du même
        tour : UN message assistant, la forme que chat/completions attend."""
        nonlocal opened
        opened = True
        flush_media()
        entry = {"id": call_id, "type": "function",
                 "function": {"name": name, "arguments": arguments}}
        last = messages[-1] if messages else None
        if last and last["role"] == "assistant":
            last.setdefault("tool_calls", []).append(entry)
        else:
            flush_pending()
            messages.append({"role": "assistant", "content": None,
                             "tool_calls": [entry]})

    items = p.get("input")
    if isinstance(items, str):
        items = [{"type": "message", "role": "user", "content": items}]
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        kind = it.get("type") or ("message" if it.get("role") else None)
        if kind == "message":
            role = it.get("role")
            parts = _parts(it.get("content"), images)
            if role in ("system", "developer"):
                text = _text_of(parts)
                if text:
                    (pending if opened else system).append(text)
                continue
            opened = True
            flush_media()
            if role == "assistant":
                # Sans texte, rien à poser : l'appel d'outil qui suit
                # ouvre lui-même son message assistant.
                if _text_of(parts):
                    flush_pending()
                    messages.append({"role": "assistant",
                                     "content": _text_of(parts)})
            elif role == "user":
                msg = {"role": "user", "content": _content_of(parts)}
                if pending:
                    _prepend_text(msg, "\n\n".join(pending))
                    pending.clear()
                messages.append(msg)
        elif kind == "function_call":
            call(str(it.get("call_id") or it.get("id") or _id("call")),
                 str(it.get("name", "")),
                 it.get("arguments")
                 if isinstance(it.get("arguments"), str) else "{}")
        elif kind == "custom_tool_call":
            # Appel d'un outil `custom` : la fonction qui en tient lieu,
            # son texte remis dans `input`.
            call(str(it.get("call_id") or it.get("id") or _id("call")),
                 ctx.custom_function(str(it.get("name", ""))),
                 _custom_arguments(it.get("input")))
        elif kind == "local_shell_call":
            call_id = str(it.get("call_id") or it.get("id") or _id("call"))
            if it.get("id"):
                shell_ids[str(it["id"])] = call_id
            call(call_id, ctx.shell or "local_shell",
                 _shell_arguments(it.get("action")))
        elif kind in OUTPUT_ITEMS:
            opened = True
            output = it.get("output")
            call_id = str(it.get("call_id") or "")
            if kind == "local_shell_call_output" and not call_id:
                # L'API y nomme `id` ce qui est le `call_id` de l'appel ;
                # un client qui y met l'id de l'ÉLÉMENT est suivi aussi.
                call_id = str(it.get("id") or "")
                call_id = shell_ids.get(call_id, call_id)
            if isinstance(output, str):
                text = output
            else:
                # Un `tool` OpenAI n'a qu'un contenu TEXTE : les images
                # d'une sortie d'outil suivent dans un message user.
                parts = _parts(output, images)
                text = _text_of(parts)
                extra = [x for x in parts if x["type"] != "text"]
                if extra:
                    media.append({"type": "text",
                                  "text": f"[résultat de l'outil {call_id}]"})
                    media.extend(extra)
            messages.append({"role": "tool", "tool_call_id": call_id,
                             "content": text})
        elif kind == "reasoning":
            continue
        elif hosted and (replayed := _replayed_call(it, hosted, ctx.client)):
            # Appel que le proxy avait exécuté : il redevient un appel
            # suivi de son résultat, comme pendant la boucle (tool_loop.py
            # reconstruit ses tours par ce même chemin).
            name, arguments, result = replayed
            call(str(it.get("id")), name, arguments)
            messages.append({"role": "tool", "tool_call_id": str(it.get("id")),
                             "content": result})
        elif kind == "item_reference":
            raise Refused(
                "`item_reference` renvoie à un élément conservé côté "
                "serveur : ce proxy ne conserve rien, renvoyer l'élément")
        else:
            # Trace d'un outil hébergé (web_search_call…) ou élément sans
            # équivalent : écarté.
            ctx.ignored.append(str(kind))
    flush_media()
    flush_pending()
    if system:
        messages.insert(0, {"role": "system", "content": "\n\n".join(system)})
    return messages


def to_chat(p: dict, images: bool = False, hosted=None,
            client: str = "") -> tuple[dict, Context]:
    """Corps /v1/responses → corps /v1/chat/completions, et le contexte
    dont la réponse aura besoin. `model` est recopié tel quel : l'appelant
    route sur son préfixe. `hosted` : l'annuaire des outils que le proxy
    exécute (tools.Hosted), ou None. `client` : à qui appartiennent les
    résultats de la mémoire (tools.owner de sa clé ; «» = proxy ouvert,
    un seul client). Lève Refused pour ce qui ne peut pas être honoré."""
    for key in ("previous_response_id", "conversation"):
        if p.get(key):
            raise Refused(
                f"`{key}` suppose une conversation conservée côté serveur : "
                f"ce proxy ne conserve rien, renvoyer l'historique dans `input`")
    if p.get("background"):
        raise Refused("`background` n'est pas pris en charge")

    ctx = Context(p)
    ctx.memory = hosted.memory if hosted else None
    ctx.client = client
    # Les outils d'abord : le rejeu d'un `custom_tool_call` a besoin du nom
    # de la fonction qui tient lieu de l'outil.
    tools = _tools(p.get("tools"), ctx, hosted)
    out: dict = {"model": p.get("model", "")}
    out["messages"] = _messages(p, images, ctx, hosted)

    if isinstance(p.get("max_output_tokens"), int):
        out["max_tokens"] = p["max_output_tokens"]
    for key in ("temperature", "top_p", "user"):
        if p.get(key) is not None:
            out[key] = p[key]
    reasoning = p.get("reasoning")
    if isinstance(reasoning, dict) and isinstance(reasoning.get("effort"), str):
        out["reasoning_effort"] = reasoning["effort"]
    text = p.get("text")
    fmt = text.get("format") if isinstance(text, dict) else None
    if isinstance(fmt, dict):
        if fmt.get("type") == "json_schema" and isinstance(fmt.get("schema"), dict):
            schema = {"name": fmt.get("name") or "response",
                      "schema": fmt["schema"]}
            if fmt.get("strict") is True:
                schema["strict"] = True
            out["response_format"] = {"type": "json_schema",
                                      "json_schema": schema}
        elif fmt.get("type") == "json_object":
            out["response_format"] = {"type": "json_object"}
    if p.get("stream"):
        out["stream"] = True
        # Sans lui, un flux SSE OpenAI ne porte aucun `usage` : les stats
        # retomberaient sur l'estimation, et le client ne saurait rien.
        out["stream_options"] = {"include_usage": True}

    if tools:
        out["tools"] = tools
        _tool_choice(p.get("tool_choice"), out, ctx)
        if isinstance(p.get("parallel_tool_calls"), bool):
            out["parallel_tool_calls"] = p["parallel_tool_calls"]
    return out, ctx
