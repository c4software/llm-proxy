"""
`image_generation` : le modèle décrit une image, le proxy la fait générer
par le modèle d'images d'un de ses backends (API OpenAI
`POST /v1/images/generations`) et la REND à l'utilisateur comme fichier
(`Result.files`) : la surface la range (files.py) et écrit son lien — une
image en markdown — à la fin de la réponse. Le modèle, lui, ne reçoit
qu'une phrase : le nom du fichier, son type, ses dimensions. Jamais de
base64 ni d'URL dans ce qu'il lit, et que la mémoire garde.

Le modèle d'images est une adresse de CONFIGURATION
([tools.image_generation].model = "<backend>/<modèle>") : la requête part
par le client HTTP du backend (backends.py), comme une requête relayée,
sans passer par app.py. Ce que app.py fait autour d'un relais est refait
ici, en petit, comme dans transcribe.py : la porte de quota et la ligne
de statistiques « requête », sous l'endpoint «/v1/tools/image_generation».

Ce que le backend rend. `{"data": [{"b64_json": …}]}` (gufo, relevé le
07/10/2026 : un PNG, sans qu'on le demande) ou `{"data": [{"url": …}]}`
(le défaut d'autres serveurs). `response_format` n'est PAS envoyé : plus
d'un modèle le refuse ; les deux formes sont lues. Une `url` est une
adresse que le BACKEND écrit — ni le modèle, ni la configuration :
  * de la MÊME origine que le backend (ou relative) : c'est l'adresse de
    configuration, déjà jointe pour générer — lue par son client, avec sa
    clé, sans le garde-fou des adresses publiques (un backend local est
    une adresse privée, et c'est voulu) ;
  * de toute AUTRE origine (un stockage, un CDN) : une adresse que
    personne ici n'a choisie — lue par le téléchargement gardé
    (net.download, [tools.net]) comme une cible du modèle : adresses
    publiques seulement. Un backend ne fait pas lire au proxy un autre
    service du réseau local.
Le type de l'image se lit dans ses OCTETS, jamais dans ce que le backend
en dit ; ce qui n'est pas une image matricielle n'est pas rendu.

RETOUCHE ([tools.image_generation].edits, inactive par défaut) : avec
`image_url`, l'image est téléchargée sous le garde-fou — c'est une cible
choisie par le modèle — puis envoyée avec le prompt à
`POST /v1/images/edits` (multipart). Elle n'arrive QUE par URL publique :
une image que le proxy vient de rendre ne se retouche donc que si son
adresse publique ([files].public_url) est jointe du proxy sans être
privée.

Une génération coûte des dizaines de secondes de GPU — et, sur un backend
qui ne tient qu'un modèle en mémoire, le déchargement du modèle de la
conversation : un délai à lui (Tool.timeout), un compte d'appels à lui et
bas (Tool.max_calls), une image par appel, et rien n'est demandé au
backend si l'image ne pourrait pas être remise (pas d'adresse publique).

Sans liaison à l'API Responses ni à l'API Messages : l'outil se déclare
par {"type": "image_generation"} sur /v1/chat/completions (ou
[chat].always) et s'appelle par POST /v1/tools/image_generation.
"""

import asyncio
import base64
import binascii
import hashlib
import re
import struct
import time

import httpx

from .. import albert, backends, config, files, stats
from ..settings import is_exempt, log
from . import net
from .contract import Artifact, Call, Result, Tool, ToolError

ENABLED = config.flag("tools.image_generation.enabled", False)
# «<backend>/<modèle>», préfixé comme tout modèle du proxy. Vide → l'outil
# répond au modèle que la génération d'images n'est pas configurée.
MODEL = config.text("tools.image_generation.model", "").strip()
# Les tailles que le modèle peut demander («LxH»), et celle d'un appel
# qui n'en demande pas. Le coût croît avec la surface.
SIZES = config.strings("tools.image_generation.sizes",
                       ("512x512", "768x768", "1024x1024"))
SIZE = config.text("tools.image_generation.size", "512x512").strip()
# Délai (s) de la requête au backend, chargement du modèle compris.
TIMEOUT = config.num("tools.image_generation.timeout", 300)
# Délai (s) d'un téléchargement : l'image à retoucher, et l'image qu'un
# backend rend par `url`.
DOWNLOAD_TIMEOUT = config.num("tools.image_generation.download_timeout", 30)
# Appels par réponse, comptés à part (Tool.max_calls) : une image coûte
# cher, et un modèle qui « améliore » son image n'en finit pas.
MAX_CALLS = config.integer("tools.image_generation.max_calls", 2)
# Octets de l'image rendue, au plus ; au-delà elle n'est pas remise. La
# borne du magasin ([files].max_file_bytes) vaut aussi.
MAX_BYTES = config.integer("tools.image_generation.max_bytes", 10_000_000)
# La retouche d'une image donnée par URL (tête de module), et les octets
# de cette image au plus : refusée au-delà, pas coupée.
EDITS = config.flag("tools.image_generation.edits", False)
MAX_INPUT_BYTES = config.integer("tools.image_generation.max_input_bytes",
                                 10_000_000)
