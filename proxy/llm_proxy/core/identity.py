"""
L'identité d'un client : le condensé STABLE de la clé qu'il présente au
proxy (`owner`), jamais la clé. Partagé par l'auth, les outils, le
stockage et la page — d'où sa place dans le noyau, sans dépendance.
"""

import hashlib
import os

from . import config


# Secret du condensé des clés clientes : tiré UNE fois, gardé dans
# data/client.secret (0600). Le condensé d'une clé est donc STABLE d'un
# démarrage à l'autre — ce dont a besoin la mémoire persistante
# (tools/memory.py) ; il ne dit rien de la clé sans ce fichier.
SECRET_PATH = config.resolve("client.secret")
_secret: bytes | None = None


def _owner_secret() -> bytes:
    global _secret
    if _secret is None:
        try:
            with open(SECRET_PATH, "rb") as fh:
                _secret = fh.read()
        except FileNotFoundError:
            value = os.urandom(32)
            # O_EXCL : deux processus qui démarrent ensemble gardent le
            # même secret, celui du premier.
            try:
                fd = os.open(SECRET_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                             0o600)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(value)
                _secret = value
            except FileExistsError:
                with open(SECRET_PATH, "rb") as fh:
                    _secret = fh.read()
        if len(_secret) < 16:
            raise RuntimeError(f"{SECRET_PATH} : secret trop court ou vide")
    return _secret


def owner_secret() -> bytes:
    """Le secret des condensés (data/client.secret) : d'autres clés en
    dérivent (la session de la page, auth.UISession)."""
    return _owner_secret()


def owner(token: str) -> str:
    """Le client : un condensé de la clé qu'il présente au proxy, JAMAIS la
    clé, stable d'un démarrage à l'autre. «» (pas de clé) reste «» : c'est
    le client unique d'un proxy ouvert."""
    if not token:
        return ""
    return hashlib.blake2b(token.encode("utf-8", "surrogatepass"),
                           key=_owner_secret()[:64], digest_size=16).hexdigest()
