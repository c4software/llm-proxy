"""
Garde-fous réseau des outils hébergés. Un outil hébergé fait partir DU
PROXY une requête dont la cible est choisie par le modèle : sans filtre,
une page web (ou un prompt) pourrait lui faire lire un service du réseau
local — le backend d'inférence, un routeur, les métadonnées d'un cloud.

Règle : seules les adresses PUBLIQUES sont jointes. Le nom est résolu
ici, toutes ses adresses doivent être publiques, et la connexion part
vers l'adresse vérifiée (pas vers le nom, qu'un DNS pourrait faire
changer entre le contrôle et la connexion).

Le garde-fou est COMMUN à tout ce que le proxy télécharge à la demande du
modèle — une page (web_fetch), une image ou un PDF (ocr), un fichier audio
(transcribe) : une seule table, [tools.net], et un seul téléchargement,
download(). Ce qu'un déploiement interdit de lire, il l'interdit aussi
d'écouter : une liste par outil serait une liste oubliée. Les bornes de
taille et de durée, elles, restent à chaque outil.
"""

import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import httpx

from .. import config
from .contract import ToolError

# Ce qui a été lu à l'ancienne place, [tools.web_fetch] — où ces trois
# clés vivaient quand web_fetch était seul à télécharger —, et ce qui y
# est resté alors que [tools.net] le règle : dit au démarrage (app.py).
LEGACY: list[str] = []
SHADOWED: list[str] = []


def _setting(key: str, read):
    """[tools.net].<clé> ; à défaut celle de [tools.web_fetch], pour un
    déploiement d'avant la table commune."""
    new, old = f"tools.net.{key}", f"tools.web_fetch.{key}"
    if config.get(old) is not None:
        if config.get(new) is None:
            LEGACY.append(key)
            return read(old)
        SHADOWED.append(key)
    return read(new)


# Joindre aussi les adresses privées : à n'ouvrir que sur un proxy dont
# tous les clients sont de confiance, et jamais derrière un modèle qui lit
# le web.
ALLOW_PRIVATE = _setting("allow_private", config.flag)
# Listes de domaines, fixées par celui qui déploie (pas par le modèle) :
# `allowed_domains` non vide = SEULS ces domaines sont joints ;
# `blocked_domains` = jamais. Règles de domain_match (sous-domaines
# couverts, chemin facultatif). C'est la seule parade à la fuite par
# l'URL : une page lue qui pousse le modèle à ouvrir
# https://ailleurs/?d=<contenu de la conversation>.
ALLOWED_DOMAINS = _setting("allowed_domains", config.strings)
BLOCKED_DOMAINS = _setting("blocked_domains", config.strings)
MAX_REDIRECTS = 5


class Blocked(Exception):
    """Cible refusée ; le message est rendu tel quel au modèle. `code` :
    le code d'erreur du contrat (contract.ERRORS) que l'outil rendra."""

    def __init__(self, message: str, code: str = "not_allowed"):
        super().__init__(message)
        self.code = code


# Préfixes IPv6 qui ne font qu'EMBALLER une adresse IPv4 dans leurs 32
# derniers bits, et qu'`is_global` tient pour publics : NAT64 (derrière
# une passerelle NAT64, 64:ff9b::a00:1 EST 10.0.0.1) et les adresses
# « compatibles IPv4 » (::10.0.0.1, obsolètes). C'est l'IPv4 emballée
# qui est jugée. Les IPv4 « mappées » (::ffff:…) le sont à part ; 6to4
# (2002::/16) et Teredo (2001::/32) sont déjà refusés par `is_global`.
EMBEDDED_V4 = (ipaddress.ip_network("64:ff9b::/96"),
               ipaddress.ip_network("::/96"))


