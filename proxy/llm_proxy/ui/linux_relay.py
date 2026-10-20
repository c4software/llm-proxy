"""
Le RELAIS RÉSEAU de la VM Linux de la page (docs/linux.md) : un serveur
Wisp v1, le protocole du backend `wisp://` de v86 — de vraies connexions
TCP (https, apk, pip, git, ssh), multiplexées sur un WebSocket. Sans lui,
la VM n'a que le backend `fetch` de v86 : des requêtes http vers les seuls
sites qui permettent CORS.

  POST /ui/linux/relay        la page, avec sa session : un jeton signé,
                              le chemin du WebSocket ; 403 si le compte
                              n'y a pas droit (la page garde alors `fetch`)
  WS   /ui/linux/wisp/<jeton> la VM (cadre sans origine, donc sans cookie :
                              le jeton vaut droit d'accès)

Qui : [ui].linux_relay — « * » (tous les comptes actifs ; sans comptes
OIDC, toute session de la page) ou des adresses de comptes. Vide : pas de
relais. Le droit est relu à chaque connexion : retirer un compte de la
liste (au redémarrage) ou le bloquer coupe ses relais suivants.

Où : les seules adresses PUBLIQUES (net.is_public : ni réseau local, ni
Tailscale, ni loopback, ni les conteneurs voisins), quel que soit
[tools.net].allow_private — ici, c'est du code écrit par le modèle qui
choisit la cible. Les noms sont résolus par la VM (DoH, dans le
navigateur) : le relais reçoit surtout des adresses, et c'est l'adresse
qu'il juge, pas un domaine. Ports refusés : [ui].linux_relay_blocked_ports
(25 par défaut : pas de courrier sortant depuis l'adresse du proxy).

Le protocole (v1, sans l'extension v2) : chaque message binaire est
[type u8][flux u32 LE][charge]. CONNECT (1) : type de flux u8 (1 = TCP ;
UDP refusé), port u16 LE, nom ; DATA (2) : des octets ; CONTINUE (3) :
u32, combien de DATA le client peut encore envoyer sur ce flux ; CLOSE
(4) : la raison, u8. Le serveur ouvre par un CONTINUE sur le flux 0 (le
crédit de départ de chaque flux), puis en renvoie un sur un flux quand la
moitié de son crédit est écrite vers la cible. Jamais de CONTINUE sur un
flux que le client a fermé : v86 lève une exception sur un flux inconnu.
"""

import asyncio
import hashlib
import hmac
import socket
import struct
import time

from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import JSONResponse

from .. import tools
from ..access import accounts
from ..access.auth import request_owner, request_user
from ..core import config
from ..core.httputil import error_response
from ..core.settings import log
from ..tools.net import is_public

router = APIRouter()

UI_CHAT = config.flag("ui.chat", False)
ACCOUNTS = [a.strip().lower() for a in config.strings("ui.linux_relay") if a.strip()]
BLOCKED_PORTS = frozenset(int(p) for p in config.get("ui.linux_relay_blocked_ports", [25]))
TOKEN_TTL = 24 * 3600            # s ; v86 se reconnecte avec la même adresse
BUFFER = 128                     # DATA d'avance accordés par flux
MAX_STREAMS = 64                 # flux ouverts par WebSocket
MAX_RELAYS = 4                   # WebSockets ouverts par client
CONNECT_TIMEOUT = 15.0           # s, résolution et connexion
READ_CHUNK = 16384

CONNECT, DATA, CONTINUE, CLOSE = 1, 2, 3, 4
TCP = 1
# Raisons de CLOSE (spécification Wisp).
VOLUNTARY, NETWORK_ERROR = 0x02, 0x03
INVALID, UNREACHABLE, TIMEOUT, REFUSED = 0x41, 0x42, 0x43, 0x44
BLOCKED, THROTTLED = 0x48, 0x49

_open: dict[str, int] = {}       # client → WebSockets ouverts


def _key() -> bytes:
    return hmac.new(tools.owner_secret(), b"llm-proxy linux relay",
                    hashlib.sha256).digest()


def _sign(body: str) -> str:
    return hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()[:32]


def make_token(uid: int | None, owner: str, now: float | None = None) -> str:
    """<compte|->.<expiration>.<client>.<signature>"""
    exp = int((time.time() if now is None else now) + TOKEN_TTL)
    body = f"{uid if uid is not None else '-'}.{exp}.{owner or '-'}"
    return f"{body}.{_sign(body)}"


def read_token(token: str) -> tuple[int | None, str] | None:
    """(compte ou None, client) d'un jeton valide et non expiré."""
    parts = token.split(".")
    if len(parts) != 4:
        return None
    uid, exp, owner, sig = parts
    if not hmac.compare_digest(sig.encode(), _sign(".".join(parts[:3])).encode()):
        return None
    if not exp.isdigit() or int(exp) < time.time():
        return None
    return (None if uid == "-" else int(uid)), ("" if owner == "-" else owner)


def allowed(user: dict | None) -> bool:
    """Ce compte (None : session sans compte OIDC) a-t-il le relais ?"""
    if not UI_CHAT or not ACCOUNTS:
        return False
    if user is not None and user["role"] not in accounts.ACTIVE:
        return False
    return "*" in ACCOUNTS or (user is not None
                               and (user["email"] or "").lower() in ACCOUNTS)


@router.post("/ui/linux/relay")
async def relay_token(request: Request):
    user = request_user(request)
    if not allowed(user):
        return error_response("openai", 403, "not_allowed",
                              "relais réseau de la VM Linux non ouvert à ce compte "
                              "([ui].linux_relay)")
    token = make_token(user["id"] if user else None, request_owner(request))
    return JSONResponse({"path": f"/ui/linux/wisp/{token}"},
                        headers={"Cache-Control": "no-store"})


