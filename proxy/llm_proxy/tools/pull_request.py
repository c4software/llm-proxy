"""
`pull_request` : PROPOSER une modification d'un projet d'une forge
(GitLab, GitHub, Gitea/Forgejo) — une branche neuve, un commit, une pull
request (merge request) en BROUILLON. Rien n'est fusionné : la relecture
humaine tient lieu de confirmation, puisqu'un webhook entrant n'a personne
derrière lui.

Le modèle donne une source (par son nom), un projet, la branche de départ
(la branche par défaut sinon), un titre, une description et les FICHIERS
ENTIERS à écrire (ou à supprimer). Le proxy :

  vérifie   avant tout appel réseau : une source du compte cochée
            « écriture » (storage/repos.writable, jamais une partagée),
            les chemins (ni `..`, ni `.git`, ni ceux de BLOCKED et des
            motifs de la source : la CI tourne sur une branche poussée,
            avec les secrets du projet), les bornes, la borne horaire
  nomme     la branche : PREFIX, le titre réduit, un condensé du projet,
            de la base et des fichiers — la même modification (même titre,
            mêmes fichiers) retombe sur la même, déjà là : refusée
  écrit     par l'API de la forge (forge.call : jeton à l'origine seule,
            aucune redirection) :
    gitlab  branche ? · fichiers existants ? (HEAD) · POST commits
            (actions, start_branch) · POST merge_requests (« Draft: »)
    github  branche ? · fichiers existants (contents : un fichier, pas un
            lien ni un sous-module) · ref de la base · son commit · POST
            git/trees (contenus en ligne) · POST git/commits · POST
            git/refs · POST pulls (draft ; une offre sans brouillons :
            « [Draft] »)
    gitea   branche ? · fichiers existants (leur sha) · POST contents
            (new_branch) · POST pulls (« WIP: »)

Un échec après la création de la branche la supprime : la même
modification redemandée ne retombe pas sur une branche sans PR. Le texte
de la PR finit toujours par FOOTER, ajouté par le proxy.
"""

import asyncio
import base64
import fnmatch
import hashlib
import re
from urllib.parse import quote

from ..core import config
from ..core.window import Window
from ..storage import repos
from . import forge, repository
from .contract import Call, Result, Tool, ToolError, ToolName

NAME = ToolName.PULL_REQUEST
ENABLED = config.flag("repos.pull_requests", False)
MAX_FILES = config.integer("repos.pr_max_files", 20)
# Octets au plus de tous les contenus d'une PR (en UTF-8).
MAX_BYTES = config.integer("repos.pr_max_bytes", 1_000_000)
# Pull requests au plus par compte et par heure glissante (0 = sans borne).
PER_HOUR = config.integer("repos.pr_per_hour", 10)
PREFIX = config.text("repos.pr_branch_prefix", "llm-proxy/").strip() or "llm-proxy/"
# Les points d'entrée usuels de la CI : une branche poussée les exécute.
DEFAULT_BLOCKED = (".gitlab-ci.yml", ".gitlab-ci/*", ".gitlab/ci/*", ".github/workflows/*",
                   ".github/actions/*", ".gitea/workflows/*", ".forgejo/workflows/*",
                   ".woodpecker*", ".drone.yml", ".circleci/*", "Jenkinsfile", ".travis.yml",
                   "azure-pipelines.yml", "bitbucket-pipelines.yml", ".buildkite/*",
                   ".tekton/*")
BLOCKED = (*DEFAULT_BLOCKED, *config.strings("repos.pr_blocked_paths"))
MAX_TITLE = 200
MAX_DESCRIPTION = 20_000
MAX_PATH = 400
FOOTER = "\n\n---\nOpened by the llm-proxy `pull_request` tool: review before merging."
FORGES = ("gitlab", "github", "gitea")
HOUR = Window(3600)

if not forge.valid_ref(PREFIX + "x") or "//" in PREFIX or any(
        p.startswith(".") or p.endswith(".lock") for p in PREFIX.split("/")):
    raise SystemExit(f"{config.CONFIG_PATH} : [repos].pr_branch_prefix « {PREFIX} » "
                     f"n'est pas un début de nom de branche valide")


def blocked(path: str, source: dict) -> bool:
    """Le chemin relève-t-il d'un motif refusé (ceux du proxy, puis ceux
    de la source) ? Casse ignorée : une forge sur un système insensible à
    la casse lirait `.GitLab-CI.yml`."""
    low = path.lower()
    return any(fnmatch.fnmatchcase(low, p.lower())
               for p in (*BLOCKED, *(source.get("blocked_paths") or ())))


