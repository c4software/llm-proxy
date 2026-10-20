"""
La REQUÊTE : Messages (Anthropic) → chat/completions (contenu, images,
outils serveur rejoués, choix d'outil).
"""

import json

from ...tools.contract import Source

from .common import CHARS_PER_TOKEN
from .response import _CONTENT, _tool_id


# ── Requête : Anthropic → OpenAI ────────────────────────────────────────

def _text_of(content) -> str:
    """Texte d'un contenu (chaîne, ou liste de blocs dont on ne garde
    que les `text`)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _image_part(block: dict) -> dict | None:
    src = block.get("source")
    if not isinstance(src, dict):
        return None
    if src.get("type") == "base64" and src.get("data"):
        media = src.get("media_type") or "image/png"
        return {"type": "image_url",
                "image_url": {"url": f"data:{media};base64,{src['data']}"}}
    if src.get("type") == "url" and src.get("url"):
        return {"type": "image_url", "image_url": {"url": src["url"]}}
    return None


def has_images(payload: dict) -> bool:
    """Au moins un bloc image (tool_result compris) ? Évite de charger un
    catalogue pour rien."""
    for m in payload.get("messages") or []:
        content = m.get("content") if isinstance(m, dict) else None
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "image":
                return True
            if b.get("type") == "tool_result":
                inner = b.get("content")
                if isinstance(inner, list) and any(
                        isinstance(x, dict) and x.get("type") == "image"
                        for x in inner):
                    return True
    return False


def _placeholder(block: dict, what: str) -> dict:
    """Ce qu'un backend texte seul reçoit à la place d'un média."""
    src = block.get("source") or {}
    media = src.get("media_type") if isinstance(src, dict) else None
    size = len(src.get("data") or "") * 3 // 4 if isinstance(src, dict) else 0
    detail = media or ""
    if size:
        detail += f", {size // 1024} Ko" if detail else f"{size // 1024} Ko"
    return {"type": "text",
            "text": f"[{what} ignoré{'e' if what == 'image' else ''}"
                    f"{' : ' + detail if detail else ''}]"}


def _blocks_to_parts(blocks, images: bool) -> list[dict]:
    """Blocs de contenu (text / image / document) → parties OpenAI.
    `images` = le backend accepte les `image_url` ; sinon un texte de
    remplacement. Un `document` n'a d'équivalent OpenAI que s'il est du
    texte ; un PDF devient un texte de remplacement."""
    parts = []
    for b in blocks if isinstance(blocks, list) else []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            parts.append({"type": "text", "text": b.get("text", "")})
        elif t == "image":
            part = _image_part(b) if images else None
            parts.append(part or _placeholder(b, "image"))
        elif t == "document":
            src = b.get("source") or {}
            if isinstance(src, dict) and src.get("type") == "text":
                parts.append({"type": "text", "text": str(src.get("data", ""))})
            else:
                parts.append(_placeholder(b, "document"))
    return parts


def _content_of(parts: list[dict]):
    """Une chaîne si tout est texte — la forme que tous les backends
    acceptent —, la liste de parties sinon."""
    if all(p["type"] == "text" for p in parts):
        return "".join(p["text"] for p in parts)
    return parts


def _user_message(content, images: bool) -> list[dict]:
    """Un message user Anthropic peut mêler n tool_result et du contenu
    libre. OpenAI veut un message `tool` PAR résultat, juste après
    l'assistant qui les a demandés — donc avant le reste. Un `tool`
    OpenAI n'a qu'un contenu TEXTE : les images d'un tool_result suivent
    dans un message user à part."""
    if isinstance(content, str):
        return [{"role": "user", "content": content}]
    if not isinstance(content, list):
        return []
    tools, parts = [], []
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "tool_result":
            inner = b.get("content")
            if isinstance(inner, str):
                text, media = inner, []
            else:
                inner_parts = _blocks_to_parts(inner, images)
                text = "".join(p["text"] for p in inner_parts
                               if p["type"] == "text")
                media = [p for p in inner_parts if p["type"] != "text"]
            if b.get("is_error") and text:
                text = f"Error: {text}"
            call_id = str(b.get("tool_use_id", ""))
            tools.append({"role": "tool", "tool_call_id": call_id,
                          "content": text})
            if media:
                parts.append({"type": "text",
                              "text": f"[résultat de l'outil {call_id}]"})
                parts.extend(media)
        else:
            parts.extend(_blocks_to_parts([b], images))
    out = tools
    if parts:
        out.append({"role": "user", "content": _content_of(parts)})
    return out


def _prepend_text(msg: dict, text: str) -> None:
    content = msg.get("content")
    if isinstance(content, list):
        content.insert(0, {"type": "text", "text": text + "\n\n"})
    elif isinstance(content, str) and content:
        msg["content"] = text + "\n\n" + content
    else:
        msg["content"] = text


class Context:
    """Ce que la requête dit des outils hébergés, pour to_openai, le
    robinet et tool_loop.py. Vide (`hosted` = {}) si aucun outil serveur
    déclaré n'est hébergé : ils sont alors ignorés.

    `hosted` : nom de fonction → outil de tools/, déclaré ET hébergé ;
    `limits` : par fonction, appels au plus pour cette réponse (le
               `max_uses` du client, borné par tools.MAX_CALLS) ;
    `cap`    : appels au plus, toutes fonctions confondues ;
    `options`: par fonction, réglages du client que l'exécution respecte
               (`allowed_domains`, `blocked_domains`, `max_chars`).
               Anthropic refuse les deux listes à la fois (400) ; ici
               elles s'appliquent toutes les deux ;
    `order`  : les fonctions dans l'ordre du registre, celui des
               compteurs de `usage.server_tool_use`."""

    def __init__(self, request: dict, hosted=None):
        self.hosted: dict = {}
        self.limits: dict[str, int] = {}
        self.cap: int = hosted.cap() if hosted else 0
        self.options: dict[str, dict] = {}
        tools = request.get("tools")
        tools = tools if isinstance(tools, list) else []
        # Une fonction du client garde son nom : l'outil hébergé homonyme
        # n'est alors pas présenté (même règle que la surface Responses).
        taken = [t.get("name") for t in tools
                 if isinstance(t, dict) and "input_schema" in t]
        for t in tools:
            tool = server_tool(t, hosted)
            name = tool.name if tool else None
            # Déclaré deux fois : le premier fait foi.
            if tool is None or name in taken or name in self.hosted:
                continue
            self.hosted[name] = tool
            self.limits[name] = hosted.cap(t.get("max_uses"))
            options: dict = {k: [str(d) for d in t[k]]
                             for k in ("allowed_domains", "blocked_domains")
                             if isinstance(t.get(k), list) and t[k]}
            # `max_content_tokens` (web fetch), converti en caractères :
            # il ne fait que BAISSER la borne du proxy.
            size = t.get("max_content_tokens")
            if isinstance(size, int) and not isinstance(size, bool) \
                    and size > 0:
                options["max_chars"] = size * CHARS_PER_TOKEN
            if options:
                self.options[name] = options
        self.order: list[str] = [t.name for t in hosted.tools
                                 if t.name in self.hosted] if hosted else []


def server_tool(tool, hosted=None):
    """L'outil hébergé que remplace cet outil du client, ou None : une
    fonction du client (`input_schema`), un outil serveur que le proxy
    n'héberge pas — ou dont ce module ne sait pas écrire le bloc."""
    if not hosted or not isinstance(tool, dict) or "input_schema" in tool:
        return None
    found = hosted.for_server(str(tool.get("type") or ""))
    return found if found and found.anthropic.block in _CONTENT else None


