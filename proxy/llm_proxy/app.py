"""
L'application FastAPI : l'assemblage des routes et des middlewares. Les
routes vivent par domaine, à côté (carte du paquet : __init__.py ; vue
d'ensemble : docs/architecture.md).

Ce que le proxy garantit :
  * ne transmet QUE les routes nécessaires (proxy.forward_post_paths,
    /v1/chat/completions, /v1/models, GET /v1/audio/voices) — toute autre
    URL reçoit un 404 local, rien n'est relayé aveuglément aux backends ;
  * routage par le PRÉFIXE du modèle (backends.py), corps multipart
    compris (multipart.py) ;
  * tool_choice n'est injecté que si [backends.<nom>].force_tool_choice
    le demande (le défaut du schéma Albert, "none", casse le tool calling) ;
  * auth optionnelle (proxy.api_keys, comptes OIDC) : la clé DU PROXY
    n'est jamais relayée aux backends quand l'auth est active ;
  * chaque requête relayée est portée par UN objet `Call` (relaying.py),
    qui écrit la ligne de stats une fois, quel que soit le chemin de sortie.

Toute la configuration vit dans data/config.toml (config.py) ; ce qui est
spécifique à Albert, dans albert.py.
"""

import mimetypes

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

from .core import config
from .core.httputil import error_response

from .access import auth
from .access.auth import UISession
from .exposure import tool_routes
from .startup import lifespan
from .surfaces import endpoints, relay_routes
from .ui import (
    config_view,
    conversation_routes,
    dashboard,
    health,
    linux_relay,
    static_files,
    usage_api,
    repo_routes,
    voice_routes,
    webhook_routes,
)

app = FastAPI(title="albert-proxy", lifespan=lifespan)

# Le manifeste de l'application installable : un type que `mimetypes` ne
# connaît pas toujours.
mimetypes.add_type("application/manifest+json", ".webmanifest")

# L'auth (auth.py) : le middleware, puis la session de la page AUTOUR de
# lui (ajoutée après, donc exécutée avant : la session y est déjà lue).
app.middleware("http")(auth.require_proxy_key)
app.add_middleware(UISession)


# Premier démarrage sans data/config.toml (config.SETUP) : seule la page
# (et /healthz) répond ; l'API attend une configuration plutôt que de
# relayer vers le backend par défaut. Ajouté en dernier : exécuté avant
# l'auth, qui n'a encore rien à vérifier.
@app.middleware("http")
async def setup_gate(request: Request, call_next):
    path = request.url.path
    if config.SETUP and not (path == "/" or path == "/healthz" or path == "/ui"
                             or path.startswith("/ui/")):
        return error_response("openai", 503, "setup_required",
                              "proxy sans configuration : ouvrir /ui pour la générer, "
                              f"la poser dans {config.CONFIG_PATH}, puis redémarrer")
    return await call_next(request)

# Les routes des modules, le relais (son catch-all) EN DERNIER, qui sinon
# les prendrait toutes.
for module in (endpoints, usage_api, static_files, dashboard,
               conversation_routes, voice_routes, config_view, health, tool_routes,
               linux_relay, webhook_routes, repo_routes):
    app.include_router(module.router)


@app.get("/")
async def root():
    return RedirectResponse("/ui")


app.include_router(relay_routes.router)
