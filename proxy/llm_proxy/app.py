"""
L'application FastAPI : l'assemblage des routes et des middlewares. Les
routes vivent par domaine, à côté :

  startup.py      le démarrage et l'arrêt (lifespan), ce qu'il dit au journal
  health.py       /healthz, /ui/health
  tool_routes.py  outils hébergés exposés : /v1/tools, /mcp, fichiers rendus
  relay_routes.py /v1/audio/voices et le catch-all (forward_post_paths,
                  404 local sinon)
  auth.py       clés, session de la page, middleware d'auth (rôle 5)
  relaying.py   le relais vers les backends : Call, gate, forward
  catalog.py    le catalogue des modèles (GET /v1/models, rôle 3)
  surfaces.py   /v1/chat/completions, /v1/messages, /v1/responses
  tool_loop.py  la boucle des outils hébergés, commune aux surfaces
  usage_api.py  l'Usage API (rôle 4bis)
  ui_routes.py  les routes du tableau de bord (/ui/*)
  httputil.py   forme des erreurs, corps JSON

Rôles du proxy :
  1. peut injecter tool_choice quand `tools` est présent sans `tool_choice`
     (le défaut du schéma Albert est "none", ce qui casse le tool calling).
     RIEN N'EST INJECTÉ par défaut : cela s'active PAR BACKEND via
     [backends.<nom>].force_tool_choice ;
  2. porte les clés upstream pour tous les clients ;
  3. multi-backends, routés par le PRÉFIXE du modèle — voir backends.py.
     GET /v1/models interroge les backends en direct et fusionne les
     catalogues, chaque id exposé préfixé ;
  4. ne transmet QUE les routes nécessaires (proxy.forward_post_paths,
     /v1/chat/completions, /v1/models, GET /v1/audio/voices) — toute autre
     URL reçoit un 404 local, rien n'est relayé aveuglément aux backends.
     Un corps multipart (transcription audio, édition d'image) est routé
     par son champ model, préfixe retiré, comme un corps JSON (multipart.py) ;
  4bis. GET /v1/organization/usage/completions : l'Usage API d'OpenAI,
     servie depuis les compteurs du proxy (SQLite, une ligne par requête
     — voir stats.py). C'est la SEULE lecture des statistiques. GET /ui
     en est le tableau de bord — une page HTML statique
     (web/templates/index.html) que web/static/dashboard.js remplit en
     appelant cette même route sous /ui/usage, en vues All / Week / Day ;
     GET /v1/organization/usage/tools en est la route sœur, de même
     forme, pour les exécutions d'outils hébergés (/ui/usage/tools) ;
  4ter. la surface Anthropic (anthropic_api.py), si [anthropic].enabled :
     POST /v1/messages et /v1/messages/count_tokens, et GET /v1/models à
     la forme Anthropic quand la requête porte `anthropic-version`. Un
     client Claude Code s'y branche avec ANTHROPIC_BASE_URL. Son outil
     serveur `web_search_…` (la sous-requête de son `WebSearch`) est
     exécuté ici quand la recherche hébergée est active — de même
     l'outil serveur `web_fetch_…` d'un client du SDK ;
  4quater. la surface Responses (responses_api.py), si [responses].enabled :
     POST /v1/responses, traduit vers /v1/chat/completions comme la
     surface Anthropic. Un client Codex CLI s'y branche avec
     wire_api = "responses". Les outils HÉBERGÉS (paquet tools/ :
     recherche web) y sont exécutés ici même : le backend est relancé
     avec leurs résultats jusqu'à la réponse finale — `hosted_loop`
     (tool_loop.py), la boucle commune aux surfaces ;
  4quinquies. les outils hébergés sur /v1/chat/completions (chat_api.py),
     si [chat].hosted_tools : une requête qui DÉCLARE `{"type":
     "web_search"}` dans `tools` passe par la même boucle et reçoit une
     réponse chat/completions ordinaire ; toute autre est relayée brute.
     [chat].always nomme des outils présentés D'OFFICE, sans déclaration
     (une interface de chat n'en fait pas). Les appels hébergés, que le client ne voit pas, sont réinsérés dans
     son historique à la requête suivante ([chat].memory) ;
  5. auth optionnelle du proxy lui-même : proxy.api_keys (liste vide par
     défaut = ouvert) exige des clients un «Authorization: Bearer <clé>»
     à la OpenAI — ou «x-api-key: <clé>», à l'Anthropic — (401 sinon,
     /healthz exempté). C'est la clé DU PROXY : jamais relayée aux
     backends quand l'auth est active. Avec [oidc] (oidc.py), des
     COMPTES : la page s'ouvre par le fournisseur OIDC, et chaque compte
     y crée ses propres clés (accounts.py), valables à côté de celles du
     TOML ;

Chaque requête relayée est portée par UN objet `Call` (relaying.py) :
c'est lui qui écrit la ligne de stats, une fois, quel que soit le chemin
de sortie.

TOUTE la configuration vit dans data/config.toml (voir config.py).
Tout ce qui est spécifique à Albert (limiteur de quotas, familles,
association routeurs ↔ modèles) vit dans albert.py.
"""

import mimetypes
import os

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import auth
from . import catalog
from . import health
from . import relay_routes
from . import surfaces
from . import tool_routes
from . import ui_routes
from . import usage_api
from .auth import UISession
from .startup import lifespan
from .ui_routes import WEB_DIR

app = FastAPI(title="albert-proxy", lifespan=lifespan)

# Feuille de style et script du tableau de bord : servis par le proxy
# lui-même, aucun CDN. Monté AVANT le catch-all, qui sinon répondrait
# 404. Le mount reste sous /ui : l'auth du proxy s'y applique.
# Le manifeste de l'application installable : un type que `mimetypes` ne
# connaît pas toujours.
mimetypes.add_type("application/manifest+json", ".webmanifest")
app.mount("/ui/static", StaticFiles(directory=os.path.join(WEB_DIR, "static")),
          name="static")

# L'auth (auth.py) : le middleware, puis la session de la page AUTOUR de
# lui (ajoutée après, donc exécutée avant : la session y est déjà lue).
app.middleware("http")(auth.require_proxy_key)
app.add_middleware(UISession)

# Les routes des modules, le relais (son catch-all) EN DERNIER, qui sinon
# les prendrait toutes.
for module in (surfaces, catalog, usage_api, ui_routes, health, tool_routes):
    app.include_router(module.router)


@app.get("/")
async def root():
    return RedirectResponse("/ui")


app.include_router(relay_routes.router)
