"""
La surface Responses du proxy : ce qu'il faut pour qu'un client écrit
pour l'API Responses d'OpenAI — Codex CLI en premier lieu — parle à un
backend qui ne connaît que /v1/chat/completions. UNIQUEMENT dans ce
sens, comme pour la surface Anthropic : aucun backend n'a besoin de
servir /v1/responses, et le proxy ne dépend pas de ce que chacun en
implémente.

Ce qu'un client Responses appelle, et ce qu'il reçoit :
  POST /v1/responses  → traduit en /v1/chat/completions, réponse
                        retraduite (objet Response, ou flux d'événements
                        `response.*`)

Le routage ne change pas : le modèle porte le préfixe du backend
(`bigchuck/qwen3.8-flash-next`), à poser dans la configuration du client.

La tolérance de la requête reprend celle de gufo (gufo-org/gufo#434),
écrite sur les corps que Codex envoie réellement :
  * les outils que seul OpenAI exécute (`web_search`, `file_search`,
    `code_interpreter`, `mcp`…) sont ignorés, les `function` restent —
    sauf ceux que le proxy HÉBERGE lui-même (voir plus bas) ;
  * un `namespace` n'est PAS un outil hébergé : c'est un groupement côté
    client, ses fonctions sont aplaties dans la liste et appelées par
    leur nom simple — le `namespace` d'origine est reposé sur l'appel
    rendu au client, qui s'en sert pour router ; un nom présent deux fois
    (deux namespaces, ou un namespace et le premier niveau) est refusé ;
  * `custom` et `local_shell` ne sont pas hébergés non plus : ce sont des
    outils que le CLIENT exécute, sous une forme que chat/completions n'a
    pas (entrée en texte libre ; élément `local_shell_call`). Chacun est
    présenté au modèle comme une fonction, et son appel rendu au client
    dans sa forme d'origine — voir « Outils du client hors `function` » ;
  * les champs sans effet ici (`include`, `reasoning.summary`,
    `text.verbosity`, `prompt_cache_key`, `client_metadata`, `store`…)
    sont tolérés : le corps upstream est RECONSTRUIT, rien d'inconnu ne
    part vers un backend qui le refuserait ;
  * les éléments `reasoning` rejoués sont jetés, comme les blocs
    `thinking` d'un client Anthropic : aucun backend OpenAI ne les rejoue.

Ce qui demanderait un état côté serveur est refusé (400) plutôt que
perdu en silence : `previous_response_id`, `conversation`, `background`,
`item_reference`. Codex renvoie tout l'historique à chaque tour.

Outils hébergés (paquet tools/) : quand le proxy sait exécuter un outil
que le client déclare — `web_search`, `image_generation` —, il présente au modèle les
fonctions correspondantes, et leurs appels ne sont PAS rendus au client
en `function_call` : le robinet les met de côté (`pending`), app.py les
exécute et relance le backend, et le client ne voit qu'un élément
`web_search_call` terminé. Au tour suivant le client renvoie cet élément
sans son résultat : il est relu dans la mémoire du paquet tools/ et
redevient, à l'identique, un appel suivi de son résultat (pour un
`image_generation_call`, que le client renvoie AVEC son image : le texte
que le modèle avait lu, jamais l'image). Ce module ne
fait aucune requête : il reçoit un objet `Hosted` et s'en sert comme
d'un annuaire.

Outils du client hors `function` (formes lues dans le code de Codex CLI
rust-v0.157.1 — protocol/src/models.rs, tools/src/responses_api.rs,
codex-api/src/sse/responses.rs — et dans les types du SDK openai-python) :
  * `{"type": "custom", name, description, format}` — outil « freeform »,
    dont l'entrée est un TEXTE, pas du JSON (Codex : `apply_patch`, avec
    une grammaire lark, pour les modèles dont le catalogue dit
    `apply_patch_tool_type = "freeform"`). Devient une fonction à un seul
    champ `input` ; la grammaire rejoint la description, c'est tout ce
    qu'un backend chat peut en faire. L'appel est rendu en
    `custom_tool_call` (`call_id`, `name`, `input`), rejoué par le client
    avec un `custom_tool_call_output` ;
  * `{"type": "local_shell"}` — devient une fonction `local_shell`
    (`command` en tableau d'arguments…), l'appel est rendu en
    `local_shell_call` avec son `action` `exec`, rejoué avec un
    `local_shell_call_output` (SDK) ou un `function_call_output` (Codex).
    Codex 0.157.1 ne DÉCLARE plus cet outil (son `ToolSpec` n'en a plus la
    variante) : il n'est là que pour un autre client Responses.
La fonction prend le nom de l'outil, ou ce nom suffixé (`_2`…) si une
fonction du client le porte déjà : c'est la fonction du client qui garde
le sien.

Même robinet que anthropic_api.Translator (feed / finish / tokens /
cached / sse / ok) : un flux OpenAI (deltas plats, outils fragmentés par
index) devient la séquence d'événements Responses, éléments ouverts et
fermés un à un, numérotés (`sequence_number`).

Ce module ne connaît ni FastAPI ni httpx.
"""

