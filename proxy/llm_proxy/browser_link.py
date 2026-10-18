"""
Le LIEN avec l'extension de navigateur (chrome_extensions/llm-proxy-browser) :
ce qui permet à l'outil hébergé `browser` (tools/browser.py) d'agir dans le
navigateur de l'utilisateur — le vrai, avec ses sessions —, quel que soit
le client qui l'appelle : le chat de la page, Codex, pi, omp (par les
surfaces ou /mcp).

  WS   /v1/browser/link          l'extension s'y connecte et y reste
  GET  /ui/browser/link          ce que la page sait du lien de son compte
  GET  /ui/browser/extension.zip l'extension, à charger dans Chrome

L'extension ouvre une WebSocket et se présente, en premier message, avec
une clé du proxy : {"type": "hello", "key": …, "version": …, "agent": …}.
La clé est celle d'un client comme un autre (auth.key_client) ; le lien
est rangé sous son CLIENT (celui de son compte avec [oidc]) : les appels
d'outil de ce client, et de lui seul, y passent. Une seconde connexion du
même client remplace la première (un navigateur relancé, un second poste).
Un navigateur ne sait pas poser d'en-tête sur une WebSocket : la clé
voyage donc dans le premier message, jamais dans l'URL.

  proxy → extension  {"type": "welcome", "protocol": 1}
                     {"type": "call", "id": …, "op": …, "args": {…}}
                     {"type": "pong"}
  extension → proxy  {"type": "result", "id": …, "ok": true, "text": …,
                      "url"?, "title"?, "image"? (data URL)}
                     {"type": "result", "id": …, "ok": false, "error": …,
                      "code"? (tools.ERRORS)}
                     {"type": "ping"} toutes les 20 s : le service worker
                     d'une extension MV3 reste éveillé tant que sa
                     WebSocket parle

Le lien ne garde rien : il relaie. Ce que l'extension accepte de faire
(les sites autorisés, sa fenêtre à elle, le bouton Arrêter) se décide
chez elle, devant l'utilisateur.
"""

import asyncio
import io
import json
import secrets
import time
import zipfile
from pathlib import Path

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from . import auth
from .settings import log
from .httputil import error_response

router = APIRouter()

PATH = "/v1/browser/link"
PROTOCOL = 1
# Délai (s) du message de présentation, après l'ouverture.
HELLO_TIMEOUT = 10
# Codes de fermeture (plage privée des applications, 4000–4999).
CLOSE_BAD_HELLO, CLOSE_DENIED, CLOSE_REPLACED = 4400, 4401, 4409


class LinkClosed(Exception):
    """Le lien s'est fermé pendant un appel."""


class Link:
    """Une extension connectée : sa WebSocket, ce qu'elle a dit d'elle,
    et les appels en attente de leur résultat."""

    def __init__(self, ws: WebSocket, client: str, info: dict):
        self.ws, self.client, self.info = ws, client, info
        self.since = time.time()
        self.pending: dict[str, asyncio.Future] = {}

    async def call(self, op: str, args: dict, timeout: float) -> dict:
        """Un appel à l'extension → son message `result`. Lève
        LinkClosed (lien perdu) ou asyncio.TimeoutError."""
        cid = secrets.token_hex(8)
        fut = asyncio.get_running_loop().create_future()
        self.pending[cid] = fut
        try:
            await self.ws.send_text(json.dumps(
                {"type": "call", "id": cid, "op": op, "args": args},
                ensure_ascii=False))
            return await asyncio.wait_for(fut, timeout)
        except (WebSocketDisconnect, RuntimeError) as exc:
            raise LinkClosed(str(exc)) from exc
        finally:
            self.pending.pop(cid, None)

    def settle(self, msg: dict) -> None:
        fut = self.pending.get(str(msg.get("id", "")))
        if fut is not None and not fut.done():
            fut.set_result(msg)

    def drop(self, reason: str) -> None:
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(LinkClosed(reason))


# Les liens ouverts, par client (auth : condensé de la clé, ou du compte).
LINKS: dict[str, Link] = {}


def link_for(client: str) -> Link | None:
    return LINKS.get(client)


def _info(hello: dict) -> dict:
    """Ce que l'extension dit d'elle, borné (affiché dans la page)."""
    return {k: str(hello.get(k) or "")[:200] for k in ("version", "agent")}


@router.websocket(PATH)
async def link(ws: WebSocket):
    await ws.accept()
    try:
        hello = json.loads(await asyncio.wait_for(ws.receive_text(), HELLO_TIMEOUT))
    except (asyncio.TimeoutError, json.JSONDecodeError, WebSocketDisconnect, RuntimeError):
        await _close(ws, CLOSE_BAD_HELLO)
        return
    if not isinstance(hello, dict) or hello.get("type") != "hello":
        await _close(ws, CLOSE_BAD_HELLO)
        return
    client = ""
    if auth.auth_required():
        who = await auth.key_client(str(hello.get("key") or ""))
        if who is None:
            log.warning("extension de navigateur : clé refusée")
            await ws.send_text(json.dumps({"type": "denied",
                                           "error": "clé du proxy invalide"}))
            await _close(ws, CLOSE_DENIED)
            return
        client = who.client
    old = LINKS.get(client)
    me = Link(ws, client, _info(hello))
    LINKS[client] = me
    if old is not None:
        old.drop("remplacé par une autre connexion")
        await _close(old.ws, CLOSE_REPLACED)
    await ws.send_text(json.dumps({"type": "welcome", "protocol": PROTOCOL}))
    log.info("extension de navigateur connectée (%s)", me.info.get("agent", "")[:60])
    try:
        while True:
            try:
                msg = json.loads(await ws.receive_text())
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("type") == "ping":
                await ws.send_text('{"type": "pong"}')
            elif msg.get("type") == "result":
                me.settle(msg)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if LINKS.get(client) is me:
            del LINKS[client]
        me.drop("extension déconnectée")


async def _close(ws: WebSocket, code: int) -> None:
    try:
        await ws.close(code)
    except RuntimeError:
        pass        # déjà fermée


@router.get("/ui/browser/link")
async def link_state(request: Request):
    """Le lien du compte de la page : connecté ou non, et depuis quand."""
    me = LINKS.get(auth.request_owner(request))
    if me is None:
        return {"connected": False}
    return {"connected": True, "since": me.since, **me.info}


# ── l'extension, à télécharger ──────────────────────────────────────────
NAME = "llm-proxy-browser"


def _source() -> Path | None:
    """Le dossier de l'extension : à la racine du dépôt, ou de l'image."""
    here = Path(__file__).resolve()
    for root in (here.parents[2], here.parents[1]):
        folder = root / "chrome_extensions" / NAME
        if (folder / "manifest.json").is_file():
            return folder
    return None


@router.get("/ui/browser/extension.zip")
async def extension_zip():
    folder = _source()
    if folder is None:
        return error_response("openai", 404, "not_found",
                              "extension absente de ce serveur (chrome_extensions/)")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(folder.rglob("*")):
            if f.is_file() and not f.name.startswith("."):
                z.write(f, f"{NAME}/{f.relative_to(folder)}")
    return Response(buf.getvalue(), media_type="application/zip", headers={
        "content-disposition": f'attachment; filename="{NAME}.zip"'})
