"""
Les fichiers de la page (web/static) : scripts, feuille de style, icônes.

Les scripts sont des MODULES ES (web/static/app.js et ce qu'il importe,
rangés par domaine : lib/, stats/, chat/, config/, memory/). La page les
demande sous un chemin VERSIONNÉ :

    /ui/static/v/<empreinte>/app.js

L'empreinte (VERSION) est celle de tout l'arbre web/static : un import
relatif d'un module (`./chat/store.js`) hérite du même préfixe, donc de
la même version — un navigateur ne peut pas mêler l'ancien et le nouveau
après une mise à jour. Sous ce préfixe, un fichier est gardé un an
(immutable) ; une empreinte périmée est servie quand même, mais sans
cache. Sans préfixe (/ui/static/icon.svg, le manifeste, les runtimes
WASM de vendor/), il est revalidé à chaque fois : un ETag inchangé rend
304, sans le contenu.

Ouvert sans clé (auth.ui_open) : c'est la page d'entrée qui en a besoin.
"""

import hashlib
import os
import re

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, Response

from .httputil import error_response

router = APIRouter()

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
STATIC_DIR = os.path.realpath(os.path.join(WEB_DIR, "static"))
PREFIX = "/ui/static/"


def _version() -> str:
    """Empreinte de web/static, sous-dossiers compris : noms, tailles et
    dates de tous ses fichiers."""
    h = hashlib.blake2b(digest_size=6)
    for root, dirs, names in os.walk(STATIC_DIR):
        dirs.sort()
        for name in sorted(names):
            path = os.path.join(root, name)
            st = os.stat(path)
            rel = os.path.relpath(path, STATIC_DIR)
            h.update(f"{rel}:{st.st_size}:{st.st_mtime_ns};".encode())
    return h.hexdigest()


VERSION = _version()
_VERSIONED = re.compile(r"v/([0-9a-f]+)/(.+)\Z")


def versioned(html: str) -> str:
    """Les scripts et la feuille de style de la page sous le préfixe
    versionné ; icônes et manifeste gardent leur URL (le manifeste d'une
    application installée doit rester où il est)."""
    return re.sub(r'/ui/static/([\w./-]+\.(?:js|css))(?=")',
                  rf"/ui/static/v/{VERSION}/\1", html)


@router.api_route(PREFIX + "{path:path}", methods=["GET", "HEAD"])
async def static_file(path: str, request: Request) -> Response:
    cache = "no-cache"
    if m := _VERSIONED.match(path):
        path = m.group(2)
        if m.group(1) == VERSION:
            cache = "public, max-age=31536000, immutable"
    full = os.path.realpath(os.path.join(STATIC_DIR, path))
    if not full.startswith(STATIC_DIR + os.sep) or not os.path.isfile(full):
        return error_response("openai", 404, "not_found", "fichier inconnu")
    response = FileResponse(full, headers={"Cache-Control": cache},
                            stat_result=os.stat(full))
    if response.headers["etag"] in request.headers.get("if-none-match", ""):
        return Response(status_code=304, headers={
            "Cache-Control": cache, "ETag": response.headers["etag"]})
    return response