MAX_PROMPT_CHARS = 4000
USER_AGENT = ("llm-proxy image_generation "
              "(+https://github.com/c4software/llm-proxy)")
PATH = "/v1/images/generations"
PATH_EDITS = "/v1/images/edits"
# L'endpoint des lignes de statistiques des requêtes au modèle d'images :
# la route du proxy d'où elles naissent (voir transcribe.ENDPOINT).
ENDPOINT = "/v1/tools/image_generation"

NAME = "image_generation"

_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
        "image/webp": "webp"}

if SIZE not in SIZES or not all(re.fullmatch(r"[1-9]\d*x[1-9]\d*", s)
                                for s in SIZES):
    raise SystemExit(
        f"{config.CONFIG_PATH} : tools.image_generation.sizes doit lister "
        f"des tailles «LxH» et contenir `size` (reçu {SIZES}, {SIZE!r})")


def dimensions(data: bytes) -> str:
    """«LxH» d'un PNG, d'un GIF ou d'un JPEG, lu dans ses octets ; «» si
    elles ne s'y lisent pas (WebP, fichier abîmé)."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return "%dx%d" % struct.unpack(">II", data[16:24])
        if data[:3] == b"GIF":
            return "%dx%d" % struct.unpack("<HH", data[6:10])
        i = 2 if data[:2] == b"\xff\xd8" else len(data)
        while data[i:i + 1] == b"\xff":     # segments JPEG, jusqu'au SOFn
            if 0xC0 <= data[i + 1] <= 0xCF and data[i + 1] not in (
                    0xC4, 0xC8, 0xCC):
                height, width = struct.unpack(">HH", data[i + 5:i + 9])
                return f"{width}x{height}"
            i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    except (struct.error, IndexError):
        pass
    return ""


def _backend() -> tuple[backends.Backend, str]:
    """(backend, modèle sans préfixe) de [tools.image_generation].model,
    lu à l'appel : un modèle absent ou mal préfixé est une réponse au
    modèle, pas un proxy qui ne démarre pas."""
    backend, prefixed = backends.route_backend({"model": MODEL}) \
        if MODEL else (None, False)
    if backend is None or not prefixed or backend.client is None:
        if MODEL:
            log.warning("image_generation : modèle %r sans backend joignable "
                        "([tools.image_generation].model = "
                        "\"<backend>/<modèle>\")", MODEL)
        raise ToolError("unavailable",
                        "image generation is not configured on this proxy.")
    return backend, MODEL[len(backend.name) + 1:]


async def _ask(path: str, fields: dict, image=None) -> dict:
    """Une requête au modèle d'images — JSON, ou multipart avec `image`
    (nom, octets, type) pour une retouche — → le premier élément de
    `data`. Même parcours qu'une requête relayée par app.py : le limiteur
    d'un backend à quotas, puis une ligne de statistiques, quelle que
    soit l'issue. Le corps d'une erreur du backend va au journal, pas au
    modèle."""
    backend, model = _backend()
    if backend.quotas and not is_exempt(path):
        try:    # une requête, coût 1 : une image n'est pas des tokens
            await backend.quota_state.get_limiter({"model": model}).acquire(1)
        except albert.QuotaWaitTooLong:
            raise ToolError("too_many_requests", (
                "the image model is over its rate limit. Do not retry now."))
    fields = {"model": model, **fields}
    started = time.monotonic()
    # 504 : l'attente a été abandonnée (délai de l'outil) avant la réponse.
    status = 504
    try:
        try:
            r = await backend.client.post(
                path, headers=backend.auth_headers(),
                timeout=httpx.Timeout(TIMEOUT, connect=backend.connect_timeout),
                **({"json": fields} if image is None
                   else {"data": fields, "files": {"image": image}}))
        except httpx.HTTPError as exc:
            status = 502 if backend.quotas else 503
            log.warning("image_generation : backend %s injoignable (%s)",
                        backend.name, type(exc).__name__)
            raise ToolError("unavailable", (
                "the image backend is offline or did not answer. Do not "
                "retry now: tell the user the image could not be made."))
        status = r.status_code
        if status >= 400:
            log.warning("image_generation : %s a répondu %d : %s",
                        backend.name, status, r.text[:300])
            if status == 429:
                raise ToolError("too_many_requests", (
                    "the image model is busy. Do not retry now."))
            # La demande est en cause, pas le backend : un prompt qu'il
            # refuse, une taille qu'il ne fait pas, une image qu'il ne lit
            # pas.
            if status in (400, 413, 415, 422):
                raise ToolError("invalid_input", (
                    f"the image model refused this request (HTTP {status}). "
                    f"Change the prompt or the size; do not send it again "
                    f"unchanged."))
            raise ToolError("unavailable", (
                f"the image backend failed (HTTP {status}). Do not retry "
                f"now: tell the user the image could not be made."))
        try:
            item = r.json()["data"][0]
        except (ValueError, LookupError, TypeError):
            item = None
        if not isinstance(item, dict):
            raise ToolError("unavailable",
                            "the image backend returned no image.")
        return item
    finally:
        try:
            stats.record(MODEL, backend.name, model, ENDPOINT, status,
                         time.monotonic() - started, 0, 0, True, False)
        except Exception:  # les statistiques ne cassent jamais un outil
            log.exception("stats : génération d'image non enregistrée")


async def _collect(item: dict, transport) -> bytes:
    """Les octets de l'image d'un élément de `data` : son `b64_json`, ou
    ce qu'on lit à son `url` (tête de module : par le client du backend
    si elle est de son origine, sous le garde-fou sinon). Lus jusqu'à
    MAX_BYTES + 1 : de quoi savoir qu'elle dépasse."""
    b64, url = item.get("b64_json"), item.get("url")
    if isinstance(b64, str) and b64:
        try:
            return base64.b64decode(b64)
        except (binascii.Error, ValueError):
            raise ToolError("unavailable",
                            "the image backend returned an unreadable image.")
    if not isinstance(url, str) or not url:
        raise ToolError("unavailable", "the image backend returned no image.")
    backend, _ = _backend()
    try:
        base = httpx.URL(backend.url)
        target = base.join(url)
        if (target.scheme, target.netloc) == (base.scheme, base.netloc):
            body = bytearray()
            async with backend.client.stream(
                    "GET", target, headers=backend.auth_headers(),
                    timeout=httpx.Timeout(
                        DOWNLOAD_TIMEOUT,
                        connect=backend.connect_timeout)) as r:
                async for chunk in r.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        break
            status, body = r.status_code, bytes(body)
        else:
            _, r, body = await asyncio.wait_for(net.download(
                str(target), timeout=DOWNLOAD_TIMEOUT, limit=MAX_BYTES + 1,
                user_agent=USER_AGENT, accept="image/*", transport=transport),
                DOWNLOAD_TIMEOUT)
            status = r.status_code
    except (ToolError, httpx.HTTPError, httpx.InvalidURL,
            asyncio.TimeoutError) as exc:
        # Le refus du garde-fou compris : son texte nomme une adresse du
        # backend, dont le modèle ne ferait rien.
        log.warning("image_generation : image rendue par URL non lue (%s) : "
                    "%s %s", url[:200], type(exc).__name__, exc)
        status, body = 0, b""
    if status != 200:
        raise ToolError("unavailable", (
            "the image was generated but could not be fetched from the "
            "image backend. Do not retry now."))
    return body


