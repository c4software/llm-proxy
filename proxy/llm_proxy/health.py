"""
L'état du proxy : GET /healthz (« ok » seul sans clé, le détail avec) et
GET /ui/health, le même détail pour la page sous sa session. Le détail
préremplit aussi le générateur de configuration de la page (config.js) :
jamais un secret.
"""

from fastapi import APIRouter, Request

from . import accounts
from . import albert
from . import anthropic_api
from . import chat_api
from . import fairshare
from . import files
from . import mcp_server
from . import oidc
from . import responses_api
from . import tools
from .auth import auth_required, client_token, key_client
from .backends import BACKENDS
from .catalog import CHAT_TYPES, always_shown
from .settings import EXEMPT_SUFFIXES, TOOL_CHOICE
from .ui_routes import ui_login_mode

router = APIRouter()


@router.get("/healthz")
async def healthz(request: Request):
    """Vivant ? Sans clé (auth active) : `{"status": "ok"}`, et rien de
    plus — ni URL de backend, ni modèles, ni outils : c'est ce que lit le
    HEALTHCHECK du conteneur. Avec une clé valide (en-tête), ou proxy
    ouvert : l'état détaillé (health_detail). La page le lit sous sa
    session, par GET /ui/health."""
    if auth_required() and await key_client(client_token(request)) is None:
        return {"status": "ok"}
    return health_detail()


@router.get("/ui/health")
async def ui_health():
    """L'état détaillé, pour la page : sous /ui, la session suffit."""
    return health_detail()


def health_detail() -> dict:
    """L'état du déploiement : backends, surfaces, outils, caches."""
    return {
        "status": "ok",
        # Le partage équitable : part par défaut, clients suivis, en attente.
        "fair_share": fairshare.snapshot(),
        "tool_choice": TOOL_CHOICE,
        "auth_required": auth_required(),
        # Comment la page s'ouvre : «oidc» (comptes), «key» (une clé du
        # TOML), None (proxy ouvert).
        "ui_login": ui_login_mode(),
        # Les comptes ([oidc]) : de quoi préremplir le générateur de la
        # page — jamais client_id ni client_secret.
        "oidc": {"enabled": oidc.ENABLED, "issuer": oidc.ISSUER,
                 "redirect_uri": oidc.REDIRECT_URI,
                 "new_accounts": accounts.NEW_ROLE,
                 "session_days": oidc.SESSION_DAYS,
                 "max_keys_per_account": accounts.MAX_KEYS,
                 "label": oidc.LABEL},
        "anthropic": {
            "enabled": anthropic_api.ENABLED,
            "model_map": anthropic_api.MODEL_MAP,
        },
        "responses": {"enabled": responses_api.ENABLED},
        # Le serveur MCP (POST /mcp) : de quoi savoir, sans clé, si un
        # client MCP (pi, omp, Claude Code) peut s'y brancher.
        "mcp_server": {"enabled": mcp_server.ENABLED},
        "chat": {"hosted_tools": chat_api.ENABLED,
                 # Présentés d'office EN CE MOMENT : les noms de
                 # [chat].always qui sont ceux d'un outil actif.
                 "always": always_shown() if chat_api.ENABLED else [],
                 "always_except": chat_api.ALWAYS_EXCEPT,
                 "memory": chat_api.MEMORY_ENABLED,
                 "memory_entries": len(chat_api.MEMORY),
                 "memory_chars": chat_api.MEMORY.size},
        # Le magasin des fichiers rendus par les outils (files.py) :
        # peut-il écrire des liens, et ce qu'il garde.
        "files": {"public_url": bool(files.PUBLIC_URL),
                  "entries": len(files.STORE), "bytes": files.STORE.size},
        "tools": {
            "enabled": [t.name for t in tools.enabled()],
            "max_calls": tools.MAX_CALLS,
            "memory_entries": len(tools.MEMORY),
            # Cache web (tools/webcache.py) : pages et recherches gardées
            # quelques minutes, et ce qu'il a épargné depuis le démarrage.
            "web_cache": {"ttl": tools.webcache.TTL,
                          "entries": len(tools.webcache.CACHE),
                          "bytes": tools.webcache.CACHE.size,
                          "hits": tools.webcache.CACHE.hits,
                          "misses": tools.webcache.CACHE.misses},
            # Cache des transcriptions (le texte, jamais l'audio).
            # Serveurs MCP de la configuration : joints ou non, leur
            # protocole, leurs outils — jamais leurs en-têtes.
            "mcp": tools.mcp.status(),
            "transcribe_cache": {"ttl": tools.transcribe.CACHE.ttl,
                                 "entries": len(tools.transcribe.CACHE),
                                 "hits": tools.transcribe.CACHE.hits,
                                 "misses": tools.transcribe.CACHE.misses},
        },
        "backends": {
            name: {
                "url": b.url,
                "quotas": b.quotas,
                "key_injection": bool(b.api_key),
                "tool_choice": b.tool_choice or False,
                "max_tokens": b.max_tokens or None,
                "images": b.images,
                "fair_share": b.fair_share,
                "tokenize_path": b.tokenize_path or None,
                "timeout": b.timeout,
                "meta_timeout": b.meta_timeout,
                "connect_timeout": b.connect_timeout,
                "last_seen_models": sorted(f"{name}/{m}" for m in b.models),
                # Ceux qu'on peut proposer à un client de chat (ni image,
                # ni voix, ni embeddings) : le tableau de bord y prend ses
                # exemples. Vide tant que le catalogue n'a pas été lu.
                "chat_models": sorted(
                    f"{name}/{m}" for m in b.models
                    if b.model_types.get(m, "text-generation") in CHAT_TYPES),
                **(b.quota_state.snapshot() if b.quotas else {}),
            }
            for name, b in BACKENDS.items()
        },
        "generic_limits": {"rpm": albert.GENERIC_RPM, "tpm": albert.GENERIC_TPM},
        "max_queue_seconds": albert.MAX_QUEUE_SECONDS,
        "exempt_paths": list(EXEMPT_SUFFIXES),
    }
