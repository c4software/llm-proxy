"""
Garde-fous réseau des outils hébergés. Un outil hébergé fait partir DU
PROXY une requête dont la cible est choisie par le modèle : sans filtre,
une page web (ou un prompt) pourrait lui faire lire un service du réseau
local — le backend d'inférence, un routeur, les métadonnées d'un cloud.

Règle : seules les adresses PUBLIQUES sont jointes. Le nom est résolu
ici, toutes ses adresses doivent être publiques, et la connexion part
vers l'adresse vérifiée (pas vers le nom, qu'un DNS pourrait faire
changer entre le contrôle et la connexion).
"""

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit


class Blocked(Exception):
    """Cible refusée ; le message est rendu tel quel au modèle."""


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


async def public_target(url: str, allow_private: bool = False) -> tuple[str, str, int]:
    """(schéma, adresse vérifiée, port) pour une URL http(s). Lève Blocked
    si le schéma n'est pas http(s), si le nom ne se résout pas, ou si
    l'UNE de ses adresses n'est pas publique."""
    # urlsplit lève ValueError sur «http://[::1», et `.port` sur un port
    # hors bornes ou non numérique : c'est une URL refusée, pas une panne.
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise Blocked("URL invalide")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise Blocked("seules les URL http(s) sont lues")
    port = port or (443 if parts.scheme == "https" else 80)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            parts.hostname, port, type=socket.SOCK_STREAM)
    # ValueError : octet nul dans le nom (UnicodeError en est une).
    except (socket.gaierror, ValueError):
        raise Blocked(f"hôte introuvable : {parts.hostname}")
    ips = [info[4][0] for info in infos]
    if not ips:
        raise Blocked(f"hôte introuvable : {parts.hostname}")
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
