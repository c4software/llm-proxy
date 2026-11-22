"""
Ce que partagent les outils des FORGES : `repository` (lire un projet) et
`pull_request` (y proposer une modification). Une source (storage/repos.py)
donne la forge, son adresse et le jeton ; le modèle ne choisit qu'une
source par son nom, un projet et une référence, vérifiés ici.

  valid_project, valid_ref   ce que le modèle peut nommer
  source(…)                  la source choisie, permise pour le projet
  api(source), auth(source)  la base de l'API de la forge, son en-tête
  get(…)                     une lecture (archive, branche par défaut) :
                             redirections suivies, jeton à l'origine seule
  call(…)                    une requête à l'API (JSON), sans redirection :
                             les écritures, et ce qu'elles vérifient
  default_branch(…)          la branche par défaut d'un projet

Aucune règle d'adresse : une source peut viser une forge du réseau interne
(choix du déploiement, docs/depots.md) — seules les listes de REFUS de
[tools.net] valent.
"""

import asyncio
import json
import re
from urllib.parse import quote, urlsplit

from ..core import config
from ..core.settings import FORGE_SCOPE
from ..storage import repos
from . import net
from .contract import ToolError

# Délai (s) d'une requête à la forge (et du téléchargement entier d'une
# archive, tools/repository.py).
TIMEOUT = config.num("repos.timeout", 120)
# Octets au plus d'une réponse JSON de l'API.
MAX_JSON = 1_000_000
USER_AGENT = "llm-proxy {} (+https://github.com/c4software/llm-proxy)"
# Un projet : « groupe/sous-groupe/projet », « propriétaire/dépôt ».
PROJECT = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_.\-]*(/[A-Za-z0-9_.][A-Za-z0-9_.\-]*)*\Z")
_REF = re.compile(r"[A-Za-z0-9_.\-/]{1,200}\Z")


def valid_project(project: str) -> bool:
    return bool(PROJECT.match(project)) and not {".", ".."} & set(project.split("/"))


def valid_ref(ref) -> bool:
    return isinstance(ref, str) and bool(_REF.match(ref)) and ".." not in ref \
        and not ref.startswith("-")


# ── la source ───────────────────────────────────────────────────────────

async def source(owner: str, wanted: str, project: str, write: bool = False) -> dict:
    """La source `wanted` du compte (la seule s'il n'en a qu'une), permise
    pour `project`. `write` : seules celles du compte cochées « écriture »
    (jamais une partagée). Dans le tour d'un événement de forge
    (settings.FORGE_SCOPE), seul le projet de l'événement, sur sa forge.
    Lève ToolError."""
    scope = FORGE_SCOPE.get()
    if scope is not None and not (scope[1] and project.casefold() == scope[1].casefold()):
        raise ToolError("not_allowed", (
            f"this answer was started by an event of the project {scope[1]!r}: only that "
            f"project can be read or changed." if scope[1] else
            "this answer was started by a forge event without a project: no repository "
            "can be read or changed."))
    sources = await asyncio.to_thread(repos.available, owner)
    if write:
        sources = [s for s in sources if repos.writable(s)]
    if not sources:
        raise ToolError("unavailable", (
            "no repository source of this user allows pull requests; they can tick "
            "« Écriture » on one of theirs in the dashboard, Configuration → Dépôts."
            if write else
            "no repository source is configured for this user; they can add one in "
            "the dashboard, Configuration → Dépôts."))
    if wanted:
        found = next((s for s in sources if s["name"].casefold() == wanted.casefold()), None)
    else:
        found = sources[0] if len(sources) == 1 else None
    if found is None:
        names = ", ".join(json.dumps(s["name"], ensure_ascii=False) for s in sources)
        raise ToolError("invalid_input", (
            f"unknown source {wanted!r}; " if wanted else
            "several sources exist, name one; ") + f"available: {names}.")
    if not repos.permits(found, project):
        raise ToolError("not_allowed", f"the source {found['name']!r} does not allow "
                                       f"the project {project!r}.")
    if scope is not None and (urlsplit(found["url"]).hostname or "").casefold() \
            != scope[0].casefold():
        raise ToolError("not_allowed", f"the source {found['name']!r} is not the forge of "
                                       f"the event that started this answer.")
    return found


def api(source: dict) -> str:
    """La base de l'API REST de la forge (pas la forge `git`)."""
    base = source["url"].rstrip("/")
    if source["forge"] == "gitlab":
        return f"{base}/api/v4"
    if source["forge"] == "github":
        host = urlsplit(base).hostname or ""
        return "https://api.github.com" if host in ("github.com", "api.github.com") \
            else f"{base}/api/v3"
    return f"{base}/api/v1"                       # gitea / forgejo


def auth(source: dict) -> dict:
    token = source.get("token") or ""
    if not token:
        return {}
    if source["forge"] == "gitlab":
        return {"PRIVATE-TOKEN": token}
    if source["forge"] == "github":
        return {"Authorization": f"Bearer {token}"}
    return {"Authorization": f"token {token}"}


