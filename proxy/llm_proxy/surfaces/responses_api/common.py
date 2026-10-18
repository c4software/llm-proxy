"""
Ce que partagent la requête, la réponse et le flux de /v1/responses :
les réglages de la surface, le refus d'une requête, le contexte d'un échange.
"""

import json
import uuid

from ...core import config


# Surface inactive par défaut.
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
        # nom → outil de tools/
        self.hosted: dict = {}
        self.memory = None
        # tools.owner() de la clé présentée, «» pour un proxy ouvert.
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


# ── Réponse : OpenAI chat → Responses ───────────────────────────────────

def _id(prefix: str) -> str:
    return f"{prefix}_" + uuid.uuid4().hex[:24]