def _path(value) -> str:
    path = value.strip().strip("/") if isinstance(value, str) else ""
    parts = path.split("/")
    if not path or len(path) > MAX_PATH or any(
            p in ("", ".", "..") or p.lower() == ".git" for p in parts) \
            or re.search(r"[\x00-\x1f\\]", path):
        raise ToolError("invalid_input", f"invalid file path {str(value)[:100]!r}: a "
                                         f"relative path inside the project is expected.")
    return path


def _files(value) -> list[dict]:
    """[{path, content}] ou [{path, delete: true}], vérifiés et bornés."""
    if not isinstance(value, list) or not value:
        raise ToolError("invalid_input", "`files` must be a non-empty list of "
                                         "{path, content} objects.")
    if len(value) > MAX_FILES:
        raise ToolError("invalid_input", f"at most {MAX_FILES} files per pull request.")
    out, seen, size = [], set(), 0
    for item in value:
        if not isinstance(item, dict):
            raise ToolError("invalid_input", "each file must be an object {path, content}.")
        path = _path(item.get("path"))
        if path.lower() in seen:
            raise ToolError("invalid_input", f"the file {path!r} is given twice.")
        seen.add(path.lower())
        if item.get("delete") is True:
            out.append({"path": path, "content": None})
            continue
        content = item.get("content")
        if not isinstance(content, str):
            raise ToolError("invalid_input", f"`content` of {path!r} must be the whole "
                                             f"text of the file (or `delete`: true).")
        size += len(content.encode("utf-8", "surrogatepass"))
        if size > MAX_BYTES:
            raise ToolError("invalid_input", f"the files exceed {MAX_BYTES} bytes, the "
                                             f"limit of this proxy.")
        out.append({"path": path, "content": content})
    return out


def _branch(title: str, project: str, base: str, files: list[dict]) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].strip("-") or "change"
    digest = hashlib.sha256(repr((project, base, [(f["path"], f["content"]) for f in files]))
                            .encode("utf-8", "surrogatepass")).hexdigest()[:8]
    return f"{PREFIX}{slug}-{digest}"


# ── les forges ──────────────────────────────────────────────────────────