import json
import time
import uuid

from . import config

# Absente du TOML = surface inactive : un déploiement existant n'expose
# rien de nouveau sans l'avoir demandé.
ENABLED = config.flag("responses.enabled", False)
# `reasoning_content` d'un backend → élément `reasoning` (résumé) pour le
# client. Codex le renvoie au tour suivant, on le jette à la traduction.
REASONING_AS_SUMMARY = config.flag("responses.reasoning_as_summary", True)
CHARS_PER_TOKEN = 4

# finish_reason OpenAI → raison d'une réponse `incomplete`. Tout le reste
# est une réponse `completed`.
INCOMPLETE_REASONS = {
    "length": "max_output_tokens",
    "content_filter": "content_filter",
}


class Refused(Exception):
    """Requête qu'on ne sait pas honorer : 400, avec ce message."""


class Context:
    """Ce que la réponse doit savoir de la requête : le modèle tel que le
    client l'a demandé (préfixé), le namespace d'origine de chaque
    fonction aplatie, et les champs que l'objet Response renvoie en écho
    (pas `instructions` : 17 000 caractères chez Codex, qu'il faudrait
    recopier trois fois par réponse en flux).
    `ignored` : types d'outils et d'éléments écartés, pour le log."""

    def __init__(self, request: dict):
        self.model = str(request.get("model", "") or "")
        self.namespaces: dict[str, str] = {}
        self.ignored: list[str] = []
        # Fonctions exécutées par le proxy : nom → module de tools/.
        self.hosted: dict = {}
        # Par nom de fonction, ce que le client a réglé sur son outil
        # hébergé (`size` d'`image_generation`) : passé à l'exécution.
        self.options: dict[str, dict] = {}
        self.memory = None
        # Le client, pour la mémoire des résultats : tools.owner() de la
        # clé présentée au proxy, «» pour un proxy ouvert.
        self.client = ""
        # Outils du CLIENT présentés au modèle comme des fonctions : nom de
        # la fonction → (nom de l'outil `custom`, son namespace ou None),
        # et le nom de la fonction qui tient lieu de `local_shell`.
        self.custom: dict[str, tuple[str, str | None]] = {}
        self.shell: str | None = None
        self.echo = {
            k: request.get(k) for k in (
                "max_output_tokens", "metadata", "temperature", "top_p")
        }
        tools = request.get("tools")
        self.echo["tools"] = tools if isinstance(tools, list) else []
        self.echo["tool_choice"] = request.get("tool_choice") or "auto"
        self.echo["parallel_tool_calls"] = bool(
            request.get("parallel_tool_calls", True))

    def form(self, function: str) -> str:
        """Sous quelle forme l'appel de cette fonction est rendu au client :
        `custom`, `local_shell`, ou `function` pour tout le reste."""
        if function in self.custom:
            return "custom"
        return "local_shell" if function == self.shell else "function"

    def custom_function(self, tool: str) -> str:
        """La fonction qui tient lieu de l'outil `custom` de ce nom — le nom
        lui-même s'il n'est pas (ou plus) déclaré : un appel rejoué garde
        sa place dans l'historique."""
        return next((fn for fn, (name, _) in self.custom.items()
                     if name == tool), tool)


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
    out: list[dict] = []
    seen: set[str] = set()
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
            modules = [m for m in (hosted.for_kind(kind) if hosted else [])
                       if m.NAME not in client and m.NAME not in seen]
            if modules:
                # Outil que le proxy héberge : ses fonctions à la place.
                # web_search renvoie à web_fetch dans sa description : sans
                # lui (désactivé, ou nom pris par le client), la variante
                # qui n'en parle pas.
                fetch = any(x.NAME == "web_fetch" for x in modules)
                for m in modules:
                    seen.add(m.NAME)
                    ctx.hosted[m.NAME] = m
                    if hasattr(m, "options"):
                        ctx.options[m.NAME] = m.options(t)
                    out.append(m.definition(fetch=fetch) if hasattr(
                        m, "definition") else m.DEFINITION)
                continue
            # Outil hébergé par OpenAI seul (file_search, code_interpreter,
            # mcp…) : rien à traduire.
            ctx.ignored.append(str(kind))
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
    module = hosted.for_item(it)
    if module is None:
        return None
    if hasattr(module, "replay"):
        # Élément sans action (image_generation_call) : le module sait le
        # relire — son prompt, jamais son image.
        return (module.NAME, *module.replay(it))
    args = {k: v for k, v in it["action"].items() if k != "type"}
    return module.NAME, json.dumps(args, ensure_ascii=False), hosted.expired