def _replayed_error(tool, code) -> str:
    """Texte rendu au modèle pour un résultat en erreur REJOUÉ par le
    client : le bloc n'en garde que le code."""
    return (f"Error: the {tool.name.replace('_', ' ')} failed "
            f"({code or 'unavailable'}).")


def _search_sources(content) -> tuple:
    """L'inverse de _search_content."""
    return tuple(Source(str(r.get("url") or ""), str(r.get("title") or ""),
                        str(r.get("page_age") or ""),
                        str(r.get("encrypted_content") or ""))
                 for r in content
                 if isinstance(r, dict) and r.get("type") == "web_search_result")


def _search_text(tool, use: dict, result: dict) -> str:
    """Le texte qu'avait lu le modèle, reconstruit sans mémoire depuis les
    deux blocs rejoués : le bloc de résultat porte tout, et Tool.render le
    remet en texte à l'octet près."""
    content = result.get("content")
    if not isinstance(content, list):
        code = content.get("error_code") if isinstance(content, dict) else None
        return _replayed_error(tool, code)
    args = use.get("input") if isinstance(use.get("input"), dict) else {}
    return tool.render(args, _search_sources(content))


def _fetch_text(tool, use: dict, result: dict) -> str:
    """Le texte qu'avait lu le modèle : la `data` du document rejoué, où
    _fetch_content l'a mis ENTIER. Un document qui n'est pas du texte (PDF
    d'une conversation commencée chez Anthropic) rend une lecture en
    échec."""
    content = result.get("content")
    content = content if isinstance(content, dict) else {}
    doc = content.get("content")
    source = doc.get("source") if isinstance(doc, dict) else None
    if content.get("type") == "web_fetch_result" and isinstance(source, dict) \
            and source.get("type") == "text" \
            and isinstance(source.get("data"), str):
        return source["data"]
    return _replayed_error(tool, content.get("error_code"))


