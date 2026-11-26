"""
Les fichiers de la page : le BUILD de Vite (web/dist, construit depuis
web/static par proxy/wasm, `npm run build`) et les runtimes des bacs
(web/static/vendor, hors du build).

    /ui                       dist/index.html (ui/dashboard/page.py)
    /ui/static/assets/…       scripts et styles du build, à empreinte dans
                              leur nom : gardés un an (immutable)
    /ui/static/vendor/…       les runtimes WASM, la VM Linux : demandés avec
                              leur empreinte (?v=, vendor/versions.json)
    /ui/static/…              le reste de dist (icônes, manifeste), sauf
                              index.html : l'application ne s'ouvre que sous
                              /ui, avec ses en-têtes de sécurité

Hors de assets/ (le fichier résolu, pas le chemin demandé), un fichier est
revalidé à chaque fois : un ETag inchangé rend 304, sans le contenu. Vite
pose lui-même les empreintes et les `<link rel="modulepreload">` de la page.

Ouvert sans clé (auth.ui_open) : c'est la page d'entrée qui en a besoin.
"""

import os

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, Response

from ..core.httputil import error_response

router = APIRouter()

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
DIST_DIR = os.path.realpath(os.path.join(WEB_DIR, "dist"))
VENDOR_DIR = os.path.realpath(os.path.join(WEB_DIR, "static", "vendor"))
PREFIX = "/ui/static/"
IMMUTABLE = "public, max-age=31536000, immutable"


def page() -> str | None:
    """Le HTML de la page construite ; None si le build manque."""
    try:
        with open(os.path.join(DIST_DIR, "index.html"), encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def _inside(root: str, path: str) -> str | None:
    full = os.path.realpath(os.path.join(root, path))
    return full if full.startswith(root + os.sep) and os.path.isfile(full) else None


@router.api_route(PREFIX + "{path:path}", methods=["GET", "HEAD"])
async def static_file(path: str, request: Request) -> Response:
    if path.startswith("vendor/"):
        full = _inside(VENDOR_DIR, path[len("vendor/"):])
    else:
        full = _inside(DIST_DIR, path)
        if full == os.path.join(DIST_DIR, "index.html"):
            full = None
    if full is None:
        return error_response("openai", 404, "not_found", "fichier inconnu")
    assets = os.path.join(DIST_DIR, "assets") + os.sep
    cache = IMMUTABLE if full.startswith(assets) else "no-cache"
    response = FileResponse(full, headers={"Cache-Control": cache},
                            stat_result=os.stat(full))
    if response.headers["etag"] in request.headers.get("if-none-match", ""):
        return Response(status_code=304, headers={
            "Cache-Control": cache, "ETag": response.headers["etag"]})
    return response