def _custom_arguments(text) -> str:
    """L'entrée texte d'un `custom_tool_call` rejoué → arguments de la
    fonction. Toujours la même sérialisation : le préfixe ne bouge pas
    d'un tour à l'autre."""
    return json.dumps({"input": text if isinstance(text, str) else ""},
                      ensure_ascii=False)


def _custom_input(arguments: str) -> str:
    """Arguments écrits par le modèle → entrée texte de l'outil `custom`.
    Un JSON invalide ou tronqué, ou sans `input`, est rendu TEL QUEL : un
    modèle qui suit la description d'origine à la lettre (« do not wrap
    the patch in JSON ») écrit son texte nu, et c'est alors le bon ; sinon
    c'est l'outil du client qui le refusera, avec un message que le modèle
    lira."""
    try:
        args = json.loads(arguments)
    except ValueError:
        return arguments
    if isinstance(args, dict) and isinstance(args.get("input"), str):
        return args["input"]
    return args if isinstance(args, str) else arguments


# Champs de l'action `exec`, dans l'ordre où ils sont rejoués.
SHELL_FIELDS = ("command", "working_directory", "timeout_ms", "env", "user")


def _shell_action(arguments: str) -> tuple[dict, bool]:
    """Arguments écrits par le modèle → (action `exec`, valide ?). Une
    commande en CHAÎNE (erreur courante) passe par `bash -lc` ; sans
    commande exploitable — JSON invalide, tronqué, champ absent — l'action
    est vide et l'appelant rend l'élément `incomplete` plutôt que de
    casser le flux : le client répondra, le modèle corrigera."""
    try:
        args = json.loads(arguments or "{}")
    except ValueError:
        args = None
    args = args if isinstance(args, dict) else {}
    command = args.get("command")
    if isinstance(command, str) and command.strip():
        command = ["bash", "-lc", command]
    ok = isinstance(command, list) and bool(command) \
        and all(isinstance(x, str) for x in command)
    env, timeout = args.get("env"), args.get("timeout_ms")
    text = lambda v: v if isinstance(v, str) and v else None
    return {
        "type": "exec",
        "command": command if ok else [],
        "env": {str(k): str(v) for k, v in env.items()}
        if isinstance(env, dict) else {},
        "timeout_ms": timeout if isinstance(timeout, int)
        and not isinstance(timeout, bool) else None,
        "user": text(args.get("user")),
        "working_directory": text(args.get("working_directory")),
    }, ok


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
            # suivi de son résultat, comme pendant la boucle (app.py
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


# ── Réponse : OpenAI chat → Responses ───────────────────────────────────

def _id(prefix: str) -> str:
    return f"{prefix}_" + uuid.uuid4().hex[:24]


def _cached(u) -> int:
    details = u.get("prompt_tokens_details") if isinstance(u, dict) else None
    if isinstance(details, dict) and isinstance(details.get("cached_tokens"), int):
        return max(details["cached_tokens"], 0)
    return 0


def _usage(u) -> dict:
    """Usage Responses. `input_tokens` INCLUT les tokens lus en cache,
    comme `prompt_tokens` côté chat : rien à soustraire (au contraire de
    la surface Anthropic)."""
    u = u if isinstance(u, dict) else {}
    prompt = int(u.get("prompt_tokens") or 0)
    completion = int(u.get("completion_tokens") or 0)
    details = u.get("completion_tokens_details")
    reasoning = details.get("reasoning_tokens") if isinstance(details, dict) else 0
    return {
        "input_tokens": prompt,
        "input_tokens_details": {"cached_tokens": _cached(u)},
        "output_tokens": completion,
        "output_tokens_details": {
            "reasoning_tokens": reasoning if isinstance(reasoning, int) else 0},
        "total_tokens": prompt + completion,
    }


def _text_part(text: str) -> dict:
    return {"type": "output_text", "text": text, "annotations": [],
            "logprobs": []}


def _message_item(item_id: str, text: str, status: str) -> dict:
    return {"id": item_id, "type": "message", "status": status,
            "content": [_text_part(text)] if status == "completed" else [],
            "role": "assistant"}


def _reasoning_item(item_id: str, text: str | None) -> dict:
    return {"id": item_id, "type": "reasoning",
            "summary": [] if text is None
            else [{"type": "summary_text", "text": text}]}


def _call_item(item_id: str, call_id: str, name: str, arguments: str,
               status: str, ctx: Context) -> dict:
    item = {"id": item_id, "type": "function_call", "call_id": call_id,
            "name": name, "arguments": arguments, "status": status}
    if name in ctx.namespaces:
        item["namespace"] = ctx.namespaces[name]
    return item


# Les éléments par lesquels la main revient au CLIENT, et le préfixe de
# leur id (ceux de Codex : protocol/src/models.rs).
CLIENT_ITEMS = {"function": ("function_call", "fc"),
                "custom": ("custom_tool_call", "ctc"),
                "local_shell": ("local_shell_call", "lsh")}