def is_public(ip: str) -> bool:
    """Adresse routable sur Internet : ni privée, ni locale, ni lien
    local, ni CGNAT (100.64.0.0/10, les adresses Tailscale), ni réservée."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        elif any(addr in n for n in EMBEDDED_V4):
            addr = ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
    return addr.is_global and not addr.is_multicast


async def public_target(url: str, allow_private: bool = False
                        ) -> tuple[str, str, int]:
    """(schéma, adresse vérifiée, port) pour une URL http(s). Lève Blocked
    si le schéma n'est pas http(s), si le nom ne se résout pas, ou si
    l'UNE de ses adresses n'est pas publique."""
    # urlsplit lève ValueError sur «http://[::1», et `.port` sur un port
    # hors bornes ou non numérique : c'est une URL refusée, pas une panne.
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise Blocked("URL invalide", "invalid_input")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise Blocked("seules les URL http(s) sont lues", "invalid_input")
    port = port or (443 if parts.scheme == "https" else 80)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            parts.hostname, port, type=socket.SOCK_STREAM)
    # ValueError : octet nul dans le nom (UnicodeError en est une).
    except (socket.gaierror, ValueError):
        raise Blocked(f"hôte introuvable : {parts.hostname}", "not_accessible")
    ips = [info[4][0] for info in infos]
    if not ips:
        raise Blocked(f"hôte introuvable : {parts.hostname}", "not_accessible")
    if not allow_private and not all(is_public(ip) for ip in ips):
        raise Blocked(
            f"{parts.hostname} désigne une adresse privée ou locale : refusé")
    return parts.scheme, ips[0], port


def domain_match(url: str, domains) -> bool:
    """`url` relève-t-elle d'une des entrées de `domains` ? Règles de
    l'outil serveur d'Anthropic, reprises pour toutes les listes de domaines : domaine nu,
    sans schéma ; les sous-domaines sont couverts (`example.com` couvre
    `docs.example.com`, l'inverse non) ; un chemin restreint à ce qui le
    prolonge (`example.com/blog`). Les jokers de chemin ne sont pas lus :
    le chemin s'arrête au premier `*`."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    for d in domains:
        d = str(d).strip().lower().split("://", 1)[-1]
        name, _, path = d.partition("/")
        if not name or not (host == name or host.endswith("." + name)):
            continue
        path = path.split("*", 1)[0]
        if not path or parts.path.lower().startswith("/" + path):
            return True
    return False


def check(url: str, settings=None) -> None:
    """Les listes de domaines : celles de [tools.net], et celles que le
    CLIENT a posées sur son outil (`settings` : Call.settings, clés
    `allowed_domains` / `blocked_domains`) — qui s'AJOUTENT aux premières,
    elles n'en lèvent rien. Lève ToolError `not_allowed`."""
    settings = settings or {}
    if any(not domain_match(url, allowed)
           for allowed in (ALLOWED_DOMAINS, settings.get("allowed_domains"))
           if allowed) \
            or domain_match(url, BLOCKED_DOMAINS) \
            or domain_match(url, settings.get("blocked_domains") or ()):
        raise ToolError("not_allowed", (
            f"{urlsplit(url).hostname or url} is not a "
            f"domain this proxy is allowed to read."))


async def _hop(url: str, timeout: float, limit, user_agent: str, accept: str,
               transport) -> tuple[httpx.Response, bytes]:
    """Un saut : résolution contrôlée, connexion vers l'adresse vérifiée
    (Host et SNI portent le nom), corps lu jusqu'à `limit` — voir
    download()."""
    scheme, ip, port = await public_target(url, ALLOW_PRIVATE)
    parts = urlsplit(url)
    host = parts.hostname or ""
    # Le nom tel qu'il part sur le fil : en ASCII (un nom accentué passe
    # en punycode, comme getaddrinfo l'a résolu — un en-tête non ASCII
    # ferait lever httpx), une IPv6 littérale entre crochets dans Host.
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise Blocked(f"hôte introuvable : {host}", "not_accessible")
    host_header = f"[{host}]" if ":" in host else host
    literal = f"[{ip}]" if ":" in ip else ip
    target = f"{scheme}://{literal}:{port}{parts.path or '/'}"
    if parts.query:
        target += "?" + parts.query
    default_port = 443 if scheme == "https" else 80
    headers = {
        "Host": host_header if port == default_port
                else f"{host_header}:{port}",
        "User-Agent": user_agent,
        "Accept": accept,
        # Pas de compression : la borne se compte après décompression,
        # bloc par bloc — un seul bloc gzip peut en rendre mille fois plus.
        "Accept-Encoding": "identity",
    }
    # trust_env=False : sans lui httpx lirait HTTP_PROXY / ALL_PROXY, et
    # « la connexion part vers l'adresse vérifiée » deviendrait « un proxy
    # s'y connecte pour nous ».
    async with httpx.AsyncClient(timeout=timeout, transport=transport,
                                 follow_redirects=False,
                                 trust_env=False) as c:
        req = c.build_request("GET", target, headers=headers,
                              extensions={"sni_hostname": host})
        r = await c.send(req, stream=True)
        body = bytearray()
        try:
            # Avant le premier octet : ce que les en-têtes suffisent à
            # refuser (un statut, une taille annoncée) n'est pas lu.
            bound = limit(r, b"")
            if bound > 0:
                async for chunk in r.aiter_bytes():
                    body += chunk
                    # Relue à chaque bloc : les premiers octets peuvent
                    # changer la borne (un PDF), ou arrêter la lecture.
                    bound = limit(r, bytes(body))
                    if len(body) >= bound:
                        break
        finally:
            await r.aclose()
    return r, bytes(body[:max(bound, 0)])


async def download(url: str, settings=None, *, timeout: float, limit,
                   user_agent: str, accept: str = "*/*",
                   transport=None) -> tuple[str, httpx.Response, bytes]:
    """LE téléchargement d'une cible choisie par le modèle : (URL
    réellement lue, réponse, corps). À chaque saut de redirection
    (MAX_REDIRECTS au plus) : les listes de domaines (check), puis
    l'adresse (public_target).

    `settings` : Call.settings — les listes du client. `timeout` : celui
    de CHAQUE requête. `limit` : la borne de taille, propre à l'outil —
    un nombre d'octets, ou une fonction (réponse, octets déjà lus) →
    octets à lire au plus, appelée avant la lecture puis à chaque bloc ;
    la lecture s'arrête dès que la borne est atteinte, le corps rendu est
    coupé à elle. La fonction peut lever ToolError (un fichier annoncé
    trop gros), rendre 0 (rien à lire de cette réponse) ou la taille déjà
    lue (assez vu). À l'outil de dire ensuite ce qu'un corps coupé veut
    dire pour lui.

    Lève ToolError, et elle seule : `not_allowed` (domaine, adresse
    privée), `invalid_input` (URL), `not_accessible` (hôte inconnu,
    erreur de transport, trop de redirections, HTTP ≥ 400),
    `too_many_requests` (HTTP 429)."""
    bound = limit if callable(limit) else lambda r, body: limit
    try:
        for _ in range(MAX_REDIRECTS + 1):
            check(url, settings)
            r, body = await _hop(url, timeout, bound, user_agent, accept,
                                 transport)
            if r.status_code in (301, 302, 303, 307, 308) \
                    and r.headers.get("location"):
                url = urljoin(url, r.headers["location"])
                continue
            break
        else:
            raise ToolError("not_accessible", "too many redirects.")
    except Blocked as exc:
        raise ToolError(exc.code, f"{exc}.")
    # InvalidURL n'est PAS une HTTPError : caractère de contrôle dans le
    # chemin, URL trop longue — y compris dans un Location de redirection.
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise ToolError("not_accessible", f"could not fetch {url} "
                                          f"({type(exc).__name__}).")
    if r.status_code >= 400:
        raise ToolError(
            "too_many_requests" if r.status_code == 429 else "not_accessible",
            f"{url} returned HTTP {r.status_code}.")
    return url, r, body
