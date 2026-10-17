"""
Qui parle au proxy : la clé présentée (en-tête), la session de la page
(/ui), et ce qui en découle pour la requête — son client (mémoire,
échanges d'outils, request_owner) et son api_key_id (stats.CLIENT).

  * proxy.api_keys vide et pas de comptes OIDC : proxy ouvert, un seul
    client «» ;
  * une clé du TOML : son propre client, sauf rattachée à un compte ;
  * une clé gérée (accounts.py) : le client de son COMPTE ;
  * la page : une session signée (UISession, cookie proxy_session) qui
    porte le compte OIDC ou le condensé de la clé du TOML — jamais la
    clé, jamais dans l'URL.

Le middleware (require_proxy_key) est enregistré par app.py ; UI_OPEN
dit ce que /ui sert sans clé ni session.
"""

import asyncio
import hmac
from typing import NamedTuple

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.sessions import SessionMiddleware

from . import accounts
from . import fairshare
from . import files
from . import oidc
from . import stats
from . import tools
from .settings import PROXY_API_KEYS, log
from .httputil import dialect_of, error_response


UI_PREFIX = "/ui"
# L'ancien cookie de /ui, qui portait la clé EN CLAIR : plus jamais posé,
# effacé à l'entrée et à la sortie. La page tient désormais une session
# signée (UISession), qui ne porte que le condensé de la clé.
LEGACY_COOKIE = "proxy_key"
# Ce que /ui sert SANS clé, même l'auth active : la page (c'est elle qui
# demande la clé quand il en faut une), ses fichiers, l'entrée et la sortie.
UI_OPEN = {("GET", "/"), ("GET", UI_PREFIX), ("HEAD", UI_PREFIX),
           ("GET", UI_PREFIX + "/sw.js"),
           ("POST", UI_PREFIX + "/login"), ("POST", UI_PREFIX + "/logout"),
           ("GET", UI_PREFIX + "/auth"), ("GET", UI_PREFIX + "/auth/login"),
           ("GET", oidc.CALLBACK_PATH)}


def ui_open(request: Request) -> bool:
    path = request.url.path
    return (request.method, path) in UI_OPEN or (
        request.method in ("GET", "HEAD")
        and path.startswith(UI_PREFIX + "/static/"))


def auth_required() -> bool:
    """Faut-il une clé ? Oui dès que proxy.api_keys en nomme une, ou que
    les comptes OIDC existent (leurs clés valent alors à côté de celles
    du TOML)."""
    return bool(PROXY_API_KEYS) or oidc.ENABLED


def account_owner(uid: int) -> str:
    """Le client d'un COMPTE : ce que partagent toutes ses clés et la page
    connectée (mémoire, échanges d'outils), et le api_key_id des requêtes
    du chat de la page. Un condensé comme tools.owner d'une clé — «\\0»
    ne peut pas commencer une clé reçue dans un en-tête."""
    return tools.owner(f"\0account:{uid}")


def request_owner(request: Request) -> str:
    """Le client de la requête, posé par le middleware : condensé de la clé
    du TOML, compte d'une clé gérée ou de la session de la page ; «» pour
    un proxy ouvert (ou sans requête : une boucle lancée en direct)."""
    return getattr(getattr(request, "state", None), "owner", "")


def ui_client(request: Request) -> str:
    """Le client de la page : celui de la session (ou de l'en-tête),
    ou du compte OIDC connecté ; «» pour un proxy ouvert — celui que
    reçoit l'outil `memory` d'un appel fait avec la même clé."""
    return request_owner(request)


def request_user(request: Request) -> dict | None:
    """Le compte de la session OIDC de la page, None sinon."""
    return getattr(getattr(request, "state", None), "user", None)


def valid_key(token: str) -> bool:
    """La clé présentée est-elle une de proxy.api_keys ?"""
    # En octets : compare_digest refuse une chaîne non ASCII (TypeError),
    # et un jeton accentué ferait tomber la requête en 500.
    presented = token.encode("utf-8", "surrogatepass")
    return any(hmac.compare_digest(presented, k.encode("utf-8"))
               for k in PROXY_API_KEYS)


