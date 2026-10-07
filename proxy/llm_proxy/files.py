"""
Le MAGASIN des fichiers rendus par un outil hébergé (contract.Result.
files) : une image tracée, un tableur sorti d'une exécution de code. Le
proxy les garde quelques heures et les sert par un LIEN,

    GET <public_url>/v1/files/<jeton>/<nom>

qu'il écrit lui-même à la fin de la réponse (`Stored.markdown` : une
image en markdown pour une image, un lien sinon). Jamais de data URL :
un client de chat la renverrait dans chaque requête suivante.

LE JETON VAUT DROIT D'ACCÈS. Le lien est ouvert par un navigateur, qui
n'envoie pas la clé du proxy : la route est donc HORS clé (app.py), et
seul le jeton — 192 bits tirés par `secrets`, un par fichier — protège le
fichier. Qui a le lien a le fichier, jusqu'à son expiration ; qui ne l'a
pas ne peut ni le deviner ni lister le magasin.

En MÉMOIRE VIVE, comme tout ce que le proxy garde entre deux requêtes
(tools.Memory, chat_api.Memory, le cache web) : des fichiers de quelques
heures n'ont pas à survivre au processus, et le volume data/ ne porte que
la configuration et les statistiques — pas de dossier à purger au
démarrage, ni de contenu écrit par un modèle sur le disque de l'hôte. Le
prix : un redémarrage du proxy casse les liens déjà rendus (404), et la
borne en octets se prend sur la mémoire du proxy. Bornée en durée, en
octets (les plus anciens sortent) et par fichier. Un seul processus : le
proxy n'a qu'un worker.

SERVIR SANS RISQUE. Le contenu est écrit par un programme qu'un modèle a
écrit : hostile. Il est servi depuis l'origine du proxy — celle du
tableau de bord — et rien ne doit s'y exécuter :
  * seules les images MATRICIELLES (PNG, JPEG, GIF, WebP), reconnues à
    leurs premiers octets et jamais à leur nom, sont servies en ligne,
    sous leur type ;
  * tout le reste — HTML, SVG, PDF, texte — part en
    `application/octet-stream` et `Content-Disposition: attachment` : le
    navigateur télécharge, il n'affiche pas ;
  * `X-Content-Type-Options: nosniff` (le type dit est le type) et
    `Content-Security-Policy: sandbox` (origine opaque, aucun script)
    sur toutes les réponses ;
  * le nom servi est refait (`safe_name`) : ni chemin, ni caractère de
    contrôle, ni guillemet dans l'en-tête.

Ce module ne connaît ni FastAPI ni les outils : app.py pose la route,
une surface range ce qu'un Result porte (`keep`).
"""

import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from urllib.parse import quote

from . import config

# L'adresse PUBLIQUE du proxy, celle que le navigateur du client joint
# («https://llm.example.org»). Vide : aucun lien ne peut être écrit, donc
# rien n'est gardé — un outil le sait par `refusal`, et le dit au modèle.
PUBLIC_URL = config.text("files.public_url", "").rstrip("/")
TTL = config.num("files.ttl", 4 * 3600)
MAX_BYTES = config.integer("files.max_bytes", 128_000_000)
MAX_FILE_BYTES = config.integer("files.max_file_bytes", 10_000_000)
# Le chemin de la route, que app.py exempte de la clé du proxy.
PATH = "/v1/files/"

# Les seuls types servis en ligne, par leurs premiers octets.
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)
_UNSAFE = re.compile(r"[^\w.-]+")


def sniff(data: bytes) -> str | None:
    """Le type d'une image matricielle, d'après ses premiers octets ;
    None pour tout le reste (SVG compris : c'est un document à scripts)."""
    for magic, kind in _MAGIC:
        if data.startswith(magic):
            return kind
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def safe_name(name: str) -> str:
    """Le nom servi : le dernier élément du chemin, réduit aux lettres,
    chiffres, `.`, `-` et `_` — rien qui ait un sens dans une URL, un
    lien markdown ou un en-tête. 80 caractères au plus, extension gardée."""
    name = _UNSAFE.sub("_", str(name).replace("\\", "/").rsplit("/", 1)[-1])
    name = name.strip("._") or "file"
    if len(name) > 80:
        stem, dot, ext = name.rpartition(".")
        ext = dot + ext if stem and len(ext) <= 10 else ""
        name = name[:80 - len(ext)] + ext
    return name


def is_link(path: str) -> bool:
    """`path` a-t-il la forme d'un lien, PATH<jeton>/<nom> ? Ce chemin-là,
    en lecture, est le seul que app.py sert sans la clé du proxy."""
    token, _, name = path[len(PATH):].partition("/")
    return path.startswith(PATH) and bool(token) and bool(name) \
        and "/" not in name


