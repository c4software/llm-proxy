"""
Les COMPTES, sous /ui : la connexion OIDC, la sauvegarde, les clés
(créées, révoquées, rattachées) et les utilisateurs (rôles, parts).
"""

import asyncio
import os
import shutil
import tempfile
import uuid
from urllib.parse import quote
from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.background import BackgroundTask
from ... import tools
from ...access import accounts, auth, oidc
from ...core.settings import log
from ...storage import backup
from ...access.auth import request_user, toml_digests
from ...core.httputil import error_response, json_only, parse_json

from .page import router


# ── Comptes OIDC (oidc.py, accounts.py) ─────────────────────────────────

def ui_login_mode() -> str | None:
    if oidc.ENABLED:
        return "oidc"
    return "key" if auth.PROXY_API_KEYS else None


@router.get("/ui/auth")
async def ui_auth():
    """Ce que la page doit proposer pour entrer, sans clé ni session."""
    return {"mode": ui_login_mode(),
            "label": oidc.LABEL if oidc.ENABLED else None}


@router.get("/ui/auth/login")
async def ui_auth_login(request: Request):
    """Vers le fournisseur OIDC (Authlib : découverte, PKCE, state,
    nonce, gardés dans la session jusqu'au retour)."""
    if not oidc.ENABLED:
        return error_response("openai", 404, "unknown_route",
                              "OIDC inactif ([oidc].issuer, client_id)")
    try:
        return await oidc.client().authorize_redirect(
            request, oidc.redirect_uri(str(request.base_url)))
    except Exception as exc:          # fournisseur injoignable, découverte
        log.warning("OIDC : connexion impossible : %s", exc)
        return RedirectResponse(
            "/ui?auth_error=" + quote("fournisseur OIDC injoignable"), 303)


@router.get(oidc.CALLBACK_PATH)
async def ui_auth_callback(request: Request):
    """Le retour du fournisseur : Authlib échange le code et valide
    l'id_token ; le compte (iss, sub) est créé ou retrouvé, et la session
    ne porte plus que lui."""
    if not oidc.ENABLED:
        return error_response("openai", 404, "unknown_route", "OIDC inactif")
    try:
        token = await oidc.client().authorize_access_token(request)
        info = token.get("userinfo") or {}
        user = await asyncio.to_thread(
            accounts.login, info.get("iss", ""), info.get("sub", ""),
            info.get("email") or "",
            info.get("name") or info.get("preferred_username") or "")
    except (oidc.OAuthError, accounts.AccountError) as exc:
        log.warning("OIDC : retour refusé : %s", exc)
        return RedirectResponse(
            "/ui?auth_error=" + quote(f"connexion refusée ({exc})"), 303)
    except Exception as exc:          # jeton invalide, fournisseur muet
        log.warning("OIDC : retour en échec : %r", exc)
        return RedirectResponse(
            "/ui?auth_error=" + quote("connexion impossible"), 303)
    request.session.clear()
    request.session["uid"] = user["id"]
    log.info("OIDC : connexion du compte %d (%s, %s)", user["id"],
             user["email"] or user["name"], user["role"])
    return RedirectResponse("/ui", 303)


def accounts_off() -> JSONResponse | None:
    if not oidc.ENABLED:
        return error_response("openai", 404, "unknown_route",
                              "comptes inactifs ([oidc] dans config.toml)")
    return None


def admin_only(request: Request) -> JSONResponse | None:
    if (off := accounts_off()) is not None:
        return off
    if (request_user(request) or {}).get("role") != "admin":
        return error_response("openai", 403, "forbidden", "réservé aux admins")
    return None


@router.get("/ui/admin/backup")
async def ui_backup(request: Request):
    """Une sauvegarde de data/ à télécharger, la même archive que
    `python -m llm_proxy.backup`, faite dans un dossier temporaire effacé
    une fois l'archive partie. Réservée aux admins OIDC : elle contient le
    secret des condensés et la mémoire de tous."""
    if (bad := admin_only(request)) is not None:
        return bad
    tmp = tempfile.mkdtemp(prefix="llm-proxy-backup-")
    try:
        path = await asyncio.to_thread(backup.backup, tmp, 0)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    user = request_user(request)
    log.info("sauvegarde téléchargée par le compte %d (%s)", user["id"],
             user["email"] or user["name"])
    return FileResponse(
        path, media_type="application/gzip",
        filename=os.path.basename(path),
        headers={"Cache-Control": "no-store"},
        background=BackgroundTask(shutil.rmtree, tmp, True))


def account_error(exc: accounts.AccountError) -> JSONResponse:
    return error_response("openai", exc.status, "invalid_request_error",
                          str(exc))


