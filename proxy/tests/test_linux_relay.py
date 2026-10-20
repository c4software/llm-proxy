"""Le relais réseau de la VM Linux (ui/linux_relay.py) : le jeton, le
droit par compte, et le protocole Wisp contre un vrai serveur TCP local."""

import socketserver
import struct
import threading

import pytest
from starlette.websockets import WebSocketDisconnect

from llm_proxy.ui import linux_relay as relay


def frame(kind, sid, payload=b""):
    return struct.pack("<BI", kind, sid) + payload


def connect(sid, port, host="127.0.0.1"):
    return frame(relay.CONNECT, sid, bytes([relay.TCP]) + struct.pack("<H", port) + host.encode())


def parse(data):
    kind, sid = struct.unpack_from("<BI", data)
    return kind, sid, data[5:]


@pytest.fixture
def opened(proxy, monkeypatch):
    monkeypatch.setattr(relay, "ACCOUNTS", ["*"])
    monkeypatch.setattr(relay, "UI_CHAT", True)
    return proxy


@pytest.fixture
def echo():
    class Echo(socketserver.BaseRequestHandler):
        def handle(self):
            while data := self.request.recv(4096):
                self.request.sendall(data)

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Echo)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def relay_path(client):
    r = client.post("/ui/linux/relay")
    assert r.status_code == 200
    return r.json()["path"]


def test_no_relay_unless_configured(proxy):
    r = proxy.client.post("/ui/linux/relay")
    assert r.status_code == 403
    with pytest.raises(WebSocketDisconnect):
        with proxy.client.websocket_connect("/ui/linux/wisp/" + relay.make_token(None, "")):
            pass


def test_a_forged_or_expired_token_is_refused(opened):
    good = relay.make_token(None, "")
    for bad in (good[:-1] + ("0" if good[-1] != "0" else "1"),
                relay.make_token(None, "", now=0), "x.y"):
        with pytest.raises(WebSocketDisconnect):
            with opened.client.websocket_connect("/ui/linux/wisp/" + bad):
                pass


def test_tcp_round_trip(opened, echo, monkeypatch):
    monkeypatch.setattr(relay, "is_public", lambda ip: True)
    with opened.client.websocket_connect(relay_path(opened.client)) as ws:
        assert parse(ws.receive_bytes()) == (relay.CONTINUE, 0, struct.pack("<I", relay.BUFFER))
        ws.send_bytes(connect(1, echo))
        ws.send_bytes(frame(relay.DATA, 1, b"bonjour"))
        got = b""
        while len(got) < 7:
            kind, sid, body = parse(ws.receive_bytes())
            assert (kind, sid) == (relay.DATA, 1)
            got += body
        assert got == b"bonjour"
        ws.send_bytes(frame(relay.CLOSE, 1, bytes([relay.VOLUNTARY])))


def test_credit_is_renewed(opened, echo, monkeypatch):
    monkeypatch.setattr(relay, "is_public", lambda ip: True)
    with opened.client.websocket_connect(relay_path(opened.client)) as ws:
        ws.receive_bytes()
        ws.send_bytes(connect(1, echo))
        for _ in range(relay.BUFFER // 2):
            ws.send_bytes(frame(relay.DATA, 1, b"x"))
        kinds = set()
        while relay.CONTINUE not in kinds:
            kind, sid, body = parse(ws.receive_bytes())
            assert sid == 1
            kinds.add(kind)
        assert body == struct.pack("<I", relay.BUFFER)


@pytest.mark.parametrize("host,port,reason", [
    ("127.0.0.1", 80, relay.BLOCKED),          # loopback
    ("192.168.1.1", 80, relay.BLOCKED),        # réseau local
    ("100.64.0.4", 443, relay.BLOCKED),        # Tailscale
    ("1.1.1.1", 25, relay.BLOCKED),            # port refusé
    ("1.1.1.1", 0, relay.INVALID),
])
def test_private_targets_and_blocked_ports_are_refused(opened, host, port, reason):
    with opened.client.websocket_connect(relay_path(opened.client)) as ws:
        ws.receive_bytes()
        ws.send_bytes(connect(1, port, host))
        assert parse(ws.receive_bytes()) == (relay.CLOSE, 1, bytes([reason]))


def test_udp_is_refused(opened):
    with opened.client.websocket_connect(relay_path(opened.client)) as ws:
        ws.receive_bytes()
        ws.send_bytes(frame(relay.CONNECT, 1, bytes([2]) + struct.pack("<H", 53) + b"1.1.1.1"))
        assert parse(ws.receive_bytes()) == (relay.CLOSE, 1, bytes([relay.INVALID]))


def test_accounts_are_checked_by_address(monkeypatch):
    monkeypatch.setattr(relay, "UI_CHAT", True)
    monkeypatch.setattr(relay, "ACCOUNTS", ["moi@exemple.fr"])
    me = {"email": "Moi@Exemple.fr", "role": "user"}
    assert relay.allowed(me)
    assert not relay.allowed({**me, "email": "autre@exemple.fr"})
    assert not relay.allowed({**me, "role": "blocked"})
    assert not relay.allowed(None)