def project_path(source: dict, project: str) -> str:
    """Le chemin d'API d'un projet : /projects/<id encodé> (GitLab),
    /repos/<propriétaire>/<dépôt> (GitHub, Gitea)."""
    if source["forge"] == "gitlab":
        return "/projects/" + quote(project, safe="")
    return "/repos/" + project


# ── les requêtes ────────────────────────────────────────────────────────

def bounded(limit: int):
    """La borne de net.download : laisse passer une redirection, dit un
    statut refusé avec les mots de la forge, et refuse un corps trop gros
    au lieu de le couper."""
    def bound(r, body: bytes) -> int:
        status = r.status_code
        if 300 <= status < 400:
            return 0
        if status in (401, 403):
            raise ToolError("not_allowed", f"the forge refused access (HTTP {status}): "
                                           f"the token of this source cannot read it.")
        if status == 404:
            raise ToolError("not_accessible", "project or ref not found (or not visible "
                                              "to the token of this source).")
        if status >= 400:
            return 0                      # net.download le dit (429 compris)
        declared = r.headers.get("content-length", "")
        if len(body) > limit or (declared.isdigit() and int(declared) > limit):
            raise ToolError("unsupported", f"the download is larger than {limit} bytes, "
                                           f"the limit of this proxy.")
        return limit + 1
    return bound


async def get(source: dict, url: str, limit: int, tool: str, transport=None,
              accept: str = "*/*") -> bytes:
    """Un téléchargement de la forge (une archive) : redirections suivies,
    le jeton au seul saut qui reste sur l'origine de la source."""
    _, _, body = await net.download(
        url, timeout=TIMEOUT, limit=bounded(limit), user_agent=USER_AGENT.format(tool),
        accept=accept, transport=transport, auth=auth(source), allow_private=True,
        domains=False)
    return body


def _said(raw: bytes) -> str:
    """Ce que la forge dit d'un refus (`message`, `error`, `errors`), court."""
    try:
        doc = json.loads(raw)
    except ValueError:
        return ""
    if not isinstance(doc, dict):
        return ""
    parts = [doc.get("message"), doc.get("error")]
    errors = doc.get("errors")
    if isinstance(errors, list):
        parts += [e.get("message") if isinstance(e, dict) else e for e in errors[:3]]
    text = "; ".join(str(p) for p in parts if isinstance(p, (str, list, dict)) and p)
    return text[:300]


async def call(source: dict, method: str, path: str, body=None, tool: str = "",
               transport=None, quiet: tuple = ()) -> tuple[int, object]:
    """Une requête à l'API de la forge (`path` sous api(source)), corps
    JSON, sans redirection (le jeton ne suit pas). → (statut, document
    JSON ou None). Un statut d'erreur lève ToolError, sauf ceux de `quiet`
    (une absence attendue : 404 d'un fichier)."""
    status, raw = await net.post(
        api(source) + path, None if body is None else json.dumps(body).encode(),
        "application/json" if body is not None else "", timeout=TIMEOUT,
        user_agent=USER_AGENT.format(tool), allow_private=True, limit=MAX_JSON + 1,
        transport=transport, method=method, auth=auth(source),
        accept="application/vnd.github+json" if source["forge"] == "github"
        else "application/json")
    if status in quiet:
        return status, None
    if len(raw) > MAX_JSON:
        raise ToolError("unsupported", "the forge answered more than this proxy reads.")
    if 300 <= status < 400:
        raise ToolError("not_accessible", f"the forge redirected the request (HTTP {status}); "
                                          f"check the address of the source.")
    if status >= 400:
        said = _said(raw)
        said = f": {said}" if said else ""
        if status in (401, 403):
            raise ToolError("not_allowed", f"the forge refused (HTTP {status}){said}. The "
                                           f"token of this source may lack the right.")
        if status == 404:
            raise ToolError("not_accessible", f"not found on the forge (HTTP 404){said}.")
        if status == 429:
            raise ToolError("too_many_requests", f"the forge asks to slow down (HTTP 429){said}.")
        raise ToolError("failed", f"the forge refused the request (HTTP {status}){said}.")
    try:
        return status, json.loads(raw) if raw.strip() else None
    except ValueError:
        raise ToolError("failed", "the forge answered something other than JSON.")


async def default_branch(source: dict, project: str, tool: str, transport=None) -> str:
    """Une lecture : par get, qui suit une redirection (un projet renommé)
    sans y porter le jeton."""
    raw = await get(source, api(source) + project_path(source, project), MAX_JSON, tool,
                    transport, "application/json")
    try:
        doc = json.loads(raw)
    except ValueError:
        doc = None
    ref = doc.get("default_branch") if isinstance(doc, dict) else None
    if not valid_ref(ref):
        raise ToolError("not_accessible", "could not read the default branch; pass it.")
    return ref
