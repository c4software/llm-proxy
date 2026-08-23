"""
Les backends OpenAI-compatibles : leur déclaration ([backends.<nom>] du
TOML), leur client HTTP, et le ROUTAGE — c'est-à-dire la seule règle qui
compte ici, le PRÉFIXE du modèle désigne le backend.

« albert/openweight-large » part vers [backends.albert], « bigchuck/qwen3 »
vers [backends.bigchuck], préfixe retiré avant transfert. Tout modèle doit
être préfixé : sans préfixe reconnu → 400. Seules les requêtes SANS champ
model (endpoints de compte, corps non JSON) n'ont rien à router et
tombent sur le backend de repli.
"""

import json

import httpx

from . import albert
from . import config
from .settings import META_TIMEOUT, TIMEOUT, TOOL_CHOICE, log


class Backend:
    """Un backend OpenAI-compatible, identifié par son nom = préfixe de
    routage. Celui marqué quotas=True (unique, Albert) passe par le
    limiteur ; les autres sont illimités mais pas toujours allumés —
    contactés uniquement à la demande, jamais sondés en tâche de fond."""

    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.url = str(cfg.get("url", "") or "").rstrip("/")
        if not self.url:
            raise SystemExit(f"backends.{name} : champ 'url' manquant")
        self.api_key = str(cfg.get("api_key", "") or "")
        self.verify_ssl = bool(cfg.get("verify_ssl", True))
        self.quotas = bool(cfg.get("quotas", False))
        self.timeout = float(cfg.get("timeout", TIMEOUT))
        self.meta_timeout = float(cfg.get("meta_timeout", META_TIMEOUT))
        # Poignée de main TCP seulement — traitée par le noyau, même si
        # le serveur derrière est occupé à générer. Un hôte vivant répond
        # en < 50 ms sur LAN, < 300 ms via Tailscale : 1 s suffit à un
        # backend local, souvent ÉTEINT, pour échouer vite (503 en 1 s
        # plutôt qu'en 5). Plus large vers l'Internet.
        self.connect_timeout = float(cfg.get(
            "connect_timeout", 15.0 if self.quotas else 1.0))
        # Injection de `tool_choice` : DÉSACTIVÉE PAR DÉFAUT. Le
        # correctif ne vaut que pour les backends dont le schéma a
        # "none" pour défaut (Albert) ; ailleurs il est au mieux inutile,
        # au pire nuisible. On l'active donc backend par backend :
        #   absent / false → aucune injection (défaut) ;
        #   true           → valeur globale proxy.tool_choice ;
        #   "auto", "required"… → cette valeur-là, pour ce backend.
        self.tool_choice = self._tool_choice(cfg.get("force_tool_choice"))
        # Plafond de `max_tokens` (0 = aucun) : Claude Code en demande
        # 32 000 par défaut, que certains backends refusent tout net. La
        # valeur du client est ramenée au plafond, jamais augmentée.
        self.max_tokens = int(cfg.get("max_tokens", 0) or 0)
        # Le backend accepte les images (`image_url`) : les blocs image
        # d'un client Anthropic lui sont transmis ; sinon remplacés par un
        # texte qui dit qu'une image manque.
        self.images = bool(cfg.get("images", False))
        # Chemin d'un endpoint de tokenisation (llama.cpp : "/tokenize")
        # pour un count_tokens EXACT ; vide = estimation locale.
        self.tokenize_path = str(cfg.get("tokenize_path", "") or "").strip()
        # Chaque backend à quotas a SES limiteurs/routeurs (deux comptes
        # Albert avec des clés différentes ne partagent rien).
        self.quota_state = albert.QuotaState(name) if self.quotas else None
        self.client: httpx.AsyncClient | None = None
        # Dernier catalogue vu lors d'un GET /v1/models (informel, healthz).
        self.models: set[str] = set()

    @staticmethod
    def _tool_choice(value) -> str | None:
        """None = ne rien injecter pour ce backend (le défaut)."""
        if value is None or value is False:
            return None
        if value is True:
            return TOOL_CHOICE
        return str(value)

    def auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}