@router.get("/ui/keys")
async def ui_keys(request: Request, all: int = 0):
    """Les clés du compte connecté, et celles du TOML qui lui sont
    rattachées ; `?all=1` (admin) : celles de tous, et toutes celles du
    TOML. Une clé du TOML ne se montre que par son condensé."""
    if (off := accounts_off()) is not None:
        return off
    user = request_user(request)
    if all and (bad := admin_only(request)) is not None:
        return bad
    data = await asyncio.to_thread(accounts.keys, None if all else user["id"])
    links = await asyncio.to_thread(accounts.bindings)
    toml = [{"api_key_id": d, "hint": d[:8], **links.get(
                d, {"user_id": None, "user": None})}
            for d in sorted(toml_digests())]
    if not all:
        toml = [k for k in toml if k["user_id"] == user["id"]]
    return {"object": "list", "data": data, "toml": toml}


@router.put("/ui/keys/toml/{digest}")
async def ui_toml_bind(request: Request, digest: str):
    """Rattache une clé du TOML (désignée par son condensé) à un compte,
    `{"user_id": <id>}`, ou l'en détache, `{"user_id": null}` (admin).
    Ce qui était rangé sous la clé seule n'est pas déplacé."""
    if (bad := admin_only(request)) is not None:
        return bad
    if (bad := json_only(request)) is not None:
        return bad
    if digest not in toml_digests():
        return error_response("openai", 404, "not_found",
                              "pas une clé de proxy.api_keys")
    body = parse_json(await request.body())
    uid = body.get("user_id") if isinstance(body, dict) else 0
    if uid is not None and (not isinstance(uid, int) or isinstance(uid, bool)):
        return error_response("openai", 400, "invalid_request_error",
                              "corps attendu : {\"user_id\": <id> | null}")
    try:
        await asyncio.to_thread(accounts.bind, digest, uid)
    except accounts.AccountError as exc:
        return account_error(exc)
    links = await asyncio.to_thread(accounts.bindings)
    return {"api_key_id": digest, "hint": digest[:8],
            **links.get(digest, {"user_id": None, "user": None})}


@router.post("/ui/keys")
async def ui_key_create(request: Request):
    """Une nouvelle clé pour le compte connecté : un uuid4, rendu ICI
    seulement (`key`) — le proxy n'en garde que le condensé."""
    if (off := accounts_off()) is not None:
        return off
    if (bad := json_only(request)) is not None:
        return bad
    body = parse_json(await request.body())
    label = body.get("label", "") if isinstance(body, dict) else ""
    if not isinstance(label, str):
        return error_response("openai", 400, "invalid_request_error",
                              "label : une chaîne")
    key = str(uuid.uuid4())
    try:
        record = await asyncio.to_thread(
            accounts.create_key, request_user(request)["id"], key,
            tools.owner(key), label)
    except accounts.AccountError as exc:
        return account_error(exc)
    return record | {"key": key}


@router.delete("/ui/keys/{kid}")
async def ui_key_revoke(request: Request, kid: str):
    """Révoque une clé : du compte connecté, ou de n'importe qui (admin)."""
    if (off := accounts_off()) is not None:
        return off
    user = request_user(request)
    try:
        return await asyncio.to_thread(
            accounts.revoke_key, kid,
            None if user["role"] == "admin" else user["id"])
    except accounts.AccountError as exc:
        return account_error(exc)


@router.get("/ui/users")
async def ui_users(request: Request):
    if (bad := admin_only(request)) is not None:
        return bad
    return {"object": "list", "data": await asyncio.to_thread(accounts.users)}


@router.patch("/ui/users/{uid}")
async def ui_user_update(request: Request, uid: int):
    """Un compte (admin) : son rôle (`role` : accepter un compte en
    attente, le bloquer, le promouvoir ; le dernier admin garde le sien)
    et/ou sa part dans le partage équitable (`share` : null = la part par
    défaut, 0 = illimitée, sinon des tokens par heure glissante)."""
    if (bad := admin_only(request)) is not None:
        return bad
    if (bad := json_only(request)) is not None:
        return bad
    body = parse_json(await request.body())
    if not isinstance(body, dict) or not ({"role", "share"} & body.keys()):
        return error_response("openai", 400, "invalid_request_error",
                              "corps attendu : {\"role\": …} et/ou "
                              "{\"share\": null | 0 | <tokens par heure>}")
    try:
        user = None
        if "role" in body:
            user = await asyncio.to_thread(accounts.set_role, uid, body["role"])
        if "share" in body:
            user = await asyncio.to_thread(accounts.set_share, uid,
                                           body["share"])
        return user
    except accounts.AccountError as exc:
        return account_error(exc)
