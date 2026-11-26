"""
La PAGE et l'entrée : le HTML du tableau de bord, son service worker,
la connexion par clé et la déconnexion ; le routeur commun à toutes les
routes /ui de ce paquet.
"""

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from ...core import identity
from ...access import auth, oidc
from ...core import config
from ...core.settings import log
from .. import static_files
from ...access.auth import LEGACY_COOKIE, UI_PREFIX, scope_https, valid_key
from ...core.httputil import error_response, json_only, parse_json


router = APIRouter()


# Le chat du tableau de bord : le cookie de la page y DÉPENSE des tokens.
# Inactif par défaut ; sans lui, le cookie ne sert qu'à lire.
UI_CHAT = config.flag("ui.chat", False)


# Modèle présélectionné (<backend>/<modèle>) pour chaque nouvelle
# conversation. Vide : le dernier choisi dans ce navigateur, sinon le premier.
UI_CHAT_MODEL = config.text("ui.chat_model", "").strip()


# Les en-têtes de sécurité de la page. Pas de Content-Security-Policy :
# - la page compile à la volée le gabarit d'un composant d'extension (il
#   lui faudrait 'unsafe-eval') et a un script en ligne ;
# - surtout, les cadres `srcdoc` (artifacts, bacs, VM Linux) HÉRITENT de
#   la politique de la page : une script-src stricte casserait leur code,
#   qui est en ligne par construction. Chacun pose la sienne dans son
#   document (chat/artifacts.js, tools/sandbox/bac.js).
# Le cadrage est refusé par X-Frame-Options plutôt que par
# `frame-ancestors`, pour la même raison : un en-tête CSP, même réduit à
# cette directive, serait hérité par ces cadres.
SECURITY_HEADERS = {
    "X-Frame-Options": "SAMEORIGIN",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


# Sans build (web/dist absent : une installation depuis les sources sans
# `npm run build`), la page le dit plutôt qu'un 404.
NOT_BUILT = """<!doctype html><meta charset="utf-8"><title>llm-proxy</title>
<p style="font:16px system-ui,sans-serif;margin:40px">La page n'est pas construite :
<code>cd proxy/wasm &amp;&amp; npm ci &amp;&amp; npm run build</code>, puis recharger.</p>"""


@router.get("/ui", response_class=HTMLResponse)
async def ui_page(request: Request):
    """Tableau de bord : la page construite par Vite (web/dist). Les
    chiffres sont lus côté client sur /ui/usage, toutes les 5 s, sans
    jamais recharger la page."""
    html = static_files.page()
    # La page elle-même n'est jamais gardée : c'est elle qui porte les noms
    # à empreinte de ses fichiers.
    if html is None:
        return HTMLResponse(NOT_BUILT, status_code=503,
                            headers={"Cache-Control": "no-cache", **SECURITY_HEADERS})
    return HTMLResponse(html, headers={"Cache-Control": "no-cache",
                                       **SECURITY_HEADERS})


def drop_legacy_cookie(response: Response, request: Request) -> None:
    """Efface l'ancien cookie de /ui (la clé en clair), s'il traîne."""
    if LEGACY_COOKIE in request.cookies:
        response.delete_cookie(LEGACY_COOKIE, httponly=True,
                               samesite="strict",
                               secure=scope_https(request.scope))


# Le service worker de l'application installable. Il ne garde RIEN : seule
# une navigation qui échoue (proxy injoignable) reçoit une page « hors ligne ».
# Servi sous /ui (pas /ui/static) avec Service-Worker-Allowed, pour que sa
# portée couvre /ui lui-même.
SERVICE_WORKER = """// llm-proxy : service worker de l'application installable.
const OFFLINE = `<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>llm-proxy — hors ligne</title>
<body style="margin:0;min-height:100vh;display:grid;place-items:center;
font:16px system-ui,sans-serif;background:#1a1917;color:#f2efe8">
<div style="text-align:center;padding:24px"><p style="font:28px Georgia,serif;margin:0 0 8px">
Proxy injoignable</p><p style="color:#9c968b;margin:0 0 18px">Vérifiez la connexion, puis réessayez.</p>
<button onclick="location.reload()" style="font:inherit;padding:8px 16px;border-radius:8px;
border:1px solid #e08b6d;background:#e08b6d;color:#fff">Réessayer</button></div>`;
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (event) => {
  if (event.request.mode !== "navigate") return;
  event.respondWith(fetch(event.request).catch(() => new Response(OFFLINE,
    {headers: {"content-type": "text/html; charset=utf-8"}})));
});
"""


@router.get("/ui/sw.js")
async def ui_service_worker():
    return Response(SERVICE_WORKER, media_type="text/javascript",
                    headers={"Service-Worker-Allowed": UI_PREFIX,
                             "Cache-Control": "no-cache",
                             "X-Content-Type-Options": "nosniff"})


@router.post("/ui/login")
async def ui_login(request: Request):
    """La clé saisie dans la page : vérifiée, puis son CONDENSÉ gardé dans
    la session de la page (jamais la clé elle-même)."""
    if (bad := json_only(request)) is not None:
        return bad
    if oidc.ENABLED:
        return error_response("openai", 404, "unknown_route",
                              "la page s'ouvre par OIDC (GET /ui/auth/login)")
    body = parse_json(await request.body())
    key = body.get("key") if isinstance(body, dict) else None
    if not auth.PROXY_API_KEYS:
        return {"ok": True}
    if not isinstance(key, str) or not key or not valid_key(key):
        log.warning("/ui/login : clé invalide → 401")
        return error_response("openai", 401, "invalid_api_key",
                              "clé du proxy invalide")
    request.session.clear()
    request.session["kd"] = identity.owner(key)
    response = JSONResponse({"ok": True})
    drop_legacy_cookie(response, request)
    return response


@router.post("/ui/logout")
async def ui_logout(request: Request):
    if (bad := json_only(request)) is not None:
        return bad
    response = JSONResponse({"ok": True})
    drop_legacy_cookie(response, request)
    if "session" in request.scope:
        request.session.clear()
    return response