CLIENT_CALLS = tuple(kind for kind, _ in CLIENT_ITEMS.values())


def _client_call(ctx: Context, call_id: str, name: str, arguments: str,
                 item_id: str | None = None) -> dict:
    """L'élément TERMINÉ d'un appel que le client exécutera, dans la forme
    de l'outil qu'il a déclaré : `function_call`, `custom_tool_call` (les
    arguments redeviennent un texte) ou `local_shell_call` (une action)."""
    form = ctx.form(name)
    item_id = item_id or _id(CLIENT_ITEMS[form][1])
    if form == "custom":
        tool, namespace = ctx.custom[name]
        item = {"id": item_id, "type": "custom_tool_call",
                "status": "completed", "call_id": call_id, "name": tool,
                "input": _custom_input(arguments)}
        if namespace:
            item["namespace"] = namespace
        return item
    if form == "local_shell":
        action, ok = _shell_action(arguments)
        return {"id": item_id, "type": "local_shell_call", "call_id": call_id,
                "status": "completed" if ok else "incomplete",
                "action": action}
    return _call_item(item_id, call_id, name, arguments, "completed", ctx)


def _response(ctx: Context, rid: str, created: int, status: str,
              output: list[dict], usage, finish: str | None = None,
              error: dict | None = None) -> dict:
    reason = INCOMPLETE_REASONS.get(finish)
    return {
        "id": rid,
        "object": "response",
        "created_at": created,
        "model": ctx.model,
        "status": status,
        "error": error,
        "incomplete_details": {"reason": reason}
        if status == "incomplete" and reason else None,
        "usage": usage,
        "output": output,
        "store": False,
        "previous_response_id": None,
        **ctx.echo,
    }


def _final_status(finish: str | None) -> str:
    return "incomplete" if finish in INCOMPLETE_REASONS else "completed"


# Ce que l'API Responses fixe pour chaque type d'élément hébergé : le
# préfixe de son id, et l'événement qui dit que l'exécution est en cours
# (`response.<type>.<étape>`, entre `in_progress` et `completed`).
HOSTED_ITEMS = {"web_search_call": ("ws", "searching"),
                "image_generation_call": ("ig", "generating")}


def _hosted_id(module) -> str:
    return _id(HOSTED_ITEMS[module.ITEM_TYPE][0])


def _hosted_item(item_id: str, module, arguments: str | None = None,
                 result: str = "") -> dict:
    """Élément qui rend compte d'un appel exécuté par le proxy
    (`web_search_call`, `image_generation_call`). `arguments` None = appel
    en cours : ni action ni résultat. Terminé, le client n'en voit que
    l'action — ou, pour un module à `item`, ce que celui-ci tire du
    résultat (l'image, et un `status` qui peut être `failed`)."""
    item = {"id": item_id, "type": module.ITEM_TYPE,
            "status": "in_progress" if arguments is None else "completed"}
    if arguments is not None:
        try:
            args = json.loads(arguments or "{}")
        except ValueError:
            args = None
        args = args if isinstance(args, dict) else {}
        if hasattr(module, "item"):
            item.update(module.item(args, result))
        else:
            item["action"] = module.action(args)
    return item


def _pending(item_id: str, name: str, arguments: str, index: int) -> dict:
    """Un appel hébergé à exécuter : ce que Translator.pending contient.
    `arguments` : tels que le modèle les a écrits (c'est ce texte-là que
    la mémoire garde, et que le rejeu rend) ; `index` : la place réservée
    à l'élément dans `output`."""
    return {"item_id": item_id, "name": name, "arguments": arguments,
            "index": index}


def _chat_items(msg: dict, ctx: Context, base: int = 0) -> tuple[list[dict], list[dict]]:
    """Message d'une réponse non streamée → (éléments de `output`, appels
    hébergés à exécuter). Un appel dont le nom est dans ctx.hosted donne
    un élément en cours et une entrée d'attente, pas un `function_call`.
    `base` : nombre d'éléments déjà dans `output` (tours précédents)."""
    output: list[dict] = []
    pending: list[dict] = []
    reasoning = msg.get("reasoning_content") or msg.get("reasoning")
    if REASONING_AS_SUMMARY and isinstance(reasoning, str) and reasoning:
        output.append(_reasoning_item(_id("rs"), reasoning))
    if isinstance(msg.get("content"), str) and msg["content"].strip():
        output.append(_message_item(_id("msg"), msg["content"], "completed"))
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        name = str(fn.get("name", ""))
        arguments = args if isinstance(args, str) else json.dumps(args or {})
        if name in ctx.hosted:
            item_id = _hosted_id(ctx.hosted[name])
            pending.append(_pending(item_id, name, arguments or "{}",
                                    base + len(output)))
            output.append(_hosted_item(item_id, ctx.hosted[name]))
            continue
        output.append(_client_call(
            ctx, str(tc.get("id") or _id("call")), name, arguments))
    return output, pending