class _Forge:
    """Les appels d'une proposition, sur une source et un projet."""

    def __init__(self, source: dict, project: str, transport):
        self.source, self.project, self.transport = source, project, transport
        self.root = forge.project_path(source, project)

    async def call(self, method: str, path: str, body=None, quiet=()):
        return await forge.call(self.source, method, self.root + path, body, NAME,
                                self.transport, quiet)

    async def existing(self, base: str, files: list[dict]) -> list:
        """Le sha de chaque fichier sur la base (GitHub, Gitea : contents),
        None s'il n'y est pas ; refuse ce qui n'est pas un fichier, et la
        suppression d'un fichier absent."""
        async def sha(f):
            status, doc = await self.call(
                "GET", f"/contents/{quote(f['path'], safe='/')}?ref={quote(base, safe='')}",
                quiet=(404,))
            if status == 404:
                _needs(f["content"] is not None, f)
                return None
            if not isinstance(doc, dict) or doc.get("type") != "file":
                raise ToolError("invalid_input", f"{f['path']!r} is not a regular file of "
                                                 f"the project.")
            return str(doc.get("sha") or "")
        return await asyncio.gather(*(sha(f) for f in files))

    async def drop(self, branch: str) -> None:
        """La branche créée, supprimée après un échec (au mieux)."""
        path = {"gitlab": "/repository/branches/" + quote(branch, safe=""),
                "github": "/git/refs/heads/" + quote(branch, safe="/"),
                "gitea": "/branches/" + quote(branch, safe="/")}[self.source["forge"]]
        try:
            await self.call("DELETE", path, quiet=(404,))
        except ToolError:
            pass

    async def has_branch(self, branch: str) -> bool:
        path = {"gitlab": "/repository/branches/" + quote(branch, safe=""),
                "github": "/git/ref/heads/" + quote(branch, safe="/"),
                "gitea": "/branches/" + quote(branch, safe="/")}[self.source["forge"]]
        status, _ = await self.call("GET", path, quiet=(404,))
        return status != 404

    async def gitlab(self, base, branch, files, title, description) -> str:
        async def exists(path):
            status, _ = await self.call(
                "HEAD", f"/repository/files/{quote(path, safe='')}?ref={quote(base, safe='')}",
                quiet=(404,))
            return status != 404
        found = await asyncio.gather(*(exists(f["path"]) for f in files))
        actions = []
        for f, there in zip(files, found):
            if f["content"] is None:
                _needs(there, f)
                actions.append({"action": "delete", "file_path": f["path"]})
            else:
                actions.append({"action": "update" if there else "create",
                                "file_path": f["path"], "content": f["content"]})
        await self.call("POST", "/repository/commits", {
            "branch": branch, "start_branch": base, "commit_message": title,
            "actions": actions})
        _, mr = await self.after(branch, self.call("POST", "/merge_requests", {
            "source_branch": branch, "target_branch": base, "title": "Draft: " + title,
            "description": description}))
        return _url(mr, "web_url")

    async def after(self, branch: str, step):
        """Une étape après la création de la branche : en cas d'échec, la
        branche est supprimée."""
        try:
            return await step
        except BaseException:
            await asyncio.shield(self.drop(branch))
            raise

    async def github(self, base, branch, files, title, description) -> str:
        await self.existing(base, files)
        _, ref = await self.call("GET", "/git/ref/heads/" + quote(base, safe="/"))
        head = _field(ref, "object", "sha")
        _, commit = await self.call("GET", "/git/commits/" + head)
        tree = [{"path": f["path"], "mode": "100644", "type": "blob",
                 **({"sha": None} if f["content"] is None else {"content": f["content"]})}
                for f in files]
        _, made = await self.call("POST", "/git/trees", {
            "base_tree": _field(commit, "tree", "sha"), "tree": tree})
        _, new = await self.call("POST", "/git/commits", {
            "message": title, "tree": _field(made, "sha"), "parents": [head]})
        await self.call("POST", "/git/refs", {"ref": "refs/heads/" + branch,
                                              "sha": _field(new, "sha")})
        _, pr = await self.after(branch, self._github_pull(
            {"title": title, "head": branch, "base": base, "body": description}))
        return _url(pr, "html_url")

    async def _github_pull(self, pull: dict):
        try:
            return await self.call("POST", "/pulls", {**pull, "draft": True})
        except ToolError as exc:
            # Une offre sans brouillons (dépôt privé d'une offre gratuite) :
            # une PR ordinaire, dite brouillon par son titre.
            if exc.code != "failed" or "draft" not in exc.message.lower():
                raise
            return await self.call("POST", "/pulls", {**pull, "title": "[Draft] " + pull["title"]})

    async def gitea(self, base, branch, files, title, description) -> str:
        shas = await self.existing(base, files)
        changes = []
        for f, known in zip(files, shas):
            change = {"path": f["path"]}
            if f["content"] is None:
                change.update(operation="delete", sha=known)
            else:
                change.update(operation="update" if known is not None else "create",
                              content=base64.b64encode(f["content"].encode(
                                  "utf-8", "surrogatepass")).decode("ascii"))
                if known is not None:
                    change["sha"] = known
            changes.append(change)
        try:
            await self.call("POST", "/contents", {
                "branch": base, "new_branch": branch, "message": title, "files": changes})
        except ToolError as exc:
            if exc.code == "not_accessible":
                # La route d'un commit de plusieurs fichiers manque avant Gitea 1.20.
                raise ToolError("unsupported", "this forge cannot commit several files at "
                                               "once (Gitea 1.20 or later is needed).")
            raise
        _, pr = await self.after(branch, self.call("POST", "/pulls", {
            "title": "WIP: " + title, "head": branch, "base": base, "body": description}))
        return _url(pr, "html_url")


def _needs(there: bool, f: dict) -> None:
    if not there:
        raise ToolError("invalid_input", f"cannot delete {f['path']!r}: no such file "
                                         f"on the base branch.")


def _field(doc, *keys) -> str:
    for k in keys:
        doc = doc.get(k) if isinstance(doc, dict) else None
    if not isinstance(doc, str) or not doc:
        raise ToolError("failed", "the forge answered without the expected "
                                  f"`{'.'.join(keys)}`.")
    return doc


def _url(doc, key: str) -> str:
    url = doc.get(key) if isinstance(doc, dict) else None
    return url if isinstance(url, str) and url.startswith(("https://", "http://")) else ""


# ── l'outil ─────────────────────────────────────────────────────────────

