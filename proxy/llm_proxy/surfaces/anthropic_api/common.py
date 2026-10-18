"""
Ce que partagent la requête, la réponse et le flux de /v1/messages :
les réglages de la surface (model_map, trace, ping), les erreurs, le
catalogue et l'estimation des tokens.
"""

import datetime as _dt
import json
import re

from ...core import config


# Noms de modèles Anthropic → noms préfixés du proxy. «default» attrape
# tout nom sans préfixe backend qui n'est pas dans la table.
_raw_map = config.section("anthropic").get("model_map")


MODEL_MAP: dict[str, str] = {
    str(k).strip().lower(): str(v).strip()
    for k, v in (_raw_map.items() if isinstance(_raw_map, dict) else ())
    if str(v).strip()
}


# Absente du TOML = surface inactive : un déploiement existant n'expose
# rien de nouveau sans l'avoir demandé.
ENABLED = config.flag("anthropic.enabled", False)


# Une ligne de log par réponse /v1/messages : stop_reason, outils appelés
# (nom + extrait des arguments), tokens — pour voir ce qu'un agent qui
# s'emballe répète.
TRACE = config.flag("anthropic.trace", False)


# En flux, pendant l'attente du limiteur : un `event: ping` toutes les N
# secondes garde la connexion vivante (certains clients coupent un flux
# muet ; l'API Anthropic envoie aussi ces pings). 0 = attendre AVANT de
# répondre.
PING_INTERVAL = config.num("anthropic.ping_interval", 10)


# `reasoning_content` d'un backend → bloc `thinking` pour le client, sans
# signature (rejoué par le client, il est jeté à la traduction).
REASONING_AS_THINKING = config.flag("anthropic.reasoning_as_thinking", True)


CHARS_PER_TOKEN = 4


# «claude-opus-5[1m]» : le suffixe entre crochets est un choix de
# contexte du client, pas un modèle. Ignoré pour la recherche.
_BRACKET_SUFFIX = re.compile(r"\[[^\]]*\]$")


STOP_REASONS = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "refusal",
}


# Les outils serveur exécutés par le proxy sont ceux de tools/ qui portent
# une liaison `anthropic` (tools.Anthropic) : préfixe du type, toutes
# versions datées confondues, et bloc de résultat — erreur en
# `<bloc>_error`, compteur `<préfixe>_requests` dans
# `usage.server_tool_use`. Ce module ne connaît que le format de ces blocs
# (_CONTENT et _REPLAY, plus bas).

# Codes d'erreur du contrat des outils (tools.ERRORS) → `error_code` des
# outils serveur d'Anthropic. Le modèle lit le texte de l'erreur ; le
# client Anthropic n'en reçoit que ce code.
ERROR_CODES = {
    "invalid_input": "invalid_tool_input",
    "not_allowed": "url_not_allowed",
    "not_accessible": "url_not_accessible",
    "unsupported": "unsupported_content_type",
    "too_many_requests": "too_many_requests",
    "limit": "max_uses_exceeded",
    "timeout": "unavailable",
    # L'outil a tourné et dit avoir échoué : Anthropic n'a pas de code
    # commun pour cela, et seul le modèle en lit le détail.
    "failed": "unavailable",
    "unavailable": "unavailable",
}


# Types d'erreur de l'API Anthropic par statut HTTP.
ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    429: "rate_limit_error",
    500: "api_error",
    529: "overloaded_error",
}


def resolve_model(name: str, backends) -> str | None:
    """Nom tel que le client l'envoie → nom préfixé routable, ou None.
    Ordre : table exacte, table sans suffixe «[…]», nom déjà préfixé
    par un backend connu (passe tel quel), «default»."""
    key = str(name or "").strip().lower()
    if not key:
        return MODEL_MAP.get("default")
    if key in MODEL_MAP:
        return MODEL_MAP[key]
    bare = _BRACKET_SUFFIX.sub("", key)
    if bare in MODEL_MAP:
        return MODEL_MAP[bare]
    if any(key.startswith(b + "/") for b in backends):
        return str(name).strip()
    return MODEL_MAP.get("default")


def error_type(status: int) -> str:
    if status in ERROR_TYPES:
        return ERROR_TYPES[status]
    return "api_error" if status >= 500 else "invalid_request_error"


def error_body(message: str, type_: str) -> dict:
    return {"type": "error", "error": {"type": type_, "message": message}}


def prompt_text(payload) -> str:
    """count_tokens : le corps sérialisé (system + messages + tools), ce
    que le limiteur approxime et qu'un /tokenize de backend reçoit."""
    doc = {k: payload.get(k) for k in ("system", "messages", "tools")
           if isinstance(payload, dict) and payload.get(k) is not None}
    return json.dumps(doc, ensure_ascii=False)


def estimate_tokens(payload) -> int:
    """Approximation locale, même ratio que le limiteur."""
    return max(len(prompt_text(payload)) // CHARS_PER_TOKEN, 1)


def models_list(entries: list[dict]) -> dict:
    """Le catalogue fusionné (entrées déjà préfixées, forme OpenAI du
    proxy) à la forme Anthropic."""
    data = []
    for m in entries:
        created = m.get("created") or 0
        data.append({
            "type": "model",
            "id": m["id"],
            "display_name": m["id"],
            "created_at": _iso(created),
        })
    return {"data": data, "has_more": False,
            "first_id": data[0]["id"] if data else None,
            "last_id": data[-1]["id"] if data else None}


def _iso(ts) -> str:
    try:
        return _dt.datetime.fromtimestamp(
            int(ts), _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError, TypeError):
        return "1970-01-01T00:00:00Z"