def from_chat(doc: dict, ctx: Context) -> dict:
    """Réponse non streamée /v1/chat/completions → objet Response. Sans
    outil hébergé (le robinet, lui, les met de côté : Translator.finish)."""
    choice = (doc.get("choices") or [{}])[0]
    output, _ = _chat_items(choice.get("message") or {}, ctx)
    finish = choice.get("finish_reason")
    created = doc.get("created")
    return _response(ctx, _id("resp"),
                     created if isinstance(created, int) else int(time.time()),
                     _final_status(finish), output, _usage(doc.get("usage")),
                     finish)


def error_body(doc, status: int) -> dict:
    """Erreur upstream → {"error": {message, type}}, la forme qu'un client
    OpenAI attend. Un corps déjà à cette forme passe tel quel."""
    if isinstance(doc, dict) and isinstance(doc.get("error"), dict):
        return doc
    message = ""
    if isinstance(doc, dict):
        err = doc.get("error")
        message = err if isinstance(err, str) \
            else str(doc.get("message") or doc.get("detail") or "")
    elif isinstance(doc, str):
        message = doc
    return {"error": {"message": message or f"upstream HTTP {status}",
                      "type": "upstream_error"}}


class Translator:
    """Le robinet de réponse pour /v1/responses : même interface que
    stats.UsageCollector et anthropic_api.Translator (feed / finish /
    tokens / cached / sse / ok), mais les octets rendus sont la réponse
    Responses, pas ceux de l'upstream.

    Trois modes, fixés à l'ouverture par le statut et le content-type
    upstream :
      * erreur (statut ≠ 2xx) : le corps est bufferisé, finish() rend
        une erreur à la forme OpenAI ;
      * JSON : bufferisé, finish() rend l'objet Response traduit ;
      * SSE : traduit au fil de l'eau, événement par événement.

    Outils hébergés : UNE réponse peut couvrir PLUSIEURS tours upstream.
    Un appel dont le nom est dans ctx.hosted n'est pas rendu au client :
    il est rangé dans `pending`, et finish() ne clôt alors PAS la réponse
    (pas de `response.completed`, b"" en JSON). L'appelant (app.py)
    exécute chaque appel et en rend le compte par resolve(), puis soit
    next_turn() et un nouveau flux upstream dans feed()/finish(), soit
    finalize(). `pending` vide après finish() = réponse close. L'identité
    de la réponse (id, `sequence_number`, `output`) traverse les tours ;
    l'usage est CUMULÉ.
    """

    def __init__(self, status: int, content_type: str, ctx: Context):
        ct = (content_type or "").lower()
        self.ok = 200 <= status < 300
        self.status = status
        self.sse = self.ok and "text/event-stream" in ct
        self.ctx = ctx
        self._buf = bytearray()
        self.usage: dict | None = None    # usage upstream du tour en cours
        self._past: list[dict] = []       # usages des tours précédents
        self.out_chars = 0
        # Appels hébergés du tour, à exécuter : voir _pending().
        self.pending: list[dict] = []
        # Appels CLIENT (`function_call`) rendus pendant le tour en cours :
        # s'il y en a, la main revient au client, pas de tour suivant.
        self.client_calls = 0
        self.turns = 1
        # État du flux.
        self._started = False
        self._finished = False
        self._seq = 0
        self._rid = _id("resp")
        self._created = int(time.time())
        self._output: list[dict] = []     # éléments terminés, dans l'ordre
        self._open: str | None = None     # "text" | "reasoning" | "tool" | "hosted"
        self._item_id = ""
        self._text: list[str] = []        # texte ou arguments de l'élément ouvert
        self._call: tuple[str, str] = ("", "")   # (call_id, name) de l'outil ouvert
        self._tool_index: int | None = None      # index OpenAI de l'outil ouvert
        self._closed_tools: set[int] = set()
        self._finish_reason: str | None = None
        self._blank = ""                  # blancs retenus avant un texte

    # ── interface robinet ──
    def feed(self, chunk: bytes) -> bytes:
        self._buf += chunk
        if not self.sse:
            return b""
        out = bytearray()
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                break
            line = bytes(self._buf[:nl]).rstrip(b"\r")
            del self._buf[:nl + 1]
            if line.startswith(b"data:"):
                out += self._data(line[5:].strip())
        return bytes(out)

    def finish(self) -> bytes:
        if self.sse:
            return self._end()
        body = bytes(self._buf)
        self._buf.clear()
        try:
            doc = json.loads(body) if body else {}
        except ValueError:
            doc = body.decode("utf-8", "replace")
        if not self.ok:
            return json.dumps(error_body(doc, self.status),
                              ensure_ascii=False).encode()
        if not isinstance(doc, dict):
            if self.turns > 1:
                return self.fail("réponse upstream illisible")
            return json.dumps(error_body("réponse upstream illisible",
                                         self.status)).encode()
        self.usage = doc.get("usage") if isinstance(doc.get("usage"), dict) \
            else None
        choice = (doc.get("choices") or [{}])[0]
        items, pending = _chat_items(choice.get("message") or {}, self.ctx,
                                     len(self._output))
        self._output += items
        self.pending += pending
        self.client_calls = sum(
            1 for item in items if item["type"] in CLIENT_CALLS)
        self._finish_reason = choice.get("finish_reason")
        if self.turns == 1 and isinstance(doc.get("created"), int):
            self._created = doc["created"]
        self.out_chars += sum(
            len(part.get("text", "")) for item in items
            for part in item.get("content") or [])
        if self.pending:
            # Appels hébergés : la réponse n'est pas finie, rien ne part.
            return b""
        return self.finalize()

    # ── outils hébergés : plusieurs tours upstream pour une réponse ──
    @property
    def output(self) -> list[dict]:
        """Les éléments de la réponse, dans l'ordre : c'est avec eux,
        ajoutés à l'`input` d'origine, que le tour suivant se reconstruit
        (to_chat les relit comme il relira ceux que le client rejouera)."""
        return list(self._output)

    def resolve(self, call: dict, result: str) -> bytes:
        """Le résultat d'un appel de `pending`, exécuté par l'appelant :
        rangé en mémoire sous l'id de l'élément (le client le rejouera
        sans son résultat), et l'élément est clos — le client n'en voit
        que l'action, ou ce que le module tire du résultat (une image).
        La mémoire ne garde que le TEXTE (`str`) : un résultat qui porte
        une image la laisserait sinon en mémoire vive jusqu'à expiration."""
        module = self.ctx.hosted[call["name"]]
        self.ctx.memory.store(call["item_id"], call["name"],
                              call["arguments"], str(result),
                              self.ctx.client)
        item = _hosted_item(call["item_id"], module, call["arguments"], result)
        self._output[call["index"]] = item
        self.pending = [c for c in self.pending if c is not call]
        if not self.sse:
            return b""
        at = {"output_index": call["index"], "item_id": call["item_id"]}
        # Pas d'événement `.completed` pour un élément en échec : seul
        # `output_item.done` le porte, avec son `status`.
        done = self._event(f"response.{module.ITEM_TYPE}.completed", at) \
            if item["status"] == "completed" else b""
        return done + self._event("response.output_item.done", {
            "output_index": call["index"], "item": item})

    def next_turn(self) -> None:
        """Avant de recevoir le flux upstream suivant : l'état propre au
        tour repart de zéro (les index d'outils OpenAI recommencent à 0),
        l'identité de la réponse reste."""
        if isinstance(self.usage, dict):
            self._past.append(self.usage)
        self.usage = None
        self._buf.clear()
        self._open, self._text = None, []
        self._blank = ""
        self._tool_index = None
        self._closed_tools = set()
        self._finish_reason = None
        self.client_calls = 0
        self.turns += 1

    def finalize(self) -> bytes:
        """Clôt la réponse : `response.completed` / `incomplete` en flux,
        le corps de l'objet Response en JSON. Sans effet si elle l'est déjà."""
        if self._finished:
            return b""
        if self.sse:
            return self._end(force=True)
        self._finished = True
        return json.dumps(self._snapshot(_final_status(self._finish_reason)),
                          ensure_ascii=False).encode()

    def fail(self, message: str, status: int = 500) -> bytes:
        """Échec d'un tour ULTÉRIEUR (quota, backend injoignable, statut
        d'erreur) : la réponse HTTP est déjà partie, il ne reste que
        `response.failed` en flux, ou un objet Response `failed` en JSON.
        `status` : sans effet ici (l'objet `failed` n'a qu'un code) ; le
        robinet Anthropic, piloté par la même boucle, en tire son type
        d'erreur."""
        if self._finished:
            return b""
        if self.sse:
            return self._fail({"message": message})
        self._finished = True
        self.pending = []
        return json.dumps(self._snapshot("failed", {
            "code": "server_error", "message": message}),
            ensure_ascii=False).encode()

    def _total(self) -> dict | None:
        """L'usage de la réponse, à la forme chat/completions : celui de
        l'upstream s'il n'y a eu qu'un tour, la SOMME sinon — chaque tour
        relit tout le préfixe, et c'est bien ce qui a été consommé."""
        turns = self._past + ([self.usage] if isinstance(self.usage, dict) else [])
        if len(turns) <= 1:
            return turns[0] if turns else None
        usages = [_usage(u) for u in turns]
        return {
            "prompt_tokens": sum(u["input_tokens"] for u in usages),
            "completion_tokens": sum(u["output_tokens"] for u in usages),
            "prompt_tokens_details": {"cached_tokens": sum(
                u["input_tokens_details"]["cached_tokens"] for u in usages)},
            "completion_tokens_details": {"reasoning_tokens": sum(
                u["output_tokens_details"]["reasoning_tokens"] for u in usages)},
        }

    def cached(self) -> int:
        return _cached(self._total())

    def tokens(self, fallback_prompt: int) -> tuple[int, int, bool]:
        u = self._total() or {}
        p, c = u.get("prompt_tokens"), u.get("completion_tokens")
        if isinstance(p, int) or isinstance(c, int):
            return (p if isinstance(p, int) else fallback_prompt,
                    c if isinstance(c, int) else _est(self.out_chars), True)
        return fallback_prompt, _est(self.out_chars), False

    # ── flux ──
    def _event(self, name: str, data: dict) -> bytes:
        data = {"type": name, **data, "sequence_number": self._seq}
        self._seq += 1
        return (f"event: {name}\ndata: "
                + json.dumps(data, ensure_ascii=False) + "\n\n").encode()

    def _snapshot(self, status: str, error: dict | None = None) -> dict:
        done = status != "in_progress"
        return _response(self.ctx, self._rid, self._created, status,
                         list(self._output),
                         _usage(self._total()) if done else None,
                         self._finish_reason, error)

    def _start(self, doc: dict) -> bytes:
        if self._started:
            return b""
        self._started = True
        if isinstance(doc.get("created"), int):
            self._created = doc["created"]
        return (self._event("response.created",
                            {"response": self._snapshot("in_progress")})
                + self._event("response.in_progress",
                              {"response": self._snapshot("in_progress")}))

    def _at(self) -> dict:
        """Où se place l'élément ouvert : son rang dans `output`, son id."""
        return {"output_index": len(self._output), "item_id": self._item_id}

    def _open_item(self, kind: str, item: dict, announce: bool = True) -> bytes:
        """`announce` faux : l'élément s'ouvre sans `output_item.added` —
        un `local_shell_call` n'a pas de forme sans son action, il n'est
        annoncé qu'à sa clôture."""
        out = self._close()
        self._blank = ""
        self._open, self._item_id, self._text = kind, item["id"], []
        if not announce:
            return out
        return out + self._event("response.output_item.added", {
            "output_index": len(self._output), "item": item})

    def _close(self) -> bytes:
        """Ferme l'élément ouvert : ses événements `done`, puis l'élément
        complet, rangé dans `output`."""
        if self._open is None:
            return b""
        at, text = self._at(), "".join(self._text)
        out = bytearray()
        if self._open == "hosted":
            # Appel que le proxy exécute : l'élément prend sa place dans
            # `output` (son index est réservé) mais reste en cours — il
            # sera clos par resolve(), une fois le résultat connu.
            _, name = self._call
            module = self.ctx.hosted[name]
            self.pending.append(_pending(self._item_id, name, text or "{}",
                                         len(self._output)))
            self._output.append(_hosted_item(self._item_id, module))
            if self._tool_index is not None:
                self._closed_tools.add(self._tool_index)
            self._tool_index = None
            self._open = None
            return self._event(
                f"response.{module.ITEM_TYPE}."
                f"{HOSTED_ITEMS[module.ITEM_TYPE][1]}", at)
        if self._open == "text":
            item = _message_item(self._item_id, text, "completed")
            out += self._event("response.output_text.done", {
                **at, "content_index": 0, "text": text, "logprobs": []})
            out += self._event("response.content_part.done", {
                **at, "content_index": 0, "part": _text_part(text)})
        elif self._open == "reasoning":
            item = _reasoning_item(self._item_id, text)
            out += self._event("response.reasoning_summary_text.done", {
                **at, "summary_index": 0, "text": text})
            out += self._event("response.reasoning_summary_part.done", {
                **at, "summary_index": 0,
                "part": {"type": "summary_text", "text": text}})
        else:
            call_id, name = self._call
            form = self.ctx.form(name)
            item = _client_call(self.ctx, call_id, name,
                                text or "{}" if form == "function" else text,
                                self._item_id)
            if form == "custom":
                # L'entrée n'est connue qu'une fois les arguments JSON
                # complets : UN delta qui la porte entière, puis `done`.
                # Codex ne lit que le delta (aperçu du patch en cours) et
                # prend l'appel dans `output_item.done`.
                if item["input"]:
                    out += self._event(
                        "response.custom_tool_call_input.delta",
                        {**at, "delta": item["input"]})
                out += self._event("response.custom_tool_call_input.done", {
                    **at, "input": item["input"]})
            elif form == "local_shell":
                out += self._event("response.output_item.added", {
                    "output_index": len(self._output),
                    "item": {**item, "status": "in_progress"}})
            else:
                out += self._event("response.function_call_arguments.done", {
                    **at, "name": name, "arguments": item["arguments"]})
            if self._tool_index is not None:
                self._closed_tools.add(self._tool_index)
            self._tool_index = None
            self.client_calls += 1
        out += self._event("response.output_item.done", {
            "output_index": len(self._output), "item": item})
        self._output.append(item)
        self._open = None
        return bytes(out)

    def _data(self, payload: bytes) -> bytes:
        if payload == b"[DONE]":
            return self._end()
        try:
            doc = json.loads(payload)
        except ValueError:
            return b""
        if not isinstance(doc, dict):
            return b""
        if "error" in doc and "choices" not in doc:
            return self._fail(error_body(doc, 500)["error"])
        out = bytearray(self._start(doc))
        if isinstance(doc.get("usage"), dict):
            self.usage = doc["usage"]
        for choice in doc.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or {}
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if REASONING_AS_SUMMARY and isinstance(reasoning, str) and reasoning:
                if self._open != "reasoning":
                    out += self._open_item(
                        "reasoning", _reasoning_item(_id("rs"), None))
                    out += self._event("response.reasoning_summary_part.added", {
                        **self._at(), "summary_index": 0,
                        "part": {"type": "summary_text", "text": ""}})
                self._text.append(reasoning)
                out += self._event("response.reasoning_summary_text.delta", {
                    **self._at(), "summary_index": 0, "delta": reasoning})
            text = delta.get("content")
            if isinstance(text, str) and text and self._open != "text":
                # Des blancs seuls avant un appel d'outil (certains gabarits
                # en émettent) ne font pas un message : retenus jusqu'au
                # premier caractère visible, jetés si rien ne suit.
                self._blank += text
                text = "" if not self._blank.strip() else self._blank
            if isinstance(text, str) and text:
                if self._open != "text":
                    self._blank = ""
                    out += self._open_item(
                        "text", _message_item(_id("msg"), "", "in_progress"))
                    out += self._event("response.content_part.added", {
                        **self._at(), "content_index": 0,
                        "part": _text_part("")})
                self._text.append(text)
                self.out_chars += len(text)
                out += self._event("response.output_text.delta", {
                    **self._at(), "content_index": 0, "delta": text,
                    "logprobs": []})
            for tc in delta.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                idx = tc.get("index", 0)
                fn = tc.get("function") or {}
                if self._open not in ("tool", "hosted") \
                        or self._tool_index != idx:
                    if idx in self._closed_tools:
                        # Fragment tardif d'un outil déjà fermé : impossible
                        # en pratique, ignoré plutôt que de casser le flux.
                        continue
                    call_id = str(tc.get("id") or _id("call"))
                    name = str(fn.get("name") or "")
                    if name in self.ctx.hosted:
                        # Outil hébergé : le client voit un élément
                        # `web_search_call` s'ouvrir, jamais ses arguments.
                        module = self.ctx.hosted[name]
                        out += self._open_item(
                            "hosted", _hosted_item(_hosted_id(module), module))
                        out += self._event(
                            f"response.{module.ITEM_TYPE}.in_progress",
                            self._at())
                    elif self.ctx.form(name) == "custom":
                        item = _client_call(self.ctx, call_id, name, "")
                        out += self._open_item(
                            "tool", {**item, "status": "in_progress"})
                    elif self.ctx.form(name) == "local_shell":
                        out += self._open_item(
                            "tool", {"id": _id("lsh")}, announce=False)
                    else:
                        out += self._open_item("tool", _call_item(
                            _id("fc"), call_id, name, "", "in_progress",
                            self.ctx))
                    self._call, self._tool_index = (call_id, name), idx
                args = fn.get("arguments")
                if isinstance(args, str) and args:
                    self._text.append(args)
                    if self._open == "tool" \
                            and self.ctx.form(self._call[1]) == "function":
                        out += self._event(
                            "response.function_call_arguments.delta",
                            {**self._at(), "delta": args})
            if choice.get("finish_reason"):
                self._finish_reason = choice["finish_reason"]
        return bytes(out)

    def _fail(self, error: dict) -> bytes:
        """Erreur en cours de flux : `response.failed`, seule façon de la
        dire une fois le 200 parti."""
        if self._finished:
            return b""
        out = bytearray(self._start({}))
        out += self._close()
        self._finished = True
        # Plus rien à exécuter : la réponse est close, en échec.
        self.pending = []
        out += self._event("response.failed", {"response": self._snapshot(
            "failed", {"code": "server_error",
                       "message": str(error.get("message") or "")})})
        return bytes(out)

    def _end(self, force: bool = False) -> bytes:
        """Fin d'un flux upstream. Avec des appels hébergés en attente, ce
        n'est que la fin d'un TOUR : la réponse reste ouverte, c'est
        finalize() (`force`) qui la clora."""
        if self._finished:
            return b""
        out = bytearray(self._start({}))
        out += self._close()
        if self.pending and not force:
            return bytes(out)
        self._finished = True
        status = _final_status(self._finish_reason)
        out += self._event(f"response.{status}",
                           {"response": self._snapshot(status)})
        return bytes(out)


def _est(chars: int) -> int:
    return max(chars // CHARS_PER_TOKEN, 1) if chars else 0
