"""
Les backends : leur déclaration ([backends.<nom>] du TOML), leur client
HTTP, et le ROUTAGE — c'est-à-dire la seule règle qui compte ici, le
PRÉFIXE du modèle désigne le backend. Un backend parle chat/completions
(OpenAI, le défaut) ou Messages (`api = "anthropic"`, voir anthropic.py).

« albert/openweight-large » part vers [backends.albert], « bigchuck/qwen3 »
vers [backends.bigchuck], préfixe retiré avant transfert. Tout modèle doit
être préfixé : sans préfixe reconnu → 400. Seules les requêtes SANS champ
model (endpoints de compte, corps non JSON) n'ont rien à router et
tombent sur le backend de repli.

Les BACKENDS DES COMPTES (storage/backends.py) s'y ajoutent, pour le client
de la requête (fairshare.CLIENT) : les siens, puis ceux que d'autres
partagent. Un nom du TOML l'emporte toujours. Les MODÈLES PERMIS du compte
(accounts.MODELS, posés par l'admin) bornent le reste : un modèle hors de
ses motifs n'est ni listé ni routé (403 `model_not_allowed`), une requête
sans modèle non plus ; ses propres backends n'y sont pas soumis.
"""

import asyncio
import json

import httpx2

from ..access import accounts, fairshare
from ..core import config
from ..core.httputil import error_response
from ..core.settings import META_TIMEOUT, TIMEOUT, TOOL_CHOICE, log
from ..storage import backends as user_backends
from . import albert, anthropic


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
        # Le protocole du backend : « openai » (chat/completions, le
        # défaut) ou « anthropic » (Messages, traduit par anthropic.py).
        self.api = str(cfg.get("api", "openai") or "openai").strip().lower()
        if self.api not in ("openai", "anthropic"):
            raise SystemExit(f"backends.{name} : api = {self.api!r} inconnu "
                             "(attendu : \"openai\" ou \"anthropic\")")
        self.verify_ssl = bool(cfg.get("verify_ssl", True))
        self.quotas = bool(cfg.get("quotas", False))
        self.timeout = float(cfg.get("timeout", TIMEOUT))
        self.meta_timeout = float(cfg.get("meta_timeout", META_TIMEOUT))
        # Poignée de main TCP seulement — traitée par le noyau, même si
        # le serveur derrière est occupé à générer : 1 s suffit à un
        # backend local, souvent ÉTEINT, pour échouer vite. Plus large
        # vers l'Internet.
        self.connect_timeout = float(cfg.get(
            "connect_timeout", 15.0 if self.quotas or self.api == "anthropic" else 1.0))
        # Injection de `tool_choice` : DÉSACTIVÉE PAR DÉFAUT. Le
        # correctif ne vaut que pour les backends dont le schéma a
        # "none" pour défaut (Albert) ; ailleurs il peut nuire :
        #   absent / false → aucune injection (défaut) ;
        #   true           → valeur globale proxy.tool_choice ;
        #   "auto", "required"… → cette valeur-là, pour ce backend.
        self.tool_choice = self._tool_choice(cfg.get("force_tool_choice"))
        # Plafond de `max_tokens` (0 = aucun) : certains clients en
        # demandent plus que le backend n'accepte. La valeur du client est
        # ramenée au plafond, jamais augmentée.
        self.max_tokens = int(cfg.get("max_tokens", 0) or 0)
        # Le backend accepte les images (`image_url`) — pour les modèles
        # que son catalogue déclare multimodaux ; un modèle texte seul y
        # reçoit un texte de remplacement. false = jamais d'image.
        # Anthropic : tous ses modèles de conversation lisent les images
        # (le catalogue le dit modèle par modèle).
        self.images = bool(cfg.get("images", self.api == "anthropic"))
        # Le partage équitable (fairshare.py) compte-t-il ce backend ?
        # Faux : ses requêtes ne pèsent sur la part de personne et n'y
        # attendent jamais.
        self.fair_share = bool(cfg.get("fair_share", True))
        # Chemin d'un endpoint de tokenisation (llama.cpp : "/tokenize")
        # pour un count_tokens EXACT ; vide = estimation locale.
        self.tokenize_path = str(cfg.get("tokenize_path", "") or "").strip()
        # Chaque backend à quotas a SES limiteurs/routeurs (deux comptes
        # Albert avec des clés différentes ne partagent rien).
        self.quota_state = albert.QuotaState(name) if self.quotas else None
        self.client: httpx2.AsyncClient | None = None
        # Le backend d'un compte (storage/backends.py) : son client (None :
        # un backend du TOML) et s'il est offert à tous.
        self.owner: str | None = None
        self.shared = False
        # Dernier catalogue vu lors d'un GET /v1/models (informel, healthz).
        self.models: set[str] = set()
        # id (minuscule) → type normalisé («image-text-to-text»…), rempli
        # avec le catalogue : c'est lui qui dit si UN modèle voit les
        # images — un backend llama.cpp en sert de toutes sortes, et
        # envoyer une image à un modèle texte seul est un 500 assuré.
        self.model_types: dict[str, str] = {}
        # Backend Anthropic : les `capabilities` (réflexion, effort) et la
        # sortie maximale de chaque modèle, lues avec le catalogue.
        self.model_caps: dict[str, dict] = {}
        self.model_max_out: dict[str, int] = {}
        # Type imposé pour des modèles de CE backend, par motif (jokers
        # shell, casse ignorée) : pour un catalogue qui ne dit pas ce que
        # sont ses modèles (llama-swap) et dont le nom trompe la déduction.
        overrides = cfg.get("model_types")
        self.type_overrides: dict[str, str] = {
            str(k).strip().lower(): str(v).strip()
            for k, v in (overrides.items() if isinstance(overrides, dict) else ())
            if str(k).strip() and str(v).strip()}

    @staticmethod
    def _tool_choice(value) -> str | None:
        """None = ne rien injecter pour ce backend (le défaut)."""
        if value is None or value is False:
            return None
        if value is True:
            return TOOL_CHOICE
        return str(value)

    def auth_headers(self) -> dict:
        if self.api == "anthropic":
            return anthropic.headers(self.api_key)
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}


