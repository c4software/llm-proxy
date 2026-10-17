"""
La connexion de la page (/ui) par OIDC, avec Authlib — le client
OpenID Connect de Starlette : découverte du fournisseur
(<issuer>/.well-known/openid-configuration), flux « authorization code »
avec PKCE (S256), `state` et `nonce` gardés dans la session, et
validation de l'id_token (signature par le JWKS du fournisseur, iss, aud,
exp, nonce). Rien n'est réécrit à la main.

Actif quand [oidc].issuer et [oidc].client_id sont posés. Alors :
  * /ui ne s'ouvre plus avec une clé du proxy, seulement par une session
    OIDC (cookie signé, SESSION_COOKIE) ;
  * chaque compte (accounts.py) gère ses propres clés, des uuid4, valables
    sur /v1, /mcp… à côté de celles du TOML (proxy.api_keys), qui restent.

La session : un cookie signé (auth.UISession, le SessionMiddleware de
Starlette), qui porte l'identifiant du compte et, le temps d'une
connexion, ce qu'Authlib garde entre la redirection et le retour.
SameSite=Lax : le retour du fournisseur est une navigation venue d'un
autre site, qui doit le porter ; Secure dès que le navigateur parle
HTTPS. Les routes /ui qui écrivent ou dépensent exigent un corps JSON
(httputil.json_only), qu'une page tierce ne peut pas envoyer.
"""

import hashlib
import hmac

from authlib.integrations.starlette_client import OAuth, OAuthError  # noqa: F401

from . import config, files

ISSUER = config.text("oidc.issuer", "").rstrip("/")
CLIENT_ID = config.text("oidc.client_id", "")
CLIENT_SECRET = config.text("oidc.client_secret", "")
SCOPES = config.strings("oidc.scopes", ("openid", "email", "profile"))
# L'adresse de retour déclarée chez le fournisseur. Vide : celle de
# [files].public_url, sinon déduite de la requête (derrière un reverse
# proxy, mieux vaut la poser).
REDIRECT_URI = config.text("oidc.redirect_uri", "")
# Durée d'une session de la page (jours).
SESSION_DAYS = config.num("oidc.session_days", 7)
# Le libellé du bouton de connexion.
LABEL = config.text("oidc.label", "Se connecter")

ENABLED = bool(ISSUER and CLIENT_ID)
SESSION_COOKIE = "proxy_session"
CALLBACK_PATH = "/ui/auth/callback"

oauth = OAuth()


def register(**client_kwargs) -> None:
    """Déclare le fournisseur auprès d'Authlib (un registre neuf : la
    découverte et le JWKS seront relus). `client_kwargs` : passés au
    client httpx2 d'Authlib (les tests y mettent un faux fournisseur)."""
    global oauth
    oauth = OAuth()
    oauth.register(
        "idp",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET or None,
        server_metadata_url=ISSUER + "/.well-known/openid-configuration",
        client_kwargs={"scope": " ".join(SCOPES),
                       "code_challenge_method": "S256",
                       "timeout": 10.0, **client_kwargs},
    )


if ENABLED:
    register()


def client():
    return oauth.create_client("idp")


def redirect_uri(base_url: str) -> str:
    """L'URL de retour : [oidc].redirect_uri, sinon [files].public_url,
    sinon l'adresse sous laquelle la requête est arrivée."""
    if REDIRECT_URI:
        return REDIRECT_URI
    return (files.PUBLIC_URL or base_url.rstrip("/")) + CALLBACK_PATH


def session_secret(owner_secret: bytes) -> str:
    """La clé de signature des sessions, dérivée du secret des condensés
    (data/client.secret) : rien de plus à garder."""
    return hmac.new(owner_secret, b"llm-proxy ui session",
                    hashlib.sha256).hexdigest()
