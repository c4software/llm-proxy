"""
Le champ « model » d'un corps multipart/form-data (transcription audio,
édition d'image) : le lire pour router, le réécrire pour retirer le préfixe
de backend.

Le routage du proxy se fait sur le préfixe du modèle (« bigchuck/… »). Pour un
corps JSON, backends.route_backend le lit directement ; un formulaire
multipart, lui, n'est pas du JSON : sans ce module, la requête partait vers le
backend de repli (Albert) quel que soit le modèle demandé. Seule la valeur du
champ « model » est touchée ; les autres parties (fichiers compris) sont
recopiées octet pour octet. La longueur change, mais Content-Length n'est
jamais relayé (HOP_BY_HOP) : httpx recalcule la bonne.
"""

import re

_BOUNDARY = re.compile(r'boundary="?([^";]+)"?', re.IGNORECASE)
_MODEL_HEADER = re.compile(
    rb'content-disposition:[^\r\n]*\bname="?model"?(?:[;\r\n]|$)', re.IGNORECASE)


def is_multipart(content_type: str) -> bool:
    return (content_type or "").lower().startswith("multipart/form-data")


def _boundary(content_type: str) -> bytes | None:
    m = _BOUNDARY.search(content_type or "")
    return m.group(1).encode() if m else None


def _model_span(raw: bytes, content_type: str) -> tuple[int, int] | None:
    """(début, fin) de la VALEUR du champ model dans `raw`, ou None."""
    boundary = _boundary(content_type)
    if not boundary:
        return None
    delim = b"--" + boundary
    pos = 0
    while True:
        start = raw.find(delim, pos)
        if start < 0:
            return None
        head_start = start + len(delim)
        head_end = raw.find(b"\r\n\r\n", head_start)
        if head_end < 0:
            return None
        nxt = raw.find(b"\r\n" + delim, head_end)
        if nxt < 0:
            return None
        if _MODEL_HEADER.search(raw[head_start:head_end + 2]):
            return head_end + 4, nxt
        pos = nxt + 2


def model_field(raw: bytes, content_type: str) -> str | None:
    """Valeur du champ model, ou None s'il n'y en a pas."""
    span = _model_span(raw, content_type)
    if span is None:
        return None
    return raw[span[0]:span[1]].decode("utf-8", "replace").strip()


def rewrite_model_field(raw: bytes, content_type: str, value: str) -> bytes:
    """`raw` avec la valeur du champ model remplacée ; inchangé sans champ."""
    span = _model_span(raw, content_type)
    if span is None:
        return raw
    return raw[:span[0]] + value.encode() + raw[span[1]:]