# Défaut si [backends] est absent du TOML : Albert seul. URLs ET clés ne
# vivent QUE là (champ api_key par backend), rien ailleurs.
DEFAULT_BACKENDS = {
    # Albert a besoin du correctif tool_choice (son schéma déclare
    # «default: none»).
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

# Les backends des comptes : (client, identifiant) → Backend. Rechargés de
# leur magasin par refresh(), au démarrage, après chaque écriture et chaque
# changement de rôle d'un compte.
ACCOUNTS: dict[tuple[str, str], Backend] = {}
# Une relecture à la fois : sinon la plus ancienne, finie la dernière,
# remettrait un état périmé.
_refreshing = asyncio.Lock()
# Les clients des backends retirés, fermés après le délai d'une requête (ou
# à l'arrêt).
_retired: set[httpx2.AsyncClient] = set()


def visible() -> dict[str, Backend]:
    """Les backends que voit le client de la requête, par nom : ceux du
    TOML, les siens, puis ceux que d'autres partagent (un des siens du même
    nom les masque)."""
    out = dict(BACKENDS)
    if not ACCOUNTS:
        return out
    me = fairshare.CLIENT.get() or ""
    for b in ACCOUNTS.values():
        if b.owner == me:
            out.setdefault(b.name, b)
    for b in ACCOUNTS.values():
        if b.shared:
            out.setdefault(b.name, b)
    return out


def permitted(b: Backend, model: str) -> bool:
    """Le client de la requête peut-il employer `model` (préfixé) de `b` ?
    Toujours sur ses propres backends ; ailleurs, selon les modèles permis
    de son compte."""
    return (b.owner is not None and b.owner == (fairshare.CLIENT.get() or "")) \
        or accounts.permitted(model)


def _find(model: str, table: dict[str, Backend]) -> Backend | None:
    m = model.lower()
    return next((b for name, b in table.items() if m.startswith(name + "/")), None)


def route_backend(payload, *, limited: bool = True) -> tuple[Backend | None, bool]:
    """Discriminant de routage : le préfixe du modèle. «<nom>/…» part
    vers le backend <nom> ; requête sans champ model → FALLBACK_BACKEND ;
    modèle sans préfixe reconnu, ou non permis au compte → None (no_route
    dit lequel). Renvoie (backend, préfixé) — préfixé impose de retirer
    «nom/». `limited` faux : les backends du TOML seuls, sans les modèles
    permis du compte — pour le modèle qu'un outil hébergé tient de la
    configuration."""
    model = ""
    if isinstance(payload, dict):
        model = str(payload.get("model", "") or "")
    if not model:
        # Un compte borné n'a pas de modèle par défaut : le backend de repli
        # en servirait un que l'admin n'a peut-être pas permis.
        if limited and accounts.MODELS.get() is not None:
            return None, False
        return FALLBACK_BACKEND, False
    b = _find(model, visible() if limited else BACKENDS)
    if b is None or (limited and not permitted(b, model)):
        return None, False
    return b, True


def unknown_prefix_message(model: str) -> str:
    """Texte du 400 «modèle sans préfixe» ; la réponse elle-même est
    construite par httputil.error_response, dans le dialecte du client."""
    prefixes = ", ".join(f"«{n}/»" for n in visible())
    log.warning("modèle %r sans préfixe backend reconnu → 400", model)
    return f"modèle «{model}» sans préfixe backend ; préfixes attendus : {prefixes}"


def no_route(dialect: str, model: str):
    """La réponse quand route_backend n'a rien rendu pour `model` : 403
    `model_not_allowed` si son backend existe mais que le compte ne peut
    pas l'employer, 400 `unknown_backend_prefix` sinon."""
    if _find(model, visible()) is not None or (
            not model and accounts.MODELS.get() is not None):
        log.warning("modèle %r non permis au compte → 403", model)
        return error_response(dialect, 403, "model_not_allowed",
                              f"modèle «{model}» non permis à ce compte")
    return error_response(dialect, 400, "unknown_backend_prefix",
                          unknown_prefix_message(model))


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


def open_client(b: Backend) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        base_url=b.url,
        timeout=httpx2.Timeout(b.timeout, connect=b.connect_timeout),
        follow_redirects=False,
        verify=b.verify_ssl,
    )