class PullRequestTool(Tool):
    name = NAME
    max_calls = 1
    timeout = forge.TIMEOUT + 10

    @property
    def enabled(self) -> bool:
        # Les sources se règlent dans la partie Dépôts, ouverte avec l'outil
        # repository.
        return ENABLED and repository.TOOL.enabled

    def prompt(self, present) -> str:
        return (
            "Propose a change to a git repository the user configured with write "
            "access (GitLab, GitHub or Gitea): the proxy creates a new branch, commits "
            "the files and opens a DRAFT pull request (merge request) for a human to "
            "review; nothing is merged. Use it ONLY when the user, or the incoming "
            "message, asks for a change to be proposed; never on your own initiative. "
            "Each entry of `files` replaces the WHOLE file with `content` (or deletes "
            "it with `delete`: true): read the current file first (repository tool) "
            "and give its complete new text, never an excerpt. CI configuration "
            "files cannot be changed. `base` is the branch to start from and to "
            "merge into; omit it for the default branch. `source` names the source, "
            "omitted when only one allows pull requests. `title`: one line; "
            "`description`: what changes and why, in Markdown. One pull request per "
            "answer.")

    def parameters(self, present) -> dict:
        return {"type": "object", "properties": {
            "project": {"type": "string", "description": (
                "Path of the project on the forge: group/subgroup/project, owner/repo.")},
            "source": {"type": "string", "description": (
                "Name of the source; optional when only one allows pull requests.")},
            "base": {"type": "string", "description": "Branch to start from; omit for the default branch."},
            "title": {"type": "string", "description": "Title of the pull request, one line."},
            "description": {"type": "string", "description": "Body of the pull request, in Markdown."},
            "files": {"type": "array", "description": (
                f"The files to write, {MAX_FILES} at most."), "items": {
                "type": "object", "properties": {
                    "path": {"type": "string", "description": "Path in the project, e.g. src/app.py."},
                    "content": {"type": "string", "description": "The complete new text of the file."},
                    "delete": {"type": "boolean", "description": "true to delete the file instead."},
                }, "required": ["path"]}},
        }, "required": ["project", "title", "files"]}

    def summary(self, args: dict, result: Result | None = None) -> dict:
        out = {"type": self.name, "project": str(args.get("project") or ""),
               "title": str(args.get("title") or "")}
        if result is not None and result.meta.get("url"):
            out["url"] = result.meta["url"]
        return out

    async def run(self, args: dict, call: Call, transport=None) -> Result:
        """`transport` : pour les tests — vers la forge."""
        project = str(args.get("project") or "").strip().strip("/")
        if not forge.valid_project(project):
            raise ToolError("invalid_input", "`project` must be a path like group/project.")
        title = " ".join(str(args.get("title") or "").split())
        if not title or len(title) > MAX_TITLE:
            raise ToolError("invalid_input", f"`title` is required, {MAX_TITLE} characters "
                                             f"at most.")
        description = str(args.get("description") or "").strip()
        if len(description) > MAX_DESCRIPTION:
            raise ToolError("invalid_input", f"`description` is longer than "
                                             f"{MAX_DESCRIPTION} characters.")
        base = str(args.get("base") or "").strip()
        if base and not forge.valid_ref(base):
            raise ToolError("invalid_input", "`base` must be a branch name.")
        files = _files(args.get("files"))
        source = await forge.source(call.client, str(args.get("source") or "").strip(),
                                    project, write=True)
        if source["forge"] not in FORGES:
            raise ToolError("unsupported", "pull requests need a GitLab, GitHub or Gitea source.")
        if refused := [f["path"] for f in files if blocked(f["path"], source)]:
            raise ToolError("not_allowed", "these files cannot be changed by this tool (CI "
                                           "configuration or a path the user protected): "
                                           + ", ".join(refused) + ".")
        if delay := HOUR.wait(call.client, PER_HOUR):
            raise ToolError("too_many_requests", f"at most {PER_HOUR} pull requests per hour; "
                                                 f"try again in {delay / 60:.0f} min.")
        on = _Forge(source, project, transport)
        try:
            async with asyncio.timeout(forge.TIMEOUT):
                base = base or await forge.default_branch(source, project, NAME, transport)
                branch = _branch(title, project, base, files)
                if await on.has_branch(branch):
                    raise ToolError("failed", f"the branch {branch!r} already exists: this "
                                              f"change was already proposed.")
                url = await getattr(on, source["forge"])(base, branch, files, title,
                                                         description + FOOTER)
        except TimeoutError:
            await asyncio.to_thread(repos.used, call.client, source["id"], "timeout")
            raise ToolError("timeout", f"the forge took more than {forge.TIMEOUT:g} s.")
        except ToolError as exc:
            await asyncio.to_thread(repos.used, call.client, source["id"], exc.message)
            raise
        await asyncio.to_thread(repos.used, call.client, source["id"])
        where = f"{source['name']}:{project}"
        return Result(f"[{where}] Draft pull request opened: {url or '(no link returned)'}\n"
                      f"Branch {branch} into {base}, {len(files)} file(s).",
                      meta={"source": source["name"], "project": project, "url": url,
                            "branch": branch})


TOOL = PullRequestTool()