def _limit(r, body: bytes) -> int:
    """La borne de lecture de l'image à retoucher (net.download) : rien
    d'une réponse qui n'est pas 2xx, pas un octet de plus dès que les
    premiers disent que ce n'est pas une image, et un octet au-delà de
    MAX_INPUT_BYTES — de quoi savoir qu'elle le dépasse."""
    if not 200 <= r.status_code < 300:
        return 0
    if len(body) >= 12 and files.sniff(body[:12]) is None:
        return len(body)
    return MAX_INPUT_BYTES + 1


async def download(url: str, settings, transport) -> tuple[str, bytes, str]:
    """L'image à retoucher, sous le garde-fou : (nom de fichier, octets,
    type lu dans les octets). Lève ToolError."""
    try:
        url, r, body = await asyncio.wait_for(net.download(
            url, settings, timeout=DOWNLOAD_TIMEOUT, limit=_limit,
            user_agent=USER_AGENT, accept="image/*", transport=transport),
            DOWNLOAD_TIMEOUT)
    except asyncio.TimeoutError:
        raise ToolError("not_accessible", (
            f"{url} took more than {int(DOWNLOAD_TIMEOUT)} s to download."))
    if not 200 <= r.status_code < 300:  # un 3xx sans Location
        raise ToolError("not_accessible",
                        f"{url} returned HTTP {r.status_code}.")
    kind = files.sniff(body)
    if kind is None:
        raise ToolError("unsupported", (
            f"{url} is not a PNG, JPEG, GIF or WebP image, which is all "
            f"this tool can edit."))
    if len(body) > MAX_INPUT_BYTES:
        raise ToolError("unsupported", (
            f"{url} is larger than {MAX_INPUT_BYTES} bytes, which this tool "
            f"does not edit."))
    return f"image.{_EXT[kind]}", body, kind


