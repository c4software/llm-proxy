"""
L'API HTTP de l'exécuteur : trois routes derrière un jeton, une de santé.

  GET  /healthz       sans jeton — pour le healthcheck du conteneur. Dit
                      seulement si le service est prêt.
  GET  /v1/status     l'état : bacs ouverts, bornes, ce que podman tient
                      réellement ici (cgroups), langages.
  POST /v1/execute    {"client", "session", "language", "code", "timeout"?}
                      → exécute dans le bac de (client, session), créé au
                      besoin. `session` vide = un bac d'un seul appel.
                      200 : {"exit_code", "timed_out", "output",
                      "truncated", "fresh", "reset", "seconds",
                      "files": [{"name", "size", "data" (base64)}],
                      "skipped": [{"name", "reason"}]}.
                      Un programme qui sort en erreur ou dépasse son délai
                      est un 200 : c'est un résultat.
  POST /v1/destroy    {"client", "session"} → {"destroyed": bool}

Erreurs : {"error": {"code", "message"}} — 400 `invalid_request`, 401
`unauthorized`, 429 `busy` (tous les bacs exécutent), 503 `not_ready`
(podman ne démarre pas ici, ou pas de jeton) et `sandbox_failed`.

Le JETON (EXECUTOR_TOKEN) est partagé avec le proxy et exigé sur /v1/* :
le réseau du compose est interne, mais tout conteneur qui le rejoindrait
pourrait sinon faire exécuter du code. Sans jeton configuré le service
démarre et REFUSE tout (503) — il ne tourne jamais ouvert.

Ce que le service ne fait pas : parler au modèle (les textes sont écrits
par l'outil du proxy), garder un fichier (ils repartent dans la réponse),
journaliser le code ou sa sortie (des mesures seulement).

Réglages, par l'environnement (le service n'a ni volume ni fichier de
configuration) : EXECUTOR_TOKEN, SANDBOX_ROOTFS ou SANDBOX_IMAGE, et les
bornes SANDBOX_* de sandbox.Limits.
"""

import asyncio
import base64
import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .sandbox import LANGS, Busy, Limits, SandboxError, Sandboxes

log = logging.getLogger("executor")

# Corps d'une requête : le code d'un appel, pas un fichier.
MAX_BODY = 2_000_000
SWEEP_EVERY = 30.0      # s, entre deux passages du nettoyage


def error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}},
                        status_code=status)


