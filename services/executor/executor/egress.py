"""
La SORTIE d'un bac vers les dépôts de paquets — et vers rien d'autre.

Un bac n'a TOUJOURS pas de réseau (`--network none`) : pas d'interface, pas
de DNS, rien à joindre. Quand SANDBOX_EGRESS est vrai, l'exécuteur lui
monte UNE prise Unix (/run/egress.sock), servie ici : un relais HTTP
`CONNECT` qui n'ouvre que des tunnels TLS (port 443) vers les hôtes de la
liste — PyPI, npm, crates.io, les modules Go par défaut. Dans le bac, un
petit relais (sandbox/forward.py, lancé à la création du bac) écoute sur
127.0.0.1:3128 et passe chaque connexion à la prise : pip, npm, cargo et
go y vont par HTTPS_PROXY.

Ce qui est jugé ICI, hors du bac, donc hors de portée du programme :
  * la requête : `CONNECT hôte:443` seulement — ni GET en clair, ni autre
    port ; l'hôte doit être dans la liste (nom exact, ou `*.domaine`) ;
  * l'adresse : le nom est résolu ici, et TOUTES ses adresses doivent être
    publiques — une entrée DNS qui pointerait vers le réseau interne ne
    l'ouvre pas ;
  * les bornes : octets par bac (dans les deux sens), connexions
    simultanées par bac, délai d'inactivité d'un tunnel.

Le contenu reste chiffré de bout en bout : le relais ne voit que l'hôte.
Ce qu'un tel accès permet encore, et que la liste ne peut pas empêcher :
un programme peut DÉPOSER des données sur un dépôt s'il a un compte (une
publication pip, npm, cargo exige un jeton qu'il n'a pas, mais qu'il
pourrait recevoir dans le prompt) — ne pas ouvrir cette sortie à un modèle
à qui l'on confie des secrets.

Ce module ne connaît ni podman ni HTTP au-delà de CONNECT.
"""

import asyncio
import ipaddress
import logging
import os
import socket

log = logging.getLogger("executor")

# Les dépôts de paquets des langages du bac : Python (pip), Node.js (npm),
# Rust (cargo, index « sparse »), Go (proxy de modules et base de sommes),
# PHP (composer).
PACKAGE_HOSTS = (
    "pypi.org", "files.pythonhosted.org",
    "registry.npmjs.org",
    "index.crates.io", "static.crates.io", "crates.io",
    "proxy.golang.org", "sum.golang.org",
    "repo.packagist.org", "packagist.org",
)
# Les sources des outils que `devbox dev-env` installe par mise : ses
# listes de versions, les publications GitHub (Python, Bun, Zig, Scala…),
# et les sites des langages (Node.js, Rust, Go, Java, Deno, .NET, Ruby,
# Clojure, Maven). GitHub y est : on y héberge n'importe quoi — c'est le
# prix de ces installations, la liste reste réglable (SANDBOX_EGRESS_HOSTS).
DEVBOX_HOSTS = (
    "mise-versions.jdx.dev", "mise.jdx.dev", "mise-java.jdx.dev",
    "github.com", "api.github.com", "codeload.github.com",
    "objects.githubusercontent.com", "release-assets.githubusercontent.com",
    "raw.githubusercontent.com",
    "nodejs.org", "static.rust-lang.org", "dl.google.com", "go.dev",
    "api.adoptium.net", "download.java.net",
    "dl.deno.land", "ziglang.org",
    "builds.dotnet.microsoft.com", "dotnetcli.azureedge.net",
    "cache.ruby-lang.org", "rubygems.org", "index.rubygems.org",
    "download.clojure.org", "repo1.maven.org", "repo.clojars.org",
)
DEFAULT_HOSTS = PACKAGE_HOSTS + DEVBOX_HOSTS
PORT = 443
HEAD_MAX = 8192                     # la requête CONNECT et ses en-têtes


def parse_hosts(raw: str) -> tuple[str, ...]:
    """SANDBOX_EGRESS_HOSTS : des noms séparés par des virgules ou des
    espaces ; `*.domaine` vaut ses sous-domaines (pas le domaine lui-même).
    Vide : la liste par défaut."""
    hosts = tuple(h.strip().lower().rstrip(".")
                  for h in raw.replace(",", " ").split() if h.strip())
    return hosts or DEFAULT_HOSTS


def allowed(host: str, hosts) -> bool:
    host = host.lower().rstrip(".")
    for entry in hosts:
        if entry.startswith("*."):
            if host.endswith(entry[1:]) and host != entry[2:]:
                return True
        elif host == entry:
            return True
    return False