# Défaut si [backends] est absent du TOML : Albert seul. URLs ET clés ne
# vivent QUE là (champ api_key par backend), rien ailleurs.
DEFAULT_BACKENDS = {
    # Albert est le seul à avoir besoin du correctif tool_choice (son
    # schéma déclare «default: none») : ce fallback l'active donc, alors
    # que l'option est à false partout ailleurs.
    "albert": {"url": "https://albert.api.etalab.gouv.fr", "quotas": True,
               "force_tool_choice": "auto"},
}


def load_backends() -> dict[str, Backend]:
    """Une table [backends.<nom>] par backend ; <nom> EST le préfixe de
    routage."""
    parsed = config.section("backends") or DEFAULT_BACKENDS
    if not isinstance(parsed, dict) or not parsed:
        raise SystemExit("[backends] : au moins un backend attendu")
    out: dict[str, Backend] = {}
    for name, cfg in parsed.items():
        key = str(name).strip().strip("/").lower()
        if not key:
            raise SystemExit("[backends] : nom de backend vide")
        out[key] = Backend(key, cfg if isinstance(cfg, dict) else {})
    return out


BACKENDS: dict[str, Backend] = load_backends()
# Les requêtes SANS champ model (endpoints de compte, corps non JSON)
# n'ont pas de préfixe à router : elles partent vers le premier backend
# à quotas (à défaut, le premier déclaré).
FALLBACK_BACKEND = next(
    (b for b in BACKENDS.values() if b.quotas),
    next(iter(BACKENDS.values())),
)

def route_backend(payload) -> tuple[Backend | None, bool]:
    """Discriminant de routage : le préfixe du modèle. «<nom>/…» part
    vers le backend <nom> ; requête sans champ model → FALLBACK_BACKEND ;
    modèle sans préfixe reconnu → None (400). Renvoie (backend, préfixé)
    — préfixé impose de retirer «nom/»."""
    model = ""
    if isinstance(payload, dict):
        model = str(payload.get("model", "") or "")
    if not model:
        return FALLBACK_BACKEND, False
    m = model.lower()
    for name, b in BACKENDS.items():
        if m.startswith(name + "/"):
            return b, True
    return None, False


def unknown_prefix_message(model: str) -> str:
    """Texte du 400 «modèle sans préfixe» ; la réponse elle-même est
    construite par app.error_response, dans le dialecte du client."""
    prefixes = ", ".join(f"«{n}/»" for n in BACKENDS)
    log.warning("modèle %r sans préfixe backend reconnu → 400", model)
    return f"modèle «{model}» sans préfixe backend ; préfixes attendus : {prefixes}"


def strip_backend_prefix(payload: dict, b: Backend) -> bytes:
    """Retire «<nom>/» du champ model (l'upstream ne connaît pas le
    préfixe) et ré-encode le corps. ensure_ascii=False : sans lui, chaque
    accent partirait en «\\uXXXX» — corps gonflé sur des prompts français,
    pour un JSON strictement équivalent."""
    payload["model"] = str(payload["model"])[len(b.name) + 1:]
    return json.dumps(payload, ensure_ascii=False).encode()


def backend_offline_message(b: Backend, exc: Exception) -> str:
    log.warning("backend %s (%s) injoignable : %s → 503", b.name, b.url, exc)
    return f"backend «{b.name}» ({b.url}) hors ligne : {exc}"


async def open_clients() -> None:
    """Un client HTTP par backend, ouvert au démarrage de l'application."""
    for b in BACKENDS.values():
        b.client = httpx.AsyncClient(
            base_url=b.url,
            timeout=httpx.Timeout(b.timeout, connect=b.connect_timeout),
            follow_redirects=False,
            verify=b.verify_ssl,
        )


async def close_clients() -> None:
    for b in BACKENDS.values():
        if b.client is not None:
            await b.client.aclose()