def refusal(size: int) -> str | None:
    """Pourquoi un fichier de cette taille ne serait PAS gardé — une
    raison en anglais, pour le texte du modèle — ou None s'il le serait.
    Un outil la demande avant d'annoncer un fichier comme rendu."""
    if not PUBLIC_URL:
        return "no public URL is configured on this proxy"
    limit = min(STORE.max_file_bytes, STORE.max_bytes)
    return f"larger than {limit} bytes" if size > limit else None


@dataclass(frozen=True)
class Stored:
    """Un fichier gardé, et de quoi le rendre au client."""
    token: str
    name: str               # le nom servi (safe_name)
    media_type: str         # le type SERVI : une image, ou octet-stream
    inline: bool            # affiché par le navigateur, ou téléchargé
    data: bytes
    expires: float          # time.monotonic()
    owner: str = ""         # tools.owner du client, pour les comptes

    @property
    def url(self) -> str:
        return f"{PUBLIC_URL}{PATH}{self.token}/{quote(self.name)}"

    @property
    def markdown(self) -> str:
        """Ce que le proxy ajoute à la réponse : l'image, ou un lien."""
        return f"![{self.name}]({self.url})" if self.inline \
            else f"[{self.name}]({self.url})"

    def headers(self) -> dict:
        """Les en-têtes de la réponse qui le sert (tête de module)."""
        left = max(int(self.expires - time.monotonic()), 0)
        plain = self.name.encode("ascii", "replace").decode().replace("?", "_")
        return {
            "Content-Type": self.media_type,
            "Content-Disposition": (
                f"{'inline' if self.inline else 'attachment'}; "
                f"filename=\"{plain}\"; filename*=UTF-8''{quote(self.name)}"),
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "Referrer-Policy": "no-referrer",
            # Une interface de chat d'une autre origine affiche l'image.
            "Cross-Origin-Resource-Policy": "cross-origin",
            "Cache-Control": f"private, max-age={left}",
        }


class Store:
    """Les fichiers gardés, par jeton. Bornés en durée (`ttl`), en octets
    toutes entrées confondues (`max_bytes` : les plus anciens sortent) et
    par fichier (`max_file_bytes` : refusé)."""

    def __init__(self, ttl: float, max_bytes: int, max_file_bytes: int):
        self.ttl, self.max_bytes = ttl, max_bytes
        self.max_file_bytes = max_file_bytes
        self.size = 0       # octets gardés
        self._data: OrderedDict[str, Stored] = OrderedDict()

    def put(self, name: str, data: bytes, owner: str = "") -> Stored | None:
        """Garde un fichier et rend de quoi le lier ; None s'il dépasse
        une borne. Le type servi se lit dans les OCTETS, jamais dans ce
        que l'outil en dit."""
        self._sweep()
        if len(data) > min(self.max_file_bytes, self.max_bytes):
            return None
        kind = sniff(data)
        stored = Stored(secrets.token_urlsafe(24), safe_name(name),
                        kind or "application/octet-stream", kind is not None,
                        bytes(data), time.monotonic() + self.ttl, owner)
        self._data[stored.token] = stored
        self.size += len(stored.data)
        while self.size > self.max_bytes:
            self._drop(next(iter(self._data)))
        return stored

    def get(self, token: str, name: str) -> Stored | None:
        """Le fichier de ce jeton, sous ce nom. Jeton inconnu, expiré, ou
        nom qui n'est pas le sien : None — la même réponse pour tous."""
        stored = self._data.get(token)
        if stored is None:
            return None
        if time.monotonic() > stored.expires:
            self._drop(token)
            return None
        return stored if name == stored.name else None

    def _sweep(self) -> None:
        # Durée unique, ordre d'arrivée : les expirés sont en tête.
        now = time.monotonic()
        while self._data and next(iter(self._data.values())).expires < now:
            self._drop(next(iter(self._data)))

    def _drop(self, token: str) -> None:
        stored = self._data.pop(token, None)
        if stored is not None:
            self.size -= len(stored.data)

    def clear(self) -> None:
        self._data.clear()
        self.size = 0

    def __len__(self) -> int:
        return len(self._data)


STORE = Store(TTL, MAX_BYTES, MAX_FILE_BYTES)


def keep(files, owner: str = "") -> list[Stored]:
    """Range les fichiers d'un Result (des contract.Artifact : `name`,
    `data`) et rend ceux qui sont gardés, dans l'ordre. Sans adresse
    publique, aucun : un lien ne pourrait pas être écrit."""
    if not PUBLIC_URL:
        return []
    kept = (STORE.put(f.name, f.data, owner) for f in files)
    return [stored for stored in kept if stored is not None]