def public(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global


class Refused(Exception):
    """La demande n'est pas servie : (statut HTTP, raison lisible)."""

    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


class Egress:
    """Les relais des bacs : une prise Unix par bac, ouverte à sa création
    (open), fermée à sa destruction (close). `resolve` (les adresses d'un
    nom) et `connect` (la connexion à une adresse) sont remplaçables : les
    tests n'ont pas de réseau."""

    def __init__(self, directory: str, hosts=DEFAULT_HOSTS,
                 max_bytes: int = 500_000_000, max_tunnels: int = 16,
                 idle: float = 60.0, connect_timeout: float = 15.0,
                 resolve=None, connect=None):
        self.directory, self.hosts = directory, tuple(hosts)
        self.max_bytes, self.max_tunnels = max_bytes, max_tunnels
        self.idle, self.connect_timeout = idle, connect_timeout
        self.resolve = resolve or self._resolve
        self.connect = connect or asyncio.open_connection
        # nom du bac → (serveur, compteurs)
        self._servers: dict[str, tuple[asyncio.base_events.Server, dict]] = {}

    def path(self, name: str) -> str:
        return os.path.join(self.directory, name + ".sock")

    async def open(self, name: str) -> str:
        """La prise du bac `name`, prête à être montée dans le bac."""
        os.makedirs(self.directory, mode=0o755, exist_ok=True)
        path = self.path(name)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        stats = {"bytes": 0, "tunnels": 0, "refused": 0, "open": 0,
                 "hosts": set()}
        server = await asyncio.start_unix_server(
            lambda r, w: self._serve(name, stats, r, w), path=path)
        # Le bac tourne sous un autre uid (son espace d'utilisateurs) : il
        # doit pouvoir s'y connecter. Rien d'autre n'atteint ce dossier.
        os.chmod(path, 0o666)
        self._servers[name] = (server, stats)
        return path

    async def close(self, name: str) -> dict | None:
        entry = self._servers.pop(name, None)
        if entry is None:
            return None
        server, stats = entry
        server.close()
        try:
            os.unlink(self.path(name))
        except FileNotFoundError:
            pass
        if stats["tunnels"] or stats["refused"]:
            log.info("sortie du bac %s : %d tunnel(s), %d refus, %d octets, "
                     "hôtes %s", name, stats["tunnels"], stats["refused"],
                     stats["bytes"], ", ".join(sorted(stats["hosts"])) or "—")
        return stats

    # ── une connexion du bac ────────────────────────────────────────────
    async def _resolve(self, host: str) -> list[str]:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, PORT, type=socket.SOCK_STREAM)
        return list(dict.fromkeys(info[4][0] for info in infos))

    async def target(self, head: bytes) -> tuple[str, list[str]]:
        """Juge la requête : (hôte, adresses publiques) ou Refused."""
        try:
            line = head.split(b"\r\n", 1)[0].decode("ascii")
            method, authority, _version = line.split(" ")
        except (UnicodeDecodeError, ValueError):
            raise Refused(400, "requête illisible")
        if method != "CONNECT":
            raise Refused(405, "seul CONNECT (HTTPS) est servi : la sortie "
                               "du bac ne sert qu'aux dépôts de paquets")
        host, _, port = authority.rpartition(":")
        host = host.strip("[]").lower()
        if port != str(PORT) or not host:
            raise Refused(403, f"port {port or '?'} refusé : HTTPS (443) "
                               f"seulement")
        if not allowed(host, self.hosts):
            raise Refused(403, f"{host} n'est pas un dépôt de paquets "
                               f"autorisé ({', '.join(self.hosts)})")
        try:
            addrs = await asyncio.wait_for(self.resolve(host),
                                           self.connect_timeout)
        except (OSError, asyncio.TimeoutError):
            raise Refused(502, f"{host} : nom introuvable")
        if not addrs or not all(public(a) for a in addrs):
            raise Refused(403, f"{host} résout vers une adresse non publique")
        return host, addrs

    async def _serve(self, name: str, stats: dict, reader, writer) -> None:
        upstream = None
        try:
            try:
                head = await asyncio.wait_for(
                    reader.readuntil(b"\r\n\r\n"), self.idle)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError,
                    asyncio.TimeoutError):
                return
            if len(head) > HEAD_MAX:
                raise Refused(431, "en-têtes trop longs")
            if stats["open"] >= self.max_tunnels:
                raise Refused(429, "trop de connexions simultanées")
            if stats["bytes"] >= self.max_bytes:
                raise Refused(429, "volume de téléchargement du bac épuisé")
            host, addrs = await self.target(head)
            error = None
            for addr in addrs:
                try:
                    upstream = await asyncio.wait_for(
                        self.connect(addr, PORT),
                        self.connect_timeout)
                    break
                except (OSError, asyncio.TimeoutError) as exc:
                    error = exc
            if upstream is None:
                raise Refused(502, f"{host} injoignable ({error})")
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            stats["tunnels"] += 1
            stats["hosts"].add(host)
            stats["open"] += 1
            try:
                await self._splice(stats, reader, writer, *upstream)
            finally:
                stats["open"] -= 1
        except Refused as exc:
            stats["refused"] += 1
            log.info("sortie du bac %s refusée : %s", name, exc.reason)
            body = (exc.reason + "\n").encode()
            try:
                writer.write(f"HTTP/1.1 {exc.status} Refused\r\n"
                             f"Content-Type: text/plain; charset=utf-8\r\n"
                             f"Content-Length: {len(body)}\r\n"
                             f"Connection: close\r\n\r\n".encode() + body)
                await writer.drain()
            except (ConnectionError, OSError):
                pass
        finally:
            for w in (writer, upstream[1] if upstream else None):
                if w is not None:
                    w.close()

    async def _splice(self, stats, creader, cwriter, ureader, uwriter) -> None:
        """Les deux sens, jusqu'à la fin de l'un, l'inactivité, ou le
        volume du bac épuisé."""

        async def pump(src, dst):
            while True:
                chunk = await asyncio.wait_for(src.read(65536), self.idle)
                if not chunk:
                    break
                stats["bytes"] += len(chunk)
                if stats["bytes"] > self.max_bytes:
                    break
                dst.write(chunk)
                await dst.drain()

        tasks = [asyncio.create_task(pump(creader, uwriter)),
                 asyncio.create_task(pump(ureader, cwriter))]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