# Bloc de résultat → comment relire, d'un bloc rejoué par le client, le
# texte qu'avait lu le modèle : (outil, bloc de l'appel, bloc du résultat).
_REPLAY = {"web_search_tool_result": _search_text,
           "web_fetch_tool_result": _fetch_text}


def _assistant_messages(content, hosted=None, results=None) -> list[dict]:
    """thinking / redacted_thinking sont JETÉS : aucun backend OpenAI ne
    les rejoue, et leur signature n'a de sens que chez Anthropic.

    Un message assistant Anthropic donne UN message OpenAI — sauf s'il
    porte des appels exécutés par le proxy (`hosted`). Chaque paire
    `server_tool_use` + bloc de résultat redevient un appel suivi de son
    message `tool`, et ce qui suit un résultat ouvre un nouveau message
    assistant (un autre tour du backend) : [texte, recherche, résultat,
    texte] rend assistant(texte + appel), tool, assistant(texte). C'est ce
    que le backend a vu pendant la boucle, et tool_loop.py le reconstruit
    par ce même chemin : le préfixe reste stable d'un tour et d'une requête
    à l'autre.

    `results` : id d'appel → texte exact rendu au modèle, pour la réponse
    EN COURS ; un bloc rejoué est relu par _REPLAY. Un `server_tool_use`
    non hébergé ou sans son résultat est ignoré : un appel sans message
    `tool` serait refusé par le backend."""
    if isinstance(content, str):
        return [{"role": "assistant", "content": content}]
    blocks = [b for b in content if isinstance(b, dict)] \
        if isinstance(content, list) else []
    hosted = hosted or {}
    kinds = [t.anthropic.block for t in hosted.values()]
    answers = {str(b.get("tool_use_id")): b for b in blocks
               if b.get("type") in kinds}
    out: list[dict] = []
    text: list[str] = []
    calls: list[dict] = []
    tools: list[dict] = []

    def flush() -> None:
        msg: dict = {"role": "assistant", "content": "".join(text) or None}
        if calls:
            msg["tool_calls"] = list(calls)
        out.append(msg)
        out.extend(tools)
        text.clear(), calls.clear(), tools.clear()

    for b in blocks:
        t = b.get("type")
        name = b.get("name")
        ours = t == "server_tool_use" and isinstance(name, str) \
            and name in hosted and answers.get(
                str(b.get("id")), {}).get("type") == hosted[name].anthropic.block
        if tools and (ours or t in ("text", "tool_use")):
            flush()
        if t == "text":
            text.append(b.get("text", ""))
        elif t == "tool_use" or ours:
            call_id = str(b.get("id") or _tool_id())
            calls.append({
                "id": call_id,
                "type": "function",
                "function": {
                    "name": str(b.get("name", "")),
                    "arguments": json.dumps(b.get("input") or {},
                                            ensure_ascii=False),
                },
            })
            if ours:
                known = (results or {}).get(call_id)
                if not isinstance(known, str):
                    tool = hosted[name]
                    known = _REPLAY[tool.anthropic.block](
                        tool, b, answers[call_id])
                tools.append({"role": "tool", "tool_call_id": call_id,
                              "content": known})
    if text or calls or not out:
        flush()
    return out