def create_app(box: Sandboxes, token: str) -> FastAPI:
    """L'application, autour de `box`. `token` vide : tout /v1 est refusé."""
    state = {"ready": False, "error": "démarrage", "started": time.time()}

    async def sweeper():
        while True:
            await asyncio.sleep(SWEEP_EVERY)
            try:
                gone = await box.sweep()
                if gone:
                    log.info("%d bac(s) expiré(s) détruit(s), %d ouvert(s)",
                             gone, len(box))
            except Exception:
                log.exception("nettoyage des bacs en échec")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not token:
            state["error"] = "EXECUTOR_TOKEN absent : tout est refusé"
            log.error(state["error"])
        try:
            # Dans l'ordre : les restes d'un processus précédent, puis ce
            # que podman sait tenir ici.
            orphans = await box.reap_orphans()
            kept = await box.probe()
            log.info("podman prêt : %d orphelin(s) détruit(s) ; bornes de "
                     "cgroups tenues : %s", orphans,
                     ", ".join(sorted(kept)) or "AUCUNE")
            missing = {"memory", "cpu", "pids"} - kept
            if missing:
                log.warning(
                    "bornes NON tenues par bac (cgroups non délégués) : %s — "
                    "seul le plafond du conteneur exécuteur les borne",
                    ", ".join(sorted(missing)))
            if token:
                state["ready"], state["error"] = True, ""
        except SandboxError as exc:
            state["error"] = str(exc)
            log.error("exécuteur HORS SERVICE : %s", exc)
        task = asyncio.create_task(sweeper())
        try:
            yield
        finally:
            task.cancel()
            await box.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)

    @app.middleware("http")
    async def require_token(request: Request, call_next):
        if request.url.path != "/healthz":
            scheme, _, presented = request.headers.get(
                "authorization", "").partition(" ")
            # En octets : compare_digest refuse une chaîne non ASCII.
            if not token or scheme.lower() != "bearer" \
                    or not hmac.compare_digest(
                        presented.strip().encode("utf-8", "surrogatepass"),
                        token.encode("utf-8")):
                if not token:
                    return error(503, "not_ready", state["error"])
                return error(401, "unauthorized",
                             "jeton absent ou invalide (Authorization: "
                             "Bearer <EXECUTOR_TOKEN>)")
        return await call_next(request)

    @app.get("/healthz")
    async def healthz():
        if state["ready"]:
            return {"status": "ok"}
        # Le détail (un message de podman) ne sort que derrière le jeton.
        return JSONResponse({"status": "error"}, status_code=503)

    @app.get("/v1/status")
    async def status():
        lim = box.limits
        return {
            "status": "ok" if state["ready"] else "error",
            "error": state["error"],
            "uptime": int(time.time() - state["started"]),
            "sessions": len(box),
            "languages": sorted(LANGS),
            "source": {"rootfs": box.rootfs} if box.rootfs
            else {"image": box.image},
            # Ce que podman tient PAR BAC ici. false = seul le plafond du
            # conteneur exécuteur borne cette ressource.
            "cgroup": {name: name in box.cgroup
                       for name in ("memory", "cpu", "pids")},
            "limits": {f: getattr(lim, f) for f in lim.__dataclass_fields__},
        }

    async def body(request: Request) -> dict | JSONResponse:
        # La taille annoncée d'abord : un corps démesuré n'est pas lu.
        announced = request.headers.get("content-length", "")
        raw = b"" if announced.isdigit() and int(announced) > MAX_BODY \
            else await request.body()
        if len(raw) > MAX_BODY or not raw and announced not in ("", "0"):
            return error(400, "invalid_request",
                         f"corps de plus de {MAX_BODY} octets")
        try:
            doc = json.loads(raw)
        except ValueError:
            doc = None
        if not isinstance(doc, dict) \
                or not isinstance(doc.get("client", ""), str) \
                or not isinstance(doc.get("session", ""), str) \
                or len(doc.get("client", "")) > 128 \
                or len(doc.get("session", "")) > 128:
            return error(400, "invalid_request",
                         "corps attendu : un objet JSON, `client` et "
                         "`session` en chaînes de 128 caractères au plus")
        return doc

    @app.post("/v1/execute")
    async def execute(request: Request):
        doc = await body(request)
        if isinstance(doc, JSONResponse):
            return doc
        language, code, limit = doc.get("language"), doc.get("code"), \
            doc.get("timeout")
        if language not in LANGS:
            return error(400, "invalid_request",
                         f"`language` : un de {', '.join(sorted(LANGS))}")
        if not isinstance(code, str) or not code.strip():
            return error(400, "invalid_request", "`code` : une chaîne non vide")
        if limit is not None and (isinstance(limit, bool)
                                  or not isinstance(limit, (int, float))):
            return error(400, "invalid_request", "`timeout` : un nombre")
        if not state["ready"]:
            return error(503, "not_ready", state["error"])
        client, session = doc.get("client", ""), doc.get("session", "")
        try:
            out = await box.execute(client, session, language, code, limit)
        except Busy as exc:
            return error(429, "busy", str(exc))
        except SandboxError as exc:
            log.error("bac à sable en échec : %s", exc)
            return error(503, "sandbox_failed", str(exc))
        # Des mesures, jamais le code ni sa sortie.
        log.info("exécution %s client=%s : code=%s délai=%s %.2fs, %d octets "
                 "de sortie, %d fichier(s), %d écarté(s)%s%s", language,
                 client[:8] or "-", out.exit_code, out.timed_out, out.seconds,
                 len(out.output), len(out.files), len(out.skipped),
                 " [bac neuf]" if out.fresh else "",
                 " [bac détruit]" if out.reset else "")
        return {
            "exit_code": out.exit_code, "timed_out": out.timed_out,
            "output": out.output, "truncated": out.truncated,
            "fresh": out.fresh, "reset": out.reset,
            "seconds": round(out.seconds, 3),
            "files": [{"name": f.name, "size": len(f.data),
                       "data": base64.b64encode(f.data).decode("ascii")}
                      for f in out.files],
            "skipped": out.skipped,
        }

    @app.post("/v1/destroy")
    async def destroy(request: Request):
        doc = await body(request)
        if isinstance(doc, JSONResponse):
            return doc
        return {"destroyed": await box.destroy(doc.get("client", ""),
                                               doc.get("session", ""))}

    return app


def from_env() -> FastAPI:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    image = os.environ.get("SANDBOX_IMAGE", "").strip()
    rootfs = "" if image else os.environ.get(
        "SANDBOX_ROOTFS", "/opt/sandbox/rootfs").strip()
    return create_app(Sandboxes(rootfs, image, Limits.from_env()),
                      os.environ.get("EXECUTOR_TOKEN", "").strip())
