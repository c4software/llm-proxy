"""La sortie des bacs vers les dépôts de paquets (executor/egress.py) : le
relais CONNECT de l'exécuteur, par sa prise Unix, SANS réseau — la
résolution et la connexion sortante sont remplacées, la « cible » est un
serveur d'écho local. Et ce que le bac reçoit de podman quand la sortie est
ouverte."""

import asyncio
import json
import os
import sys

import pytest

from executor import egress, sandbox
from executor.egress import Egress, allowed, parse_hosts, public

PUBLIC_IP = "151.101.0.223"          # une adresse publique (pypi.org)


def test_liste_des_hotes():
    hosts = parse_hosts("pypi.org, *.example.org")
    assert allowed("pypi.org", hosts) and allowed("PyPI.org.", hosts)
    assert allowed("a.example.org", hosts) and allowed("b.a.example.org", hosts)
    # `*.domaine` vaut ses sous-domaines, pas le domaine, ni un voisin.
    assert not allowed("example.org", hosts)
    assert not allowed("evilexample.org", hosts)
    assert not allowed("pypi.org.evil.com", hosts)
    assert parse_hosts("") == egress.DEFAULT_HOSTS
    assert public(PUBLIC_IP) and public("2606:4700::1111")
    for addr in ("127.0.0.1", "10.1.2.3", "192.168.1.1", "172.18.0.2",
                 "169.254.169.254", "100.64.0.1", "::1", "fd00::1",
                 "::ffff:10.0.0.1", "pas une adresse"):
        assert not public(addr), addr


async def _echo_server():
    async def echo(reader, writer):
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        writer.close()
    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _ask(path, head: bytes, payload: bytes = b""):
    reader, writer = await asyncio.open_unix_connection(path)
    writer.write(head)
    await writer.drain()
    status = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    echoed = b""
    if status.startswith(b"HTTP/1.1 200") and payload:
        writer.write(payload)
        await writer.drain()
        echoed = await asyncio.wait_for(reader.readexactly(len(payload)), 5)
    else:
        echoed = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    return status.decode().split(" ", 2)[1], echoed


def test_relais_n_ouvre_que_les_depots(tmp_path):
    async def scenario():
        server, port = await _echo_server()
        targets = []

        async def resolve(host):
            return {"pypi.org": [PUBLIC_IP],
                    "files.pythonhosted.org": ["10.0.0.7"]}.get(host, [PUBLIC_IP])

        async def connect(addr, port_):
            targets.append((addr, port_))
            return await asyncio.open_connection("127.0.0.1", port)

        relay = Egress(str(tmp_path / "sock"), resolve=resolve,
                       connect=connect, max_bytes=1000)
        path = await relay.open("sbx-1")
        assert oct(os.stat(path).st_mode)[-3:] == "666"
        out = {}
        # Un dépôt autorisé : le tunnel s'ouvre, les octets passent.
        out["ok"] = await _ask(path, b"CONNECT pypi.org:443 HTTP/1.1\r\n"
                               b"Host: pypi.org:443\r\n\r\n", b"bonjour")
        # Tout le reste est refusé, avec sa raison.
        out["hote"] = await _ask(path, b"CONNECT evil.com:443 HTTP/1.1\r\n\r\n")
        out["port"] = await _ask(path, b"CONNECT pypi.org:80 HTTP/1.1\r\n\r\n")
        out["clair"] = await _ask(path, b"GET http://pypi.org/ HTTP/1.1\r\n\r\n")
        out["interne"] = await _ask(
            path, b"CONNECT files.pythonhosted.org:443 HTTP/1.1\r\n\r\n")
        # Le volume du bac épuisé : coupé.
        try:
            out["volume"] = await _ask(
                path, b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\n", b"x" * 2000)
        except asyncio.IncompleteReadError as exc:
            out["volume"] = ("coupé", exc.partial)
        stats = await relay.close("sbx-1")
        server.close()
        return out, stats, targets, os.path.exists(path)

    out, stats, targets, exists = asyncio.run(scenario())
    assert out["ok"] == ("200", b"bonjour")
    assert out["hote"][0] == "403" and b"evil.com" in out["hote"][1]
    assert out["port"][0] == "403" and out["clair"][0] == "405"
    assert out["interne"][0] == "403" and b"non publique" in out["interne"][1]
    assert out["volume"] == ("coupé", b"")
    assert targets[0] == (PUBLIC_IP, 443)
    assert stats["refused"] == 4 and stats["hosts"] == {"pypi.org"}
    assert stats["bytes"] > 1000 and not exists


def test_un_bac_avec_sortie_recoit_la_prise_et_le_proxy(tmp_path, monkeypatch):
    """Ce que podman reçoit pour un bac à sortie ouverte : toujours
    `--network none`, la prise montée, HTTPS_PROXY vers le relais local,
    et le relais lancé avant `sleep infinity`."""
    fake = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fake_podman.py")
    monkeypatch.setenv("FAKE_PODMAN_ROOT", str(tmp_path / "pods"))
    os.makedirs(tmp_path / "pods")
    relay = Egress(str(tmp_path / "sock"))
    box = sandbox.Sandboxes(rootfs="/opt/sandbox/rootfs",
                            limits=sandbox.Limits(timeout=10, egress=True),
                            podman=(sys.executable, fake), egress=relay)

    async def scenario():
        out = await box.execute("c", "s", "python", "print(1)")
        (name,) = os.listdir(tmp_path / "pods")
        args = json.load(open(tmp_path / "pods" / name / ".args"))
        opened = os.path.exists(relay.path(name))
        await box.close()
        return out, args, opened, os.path.exists(relay.path(name))

    out, args, opened, left = asyncio.run(scenario())
    line = " ".join(args)
    assert out.exit_code == 0 and opened and not left
    for wanted in ("--network none", "--env HTTPS_PROXY=http://127.0.0.1:3128",
                   "--env PIP_USER=1", "--env GOPROXY=https://proxy.golang.org",
                   "--tmpfs /opt/devbox:rw,exec,size=2g,mode=1777",
                   "--env MISE_DATA_DIR=/opt/devbox/mise",
                   "--env PIP_BREAK_SYSTEM_PACKAGES=1",
                   f"--volume {relay.path(args[args.index('--name') + 1])}:/run/egress.sock"):
        assert wanted in line, wanted
    assert "GOPROXY=off" not in line and "CARGO_NET_OFFLINE" not in line
    assert args[-3:-1] == ["sh", "-c"] and sandbox.FORWARD in args[-1] \
        and args[-1].endswith("exec sleep infinity")


def test_reglage_booleen_de_l_environnement():
    assert sandbox.Limits.from_env({"SANDBOX_EGRESS": "true"}).egress is True
    assert sandbox.Limits.from_env({"SANDBOX_EGRESS": "0"}).egress is False
    assert sandbox.Limits.from_env({}).egress is False
    with pytest.raises(SystemExit):
        sandbox.Limits.from_env({"SANDBOX_EGRESS": "peut-être"})