async def open_clients() -> None:
    """Un client HTTP par backend, ouvert au démarrage de l'application ;
    puis les backends des comptes."""
    for b in BACKENDS.values():
        b.client = open_client(b)


def _same(b: Backend, row: dict) -> bool:
    return (b.name, b.api, b.url, b.api_key) == \
        (row["name"], row["api"], row["url"], row["api_key"])


async def refresh(live) -> None:
    """Relit les backends des comptes de leur magasin. `live` : la fonction
    BLOQUANTE qui rend les clients dont les backends servent encore
    (auth.live_owners : un compte bloqué perd les siens, partagés compris).
    Un backend inchangé garde son client et son catalogue ; le client d'un
    backend retiré ou modifié se ferme après son délai de requête (une
    réponse en cours finit)."""
    def read():
        if not user_backends.ENABLED:
            return []
        owners = live()
        return [r for r in user_backends.rows() if r["owner"] in owners]
    async with _refreshing:
        rows = await asyncio.to_thread(read)
        fresh: dict[tuple[str, str], Backend] = {}
        for row in rows:
            key = (row["owner"], row["id"])
            b = ACCOUNTS.get(key)
            if b is None or not _same(b, row):
                try:
                    b = Backend(row["name"], {
                        "url": row["url"], "api_key": row["api_key"], "api": row["api"],
                        "connect_timeout": 15.0, "fair_share": False})
                except SystemExit as exc:    # une ligne abîmée : écartée seule
                    log.warning("backend de compte %s écarté : %s", row["name"], exc)
                    continue
                b.owner = row["owner"]
                b.client = open_client(b)
            b.shared = row["shared"]
            fresh[key] = b
        gone = [b for key, b in ACCOUNTS.items() if fresh.get(key) is not b]
        ACCOUNTS.clear()
        ACCOUNTS.update(fresh)
    for b in gone:
        _retired.add(b.client)
        asyncio.get_running_loop().call_later(
            b.timeout, lambda c=b.client: asyncio.ensure_future(_close(c)))


async def _close(client: httpx2.AsyncClient) -> None:
    if client in _retired:
        _retired.discard(client)
        await client.aclose()


async def close_clients() -> None:
    for b in [*BACKENDS.values(), *ACCOUNTS.values()]:
        if b.client is not None:
            await b.client.aclose()
    for client in list(_retired):
        await _close(client)