def frame(kind: int, sid: int, payload: bytes = b"") -> bytes:
    return struct.pack("<BI", kind, sid) + payload


async def target(host: str, port: int) -> tuple[str | None, int]:
    """(adresse vérifiée, 0) ou (None, raison de CLOSE)."""
    if not 0 < port < 65536 or not host:
        return None, INVALID
    if port in BLOCKED_PORTS:
        return None, BLOCKED
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, ValueError):
        return None, UNREACHABLE
    ips = [info[4][0] for info in infos]
    if not ips:
        return None, UNREACHABLE
    if not all(is_public(ip) for ip in ips):
        return None, BLOCKED
    return ips[0], 0


class Stream:
    """Un flux : la connexion TCP, et ce que le client y envoie (une file :
    des DATA peuvent arriver avant que la connexion soit faite)."""

    def __init__(self):
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.task: asyncio.Task | None = None
        self.writer: asyncio.StreamWriter | None = None


class Relay:
    def __init__(self, ws: WebSocket, owner: str):
        self.ws = ws
        self.owner = owner
        self.streams: dict[int, Stream] = {}
        self.lock = asyncio.Lock()

    async def send(self, kind: int, sid: int, payload: bytes = b"") -> None:
        async with self.lock:
            await self.ws.send_bytes(frame(kind, sid, payload))

    async def close_stream(self, sid: int, reason: int) -> None:
        """Fermé par le serveur : dit au client, une fois."""
        if self.streams.pop(sid, None) is not None:
            try:
                await self.send(CLOSE, sid, bytes([reason]))
            except Exception:
                pass

    async def run(self) -> None:
        await self.send(CONTINUE, 0, struct.pack("<I", BUFFER))
        try:
            while True:
                msg = await self.ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                data = msg.get("bytes")
                if not data or len(data) < 5:
                    continue
                kind, sid = struct.unpack_from("<BI", data)
                body = data[5:]
                if kind == CONNECT:
                    await self.connect(sid, body)
                elif kind == DATA and sid in self.streams:
                    stream = self.streams[sid]
                    if stream.queue.qsize() > 2 * BUFFER:
                        await self.close_stream(sid, THROTTLED)
                        stream.task.cancel()
                    else:
                        stream.queue.put_nowait(body)
                elif kind == CLOSE and sid in self.streams:
                    self.streams.pop(sid).task.cancel()
        finally:
            for stream in self.streams.values():
                stream.task.cancel()
            self.streams.clear()

    async def connect(self, sid: int, body: bytes) -> None:
        if sid == 0 or sid in self.streams or len(body) < 3:
            return
        if body[0] != TCP:
            await self.send(CLOSE, sid, bytes([INVALID]))
            return
        if len(self.streams) >= MAX_STREAMS:
            await self.send(CLOSE, sid, bytes([THROTTLED]))
            return
        port = struct.unpack_from("<H", body, 1)[0]
        host = body[3:].decode("utf-8", "replace").strip()
        stream = self.streams[sid] = Stream()
        stream.task = asyncio.create_task(self.pump(sid, stream, host, port))

    async def pump(self, sid: int, stream: Stream, host: str, port: int) -> None:
        reader_task = None
        try:
            try:
                async with asyncio.timeout(CONNECT_TIMEOUT):
                    ip, reason = await target(host, port)
                    if ip is None:
                        log.info("relais Linux %s : %s:%d refusé (0x%02x)",
                                 self.owner[:8], host, port, reason)
                        await self.close_stream(sid, reason)
                        return
                    reader, stream.writer = await asyncio.open_connection(ip, port)
            except TimeoutError:
                await self.close_stream(sid, TIMEOUT)
                return
            except ConnectionRefusedError:
                await self.close_stream(sid, REFUSED)
                return
            except OSError:
                await self.close_stream(sid, UNREACHABLE)
                return
            log.info("relais Linux %s : %s:%d", self.owner[:8], host, port)
            reader_task = asyncio.create_task(self.download(sid, reader))
            written = 0
            while True:
                chunk = await stream.queue.get()
                stream.writer.write(chunk)
                await stream.writer.drain()
                written += 1
                if written >= BUFFER // 2 and sid in self.streams:
                    written = 0
                    await self.send(CONTINUE, sid, struct.pack("<I", BUFFER))
        except (ConnectionError, OSError):
            await self.close_stream(sid, NETWORK_ERROR)
        finally:
            if reader_task:
                reader_task.cancel()
            if stream.writer:
                stream.writer.close()

    async def download(self, sid: int, reader: asyncio.StreamReader) -> None:
        """La cible vers le client, jusqu'à sa fin."""
        reason = VOLUNTARY
        try:
            while chunk := await reader.read(READ_CHUNK):
                if sid not in self.streams:
                    return
                await self.send(DATA, sid, chunk)
        except (ConnectionError, OSError):
            reason = NETWORK_ERROR
        stream = self.streams.get(sid)
        await self.close_stream(sid, reason)
        if stream:
            stream.task.cancel()


@router.websocket("/ui/linux/wisp/{token}")
async def wisp(ws: WebSocket, token: str):
    found = read_token(token)
    user = None
    if found is not None and found[0] is not None:
        user = await asyncio.to_thread(accounts.user, found[0])
        if user is None:
            found = None
    if found is None or not allowed(user):
        await ws.close(code=1008)
        return
    owner = found[1]
    if _open.get(owner, 0) >= MAX_RELAYS:
        await ws.close(code=1008)
        return
    await ws.accept()
    _open[owner] = _open.get(owner, 0) + 1
    try:
        await Relay(ws, owner).run()
    finally:
        _open[owner] -= 1
        if not _open[owner]:
            del _open[owner]