def _tool_choice(value, payload: dict) -> None:
    if not isinstance(value, dict):
        return
    t = value.get("type")
    if t == "auto":
        payload["tool_choice"] = "auto"
    elif t == "any":
        payload["tool_choice"] = "required"
    elif t == "none":
        payload["tool_choice"] = "none"
    elif t == "tool" and value.get("name"):
        payload["tool_choice"] = {"type": "function",
                                  "function": {"name": value["name"]}}
    if value.get("disable_parallel_tool_use"):
        payload["parallel_tool_calls"] = False


def to_openai(p: dict, images: bool = False, hosted=None,
              results: dict | None = None) -> dict:
    """Corps /v1/messages → corps /v1/chat/completions. `model` est déjà
    résolu (resolve_model). Ce qui n'a pas d'équivalent (thinking, top_k,
    cache_control, output_config…) est ignoré plutôt que relayé à un
    backend qui le refuserait.
    `images` : le backend accepte les `image_url`. `hosted` : l'annuaire
    des outils exécutés par le proxy (tools.Hosted), ou None. `results` :
    voir _assistant_messages."""
    ctx = Context(p, hosted)
    out: dict = {"model": p.get("model", "")}
    messages: list[dict] = []
    system = _text_of(p.get("system"))
    if system:
        messages.append({"role": "system", "content": system})
    # Un message `system` EN COURS de conversation (rappels de certains
    # clients) : beaucoup de gabarits de chat (Qwen, Mistral…) n'acceptent
    # un system qu'en tête et répondent 500 sinon. Son texte est fondu en
    # tête du message user qui le suit.
    # Suivi d'un assistant, il est posé en message user À SA PLACE, avant
    # lui : c'est là qu'il était au tour précédent, quand il fermait la
    # requête. Le déplacer ferait diverger le préfixe d'un tour à l'autre,
    # et un backend à cache de préfixe (llama.cpp…) recalculerait le
    # dernier échange à chaque tour.
    pending: list[str] = []
    for m in p.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role, content = m.get("role"), m.get("content")
        if role == "assistant":
            if pending:
                messages.append({"role": "user", "content": "\n\n".join(pending)})
                pending = []
            messages.extend(_assistant_messages(content, ctx.hosted, results))
        elif role == "user":
            batch = _user_message(content, images)
            if pending:
                text = "\n\n".join(pending)
                pending = []
                if batch and batch[-1]["role"] == "user":
                    _prepend_text(batch[-1], text)
                else:
                    # Que des tool_result : le texte suit, en message user.
                    batch.append({"role": "user", "content": text})
            messages.extend(batch)
        elif role == "system":
            text = _text_of(content)
            if text:
                pending.append(text)
    if pending:
        messages.append({"role": "user", "content": "\n\n".join(pending)})
    out["messages"] = messages

    if isinstance(p.get("max_tokens"), int):
        out["max_tokens"] = p["max_tokens"]
    for src, dst in (("temperature", "temperature"), ("top_p", "top_p"),
                     ("stop_sequences", "stop")):
        if p.get(src) is not None:
            out[dst] = p[src]
    if p.get("stream"):
        out["stream"] = True
        # Sans lui, un flux SSE OpenAI ne porte aucun `usage` : les stats
        # retomberaient sur l'estimation, et le client ne saurait rien.
        out["stream_options"] = {"include_usage": True}
    meta = p.get("metadata")
    if isinstance(meta, dict) and meta.get("user_id"):
        out["user"] = str(meta["user_id"])

    tools = []
    present = frozenset(ctx.hosted)
    left = dict(ctx.hosted)         # fonctions hébergées restant à présenter
    for t in p.get("tools") or []:
        if not isinstance(t, dict):
            continue
        if t.get("name") and "input_schema" in t:
            tools.append({"type": "function", "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema")
                or {"type": "object", "properties": {}},
            }})
        elif (tool := server_tool(t, hosted)) and tool.name in left:
            # Sa fonction à la place de l'outil serveur, à la même
            # position, une seule fois. Une description ne renvoie à une
            # autre fonction que si elle est présentée aussi (`present`).
            tools.append(left.pop(tool.name).spec(present))
        # Les autres outils serveur Anthropic (code_execution, bash,
        # text_editor…) ont un `type` et pas d'input_schema : rien à
        # traduire.
    if tools:
        out["tools"] = tools
        _tool_choice(p.get("tool_choice"), out)
    return out
