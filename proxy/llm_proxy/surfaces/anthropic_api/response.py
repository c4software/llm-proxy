"""
La RÉPONSE : chat/completions → Messages, d'un seul tenant (blocs,
usage, raison d'arrêt), ce que le client voit des outils serveur, les
erreurs et les événements SSE.
"""

import json
import time
import uuid

from .common import REASONING_AS_THINKING, STOP_REASONS, error_body, error_type, _iso


# ── Réponse : OpenAI → Anthropic ────────────────────────────────────────

def _tool_id() -> str:
    return "toolu_" + uuid.uuid4().hex[:24]


def _server_tool_id() -> str:
    return "srvtoolu_" + uuid.uuid4().hex[:24]


def _msg_id() -> str:
    return "msg_" + uuid.uuid4().hex[:24]


def _cached(u) -> int:
    details = u.get("prompt_tokens_details") if isinstance(u, dict) else None
    if isinstance(details, dict) and isinstance(details.get("cached_tokens"), int):
        return max(details["cached_tokens"], 0)
    return 0


def _usage(u) -> dict:
    """Usage Anthropic. Chez Anthropic, input_tokens EXCLUT les tokens
    lus en cache ; chez OpenAI, prompt_tokens les inclut — on soustrait,
    pour que le client additionne juste."""
    u = u if isinstance(u, dict) else {}
    cached = _cached(u)
    prompt = int(u.get("prompt_tokens") or 0)
    return {
        "input_tokens": max(prompt - cached, 0),
        "output_tokens": int(u.get("completion_tokens") or 0),
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": cached,
    }


def _parse_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        obj = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _message_blocks(msg: dict, hosted: dict) -> tuple[list[dict], list[dict]]:
    """Message d'une réponse non streamée → (blocs de `content`, appels
    hébergés à exécuter). Un appel dont le nom est dans `hosted` ne donne
    pas de bloc `tool_use` : il attend d'être exécuté (voir _pending)."""
    content: list[dict] = []
    pending: list[dict] = []
    reasoning = msg.get("reasoning_content") or msg.get("reasoning")
    if REASONING_AS_THINKING and isinstance(reasoning, str) and reasoning:
        content.append({"type": "thinking", "thinking": reasoning,
                        "signature": ""})
    if isinstance(msg.get("content"), str) and msg["content"].strip():
        content.append({"type": "text", "text": msg["content"]})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        name = str(fn.get("name", ""))
        if name in hosted:
            args = fn.get("arguments")
            pending.append(_pending(name, args if isinstance(args, str)
                                    else json.dumps(args or {})))
            continue
        content.append({
            "type": "tool_use",
            "id": str(tc.get("id") or _tool_id()),
            "name": name,
            "input": _parse_args(fn.get("arguments")),
        })
    return content, pending


def _stop_reason(finish, client_tool: bool, searched: bool = False) -> str:
    """`searched` : la réponse a exécuté des appels hébergés. Un
    dernier tour clos sur `tool_calls` sans aucun `tool_use` pour le
    client (le modèle cherchait encore quand la boucle s'est arrêtée)
    n'est pas un `tool_use` : le client n'a rien à exécuter."""
    stop = STOP_REASONS.get(finish, "end_turn")
    if client_tool and finish != "length":
        return "tool_use"
    if searched and stop == "tool_use":
        return "end_turn"
    return stop


def _message(msg_id: str, model: str, content: list[dict], stop: str,
             usage: dict) -> dict:
    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": usage,
    }


def from_openai(doc: dict, model: str) -> dict:
    """Réponse non streamée /v1/chat/completions → objet Message, sans
    outil hébergé (ceux-là passent par Translator.finish)."""
    choice = (doc.get("choices") or [{}])[0]
    content, _ = _message_blocks(choice.get("message") or {}, {})
    stop = _stop_reason(choice.get("finish_reason"),
                        any(b["type"] == "tool_use" for b in content))
    return _message(str(doc.get("id") or _msg_id()), model, content, stop,
                    _usage(doc.get("usage")))


# ── outils hébergés : ce que le client en voit ──

def _pending(name: str, arguments: str) -> dict:
    """Un appel hébergé à exécuter (Translator.pending). `id` : celui des
    deux blocs ; `arguments` : tels que le modèle les a écrits, c'est ce
    texte que l'exécution reçoit."""
    return {"id": _server_tool_id(), "name": name,
            "arguments": arguments or "{}", "announced": False}


def _search_content(call: dict, result) -> list[dict]:
    """Résultat d'une recherche → contenu du bloc `web_search_tool_result`.
    `encrypted_content` : chez Anthropic, un blob opaque que le client
    renvoie tel quel. Ici on y met l'EXTRAIT, en clair : le bloc porte
    tout ce que le modèle a lu, et son rejeu n'a besoin d'aucune mémoire.
    `page_age` : la date du résultat (AAAA-MM-JJ), ou null."""
    return [{"type": "web_search_result", "title": s.title,
             "url": s.url, "encrypted_content": s.snippet,
             "page_age": s.date or None}
            for s in result.sources]


def _fetch_content(call: dict, result) -> dict:
    """Résultat d'une lecture → contenu du bloc `web_fetch_tool_result`.
    `data` : le texte ENTIER que le modèle a lu, en-tête compris, pas le
    seul corps de la page — ce qui rend le rejeu exact sans mémoire.
    `url` : celle réellement lue, après redirections."""
    return {"type": "web_fetch_result",
            "url": str(result.meta.get("url") or "")
            or str(_parse_args(call["arguments"]).get("url") or ""),
            "content": {"type": "document",
                        "source": {"type": "text", "media_type": "text/plain",
                                   "data": result.text},
                        "title": result.meta.get("title") or None},
            "retrieved_at": _iso(time.time())}


# Bloc de résultat → comment en écrire le contenu, pour un appel abouti :
# (appel, résultat). Les blocs que ce module sait rendre : un outil lié à
# un autre bloc n'est pas présenté (server_tool).
_CONTENT = {"web_search_tool_result": _search_content,
            "web_fetch_tool_result": _fetch_content}


def from_openai_error(doc, status: int) -> dict:
    """Erreur OpenAI {"error": {"message", "type"}} → erreur Anthropic.
    Un corps qui n'est pas de cette forme est relayé en message."""
    message = ""
    if isinstance(doc, dict):
        err = doc.get("error")
        if isinstance(err, dict):
            message = str(err.get("message") or "")
        elif isinstance(err, str):
            message = err
        else:
            message = str(doc.get("message") or doc.get("detail") or "")
    elif isinstance(doc, str):
        message = doc
    return error_body(message or f"upstream HTTP {status}", error_type(status))


def _sse(event: str, data: dict) -> bytes:
    return (f"event: {event}\ndata: "
            + json.dumps(data, ensure_ascii=False) + "\n\n").encode()


def ping_event() -> bytes:
    return _sse("ping", {"type": "ping"})


def sse_error(body: dict) -> bytes:
    """Une erreur (forme error_body) émise DANS un flux déjà ouvert —
    la seule façon de la dire une fois le 200 parti."""
    return _sse("error", body)