def is_ui_path(path: str) -> bool:
    # «/» y est inclus : il ne fait que rediriger vers /ui.
    return path == "/" or path == UI_PREFIX or path.startswith(UI_PREFIX + "/")


def client_token(request: Request) -> str:
    """Clé présentée par le client : «Authorization: Bearer» (OpenAI) ou
    «x-api-key» (Anthropic). Jamais dans l'URL ni dans un cookie : un
    NAVIGATEUR entre par POST /ui/login, et la page tient ensuite une
    session signée qui ne porte que le condensé de la clé (ui_key)."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:]
    return request.headers.get("x-api-key", "")


class Who(NamedTuple):
    """Qui présente une clé : son api_key_id (les statistiques), son
    client (mémoire, échanges d'outils, partage équitable) et son compte
    (None : clé du TOML seule)."""
    api_key_id: str
    client: str
    account: int | None


async def key_client(token: str) -> Who | None:
    """La clé présentée → Who, None si elle ne vaut rien. Une clé du TOML
    est son propre client, sauf rattachée à un compte (accounts.bind) ;
    une clé gérée (accounts.py) a pour client son COMPTE — toutes ses
    clés partagent sa mémoire. Chacune garde son propre api_key_id dans
    les statistiques."""
    if not token:
        return None
    digest = tools.owner(token)
    if valid_key(token):
        if not oidc.ENABLED:
            return Who(digest, digest, None)
        link = await asyncio.to_thread(accounts.bound, digest)
        if link is None:
            return Who(digest, digest, None)
        # Rattachée : elle suit son compte, bloqué ou en attente compris.
        uid = link["user_id"]
        return Who(digest, account_owner(uid), uid) if link["active"] \
            else None
    if not oidc.ENABLED:
        return None
    hit = await asyncio.to_thread(accounts.resolve, digest)
    if hit is None:
        return None
    if accounts.touch_due(hit["id"]):
        await asyncio.to_thread(accounts.touch, hit["id"])
    uid = hit["user_id"]
    return Who(digest, account_owner(uid), uid)


def identify(request: Request, who: Who) -> None:
    """Pose sur la requête ce qui en découle : son api_key_id pour ses
    lignes de statistiques (stats.CLIENT), son client pour ce que ses
    outils gardent (request_owner) et pour le partage équitable
    (fairshare). Des variables de contexte : tout ce que la route lance
    en hérite."""
    stats.CLIENT.set(who.api_key_id)
    fairshare.CLIENT.set(who.client)
    fairshare.ACCOUNT.set(who.account)
    request.state.owner = who.client


# Ce qu'un compte en attente ou bloqué peut encore voir de /ui : de quoi
# savoir où il en est (le reste de UI_OPEN est déjà ouvert à tous).
UI_INACTIVE = {("GET", UI_PREFIX + "/session")}


async def ui_account(request: Request) -> JSONResponse | None:
    """/ui avec les comptes OIDC : la session de la page, et elle seule —
    une clé du proxy n'ouvre pas la page. Pose le compte et son client
    sur la requête ; rend la réponse de refus s'il n'y en a pas."""
    uid = request.session.get("uid")
    user = await asyncio.to_thread(accounts.user, uid) \
        if isinstance(uid, int) else None
    if user is None:
        request.session.pop("uid", None)
        return error_response("openai", 401, "login_required",
                              "connexion requise (GET /ui/auth/login)")
    request.state.user = user
    if user["role"] not in accounts.ACTIVE \
            and (request.method, request.url.path) not in UI_INACTIVE:
        return error_response(
            "openai", 403, "account_inactive",
            "compte en attente de validation par un admin"
            if user["role"] == "pending" else "compte bloqué")
    owner = account_owner(user["id"])
    identify(request, Who(owner, owner, user["id"]))
    return None


async def require_proxy_key(request: Request, call_next):
    # /v1/files/<jeton>/<nom> : ouvert par un NAVIGATEUR, qui n'envoie pas
    # la clé du proxy. Le jeton du chemin vaut droit d'accès (files.py).
    served = request.method in ("GET", "HEAD") \
        and files.is_link(request.url.path)
    if not auth_required() or request.url.path == "/healthz" or served \
            or ui_open(request):
        return await call_next(request)
    if oidc.ENABLED and is_ui_path(request.url.path):
        if (denied := await ui_account(request)) is not None:
            return denied
        return await call_next(request)
    if is_ui_path(request.url.path) and (digest := ui_key(request)):
        identify(request, Who(digest, digest, None))
        return await call_next(request)
    who = await key_client(client_token(request))
    if who is None:
        log.warning(
            "clé proxy absente ou invalide : %s %s → 401",
            request.method, request.url.path,
        )
        return error_response(
            dialect_of(request), 401, "invalid_api_key",
            "clé API du proxy absente ou invalide (en-tête "
            "«Authorization: Bearer <clé>» ou «x-api-key: <clé>» attendu)",
        )
    identify(request, who)
    return await call_next(request)


def toml_digests() -> set[str]:
    """Les condensés des clés de proxy.api_keys (tools.owner)."""
    return {tools.owner(k) for k in PROXY_API_KEYS}


def ui_key(request: Request) -> str:
    """La clé du TOML de la session de la page (mode clé, sans OIDC) : son
    condensé, s'il est encore celui d'une clé de proxy.api_keys — retirer
    la clé du TOML ferme aussi la page. «» sinon."""
    digest = request.session.get("kd") if "session" in request.scope else None
    return digest if isinstance(digest, str) and digest in toml_digests() \
        else ""


def scope_https(scope) -> bool:
    """Le navigateur parle-t-il HTTPS ? Directement, ou derrière un reverse
    proxy qui termine le TLS (Nginx Proxy Manager) et le dit par
    `X-Forwarded-Proto` — uvicorn ne le croit que de 127.0.0.1, et NPM est
    un autre conteneur. L'en-tête ne sert QU'À cela : poser `Secure` sur le
    cookie de /ui. Un client qui le falsifie ne peut que rendre son propre
    cookie plus strict."""
    proto = next((v.decode("latin-1") for k, v in scope.get("headers", ())
                  if k == b"x-forwarded-proto"), "")
    return scope.get("scheme") == "https" \
        or proto.split(",")[0].strip().lower() == "https"


class UISession:
    """La session de la page, dès qu'il faut une clé : le SessionMiddleware
    de Starlette (cookie signé `proxy_session`, HttpOnly, SameSite=Lax —
    le retour d'un fournisseur OIDC est une navigation venue d'ailleurs).
    Elle porte le compte OIDC (`uid`) ou le condensé de la clé du TOML
    (`kd`), jamais la clé. Deux instances, construites à la première
    requête (leur clé dérive de data/client.secret, qu'on ne lit pas à
    l'import) : l'une pose `Secure`, choisie quand le navigateur parle
    HTTPS (scope_https). Proxy ouvert : rien, ni cookie ni
    request.session. Ajoutée APRÈS le middleware d'auth, donc autour de
    lui : la session y est déjà lue."""

    def __init__(self, app):
        self.app = app
        self.inner: dict[bool, SessionMiddleware] = {}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not auth_required():
            return await self.app(scope, receive, send)
        secure = scope_https(scope)
        if secure not in self.inner:
            days = oidc.SESSION_DAYS if oidc.ENABLED else 30
            self.inner[secure] = SessionMiddleware(
                self.app,
                secret_key=oidc.session_secret(tools.owner_secret()),
                session_cookie=oidc.SESSION_COOKIE,
                max_age=int(days * 86400), same_site="lax",
                https_only=secure)
        return await self.inner[secure](scope, receive, send)