def _asked(args: dict, name: str) -> str:
    """Un argument chaîne facultatif : «» s'il est absent."""
    value = args.get(name)
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise ToolError("invalid_input", f"`{name}` must be a string.")
    return value.strip()


class ImageGeneration(Tool):
    name = NAME

    @property
    def enabled(self) -> bool:
        return ENABLED

    @property
    def timeout(self) -> float:
        """Le délai de l'outil (contrat : Tool.timeout), à la place de
        [tools].run_timeout : la génération, et les deux téléchargements
        possibles — l'image à retoucher, l'image rendue par `url`."""
        return TIMEOUT + 2 * DOWNLOAD_TIMEOUT

    @property
    def max_calls(self) -> int:
        return MAX_CALLS

    def prompt(self, present) -> str:
        return (
            "Generate an image from a text description"
            + (", or edit an existing image given by its URL" if EDITS
               else "") + ". The image is delivered to the user "
            "automatically, with your answer: you only get a short "
            "confirmation, you cannot see the image, and you must not "
            "write a link or a markdown image for it. Write a complete, "
            "self-contained prompt. Generation is slow (it can take a "
            "minute or more): call it once per image the user asked "
            f"for, {MAX_CALLS} at most in one answer.")

    def parameters(self, present) -> dict:
        properties = {
            "prompt": {
                "type": "string",
                "description": "What the image must show: subject, style, "
                               "composition, colours, any text to draw."},
            "size": {
                "type": "string", "enum": list(SIZES),
                "description": f"Width x height in pixels (default {SIZE}). "
                               f"Larger is much slower."},
        }
        if EDITS:
            properties["image_url"] = {
                "type": "string",
                "description": "To EDIT an existing image instead of "
                               "creating one: its public http(s) URL. "
                               "`prompt` then describes the change."}
        return {"type": "object", "properties": properties,
                "required": ["prompt"]}

    async def run(self, args: dict, call: Call, transport=None) -> Result:
        """`transport` : celui des TÉLÉCHARGEMENTS gardés (tests). Les
        requêtes au modèle d'images partent par le client du backend."""
        prompt = args.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ToolError("invalid_input", "`prompt` is required: what "
                                             "the image must show.")
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ToolError("invalid_input", f"`prompt` is longer than "
                                             f"{MAX_PROMPT_CHARS} characters.")
        size = _asked(args, "size").lower().replace("×", "x").replace(" ", "")
        if size and size not in SIZES:
            raise ToolError("invalid_input", "`size` must be one of "
                                             + ", ".join(SIZES) + ".")
        source = _asked(args, "image_url")
        if source and not EDITS:
            raise ToolError("invalid_input", (
                "this proxy does not edit images: `image_url` is not "
                "accepted. Call again without it to generate a new image."))
        # Dit AVANT de dépenser une génération : non configuré, ou une
        # image qui ne pourrait pas être remise.
        _backend()
        why = files.refusal(0)
        if why:
            raise ToolError("unavailable", (
                f"images cannot be delivered to the user on this proxy "
                f"({why}). Do not retry: tell the user."))
        if source:
            # La taille d'une retouche est celle de l'image, sauf demande.
            item = await _ask(
                PATH_EDITS, {"prompt": prompt.strip(),
                             **({"size": size} if size else {})},
                await download(source, call.settings, transport))
        else:
            item = await _ask(PATH, {"prompt": prompt.strip(),
                                     "size": size or SIZE})
        data = await _collect(item, transport)
        kind = files.sniff(data)
        if kind is None:
            log.warning("image_generation : %s n'a pas rendu une image "
                        "matricielle (%d octets, début %r)", MODEL, len(data),
                        data[:12])
            raise ToolError("unavailable",
                            "the image backend returned an unreadable image.")
        why = files.refusal(len(data)) or (
            f"larger than {MAX_BYTES} bytes" if len(data) > MAX_BYTES else None)
        if why:
            raise ToolError("unsupported", (
                f"the image was generated but is not delivered to the user: "
                f"{why}. Ask for a smaller size."))
        # Un nom par image : le modèle les distingue dans une conversation.
        name = (f"image-{hashlib.blake2b(data, digest_size=3).hexdigest()}"
                f".{_EXT[kind]}")
        shown = dimensions(data) or size or SIZE
        return Result(
            f"Image {'edited' if source else 'generated'}: {name} ({kind}, "
            f"{shown}, {max(len(data) // 1000, 1)} kB). It is shown to the "
            f"user with your answer; do not write a link or a markdown image "
            f"yourself. You cannot see it: do not describe details the "
            f"prompt does not state.",
            files=(Artifact(name, kind, data),),
            meta={"model": MODEL, "file": name, "media_type": kind,
                  "size": shown, "bytes": len(data)})


TOOL = ImageGeneration()
