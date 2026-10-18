"""
La RÉPONSE : chat/completions → Responses, d'un seul tenant (éléments,
usage, statut, erreurs).
"""

import json
import time

from .common import (
    INCOMPLETE_REASONS,
    REASONING_AS_SUMMARY,
    Context,
    _custom_input,
    _id,
    _shell_action,
)


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


def _hosted_item(item_id: str, tool, arguments: str | None = None,
                 result=None) -> dict:
    """Élément qui rend compte d'un appel exécuté par le proxy
    (`web_search_call` : la liaison Responses de l'outil). `arguments`
    None = appel en cours : ni action ni résultat. Terminé, le client
    n'en voit que l'action — le `summary` de l'outil, qui peut y mettre
    ce qu'il tire du résultat (la plage lue d'une page)."""
    item = {"id": item_id, "type": tool.responses.item,
            "status": "in_progress" if arguments is None else "completed"}
    if arguments is not None:
        try:
            args = json.loads(arguments or "{}")
        except ValueError:
            args = None
        item["action"] = tool.summary(args if isinstance(args, dict) else {},
                                      result)
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
            item_id = _id("ws")
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
