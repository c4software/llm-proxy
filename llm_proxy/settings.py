"""
Réglages généraux du proxy : la table [proxy] de data/config.toml, lue
une fois et exposée en constantes typées. Les autres sections ont leur
propre module (quotas → albert.py, stats → stats.py, backends →
backends.py) : chacun lit ce qui le regarde, personne ne centralise tout.
"""

import logging

from . import config

TOOL_CHOICE = config.text("proxy.tool_choice", "auto")
TIMEOUT = config.num("proxy.upstream_timeout", 600)
# Court : un backend lent ne doit pas bloquer le catalogue.
META_TIMEOUT = config.num("proxy.meta_timeout", 5.0)

# Liste vide = proxy ouvert. /healthz reste accessible sans clé.
PROXY_API_KEYS = frozenset(config.strings("proxy.api_keys"))

# En plus des handlers dédiés ; tout le reste → 404 local.
FORWARD_POST_PATHS = frozenset(
    "/" + p.strip("/") for p in config.strings(
        "proxy.forward_post_paths",
        ("/v1/completions", "/v1/embeddings", "/v1/rerank",
         "/v1/audio/transcriptions", "/v1/ocr"),
    )
)

# Suffixes d'URL jamais temporisés par le limiteur.
EXEMPT_SUFFIXES = tuple(config.strings(
    "proxy.exempt_paths",
    ("/embeddings", "/rerank", "/audio/transcriptions", "/ocr"),
))

# En-têtes qui ne se relaient pas : ils décrivent CETTE connexion.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
    "host", "content-length", "accept-encoding",
}

logging.basicConfig(
    level=config.text("proxy.log_level", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("albert-proxy")


def is_exempt(path: str) -> bool:
    return ("/" + path.strip("/")).endswith(EXEMPT_SUFFIXES)
