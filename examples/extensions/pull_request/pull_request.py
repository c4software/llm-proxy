"""
Extension `pull_request` (docs/extensions.md) : PROPOSER une modification
d'un projet d'une forge — une branche neuve, un commit, une pull request
(merge request) en BROUILLON, jamais fusionnée. La relecture humaine tient
lieu de confirmation : un webhook entrant n'a personne derrière lui.

Un COMPTE DE SERVICE ouvre les PR : son jeton est réglé par l'admin, et
sert à tout compte qui a la page ou un webhook. La sûreté tient à
`projects` (étroit), à la branche par défaut protégée sur la forge, à la
PR en brouillon et aux chemins refusés (la CI tourne sur une branche
poussée, avec les secrets du projet).

  [extensions.pull_request]
  forge = "gitlab"                    # gitlab | github | gitea
  url = "https://gitlab.exemple.fr"   # l'adresse de la forge (pas de l'API)
  token = "${PR_TOKEN}"               # le jeton du compte de service
  projects = ["equipe/*"]             # les projets permis (motifs) ; aucun sinon
  blocked_paths = ["deploy/*"]        # refusés, en plus des chemins de CI
  allow_private = false               # true : forge sur le réseau interne

Offert au chat de la page et aux webhooks (Offer.WEB), une PR par réponse,
PER_ACCOUNT par compte et PER_HOUR pour tout le proxy par heure glissante.
Chaque PR est dite au journal et signée du condensé court du compte.
"""

import asyncio
import base64
import fnmatch
import hashlib
import json
import re
import time
from collections import deque
from urllib.parse import quote, urlsplit

from llm_proxy import extension as ext

REQUIRES = 1

NAME = "pull_request"
MAX_FILES = 20
MAX_BYTES = 1_000_000            # tous les contenus d'une PR, en UTF-8
PER_ACCOUNT = 5                  # PR par compte et par heure glissante
PER_HOUR = 20                    # PR pour tout le proxy, par heure glissante
MAX_TITLE = 200
MAX_DESCRIPTION = 20_000
MAX_PATH = 400
TIMEOUT = 60                     # toute la proposition, appels compris
PREFIX = "llm-proxy/"
USER_AGENT = "llm-proxy-pull-request"
# Les points d'entrée usuels de la CI : une branche poussée les exécute.
CI_PATHS = (".gitlab-ci.yml", ".gitlab-ci/*", ".gitlab/ci/*", ".github/workflows/*",
            ".github/actions/*", ".gitea/workflows/*", ".forgejo/workflows/*",
            ".woodpecker*", ".drone.yml", ".circleci/*", "Jenkinsfile", ".travis.yml",
            "azure-pipelines.yml", "bitbucket-pipelines.yml", ".buildkite/*", ".tekton/*")
FORGES = ("gitlab", "github", "gitea")
SEGMENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")
REF = re.compile(r"[A-Za-z0-9._/-]{1,200}\Z")
SHA = re.compile(r"[0-9a-f]{40,64}\Z")

log = ext.logger(NAME)
_HITS: dict[str, deque] = {}


def _config() -> dict:
    return ext.settings(NAME)


def _strings(value) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) and v for v in value)


def _delay(key: str, bound: int, now: float) -> float:
    """Secondes avant qu'une PR de plus tienne dans la borne de `key` (0 :
    elle tient)."""
    hits = _HITS.setdefault(key, deque())
    while hits and now - hits[0] >= 3600:
        hits.popleft()
    return 3600 - (now - hits[0]) if len(hits) >= bound else 0


def valid_project(project: str, forge: str) -> bool:
    parts = project.split("/")
    if any(not SEGMENT.match(p) or p in (".", "..") or p.endswith(".git") for p in parts):
        return False
    return len(parts) == 2 if forge in ("github", "gitea") else len(parts) >= 2


def valid_ref(ref: str) -> bool:
    return bool(REF.match(ref)) and ".." not in ref and "//" not in ref \
        and not ref.startswith("/") and not ref.endswith(("/", ".lock")) \
        and not any(p.startswith(".") for p in ref.split("/"))


def blocked(path: str, extra) -> bool:
    """Le chemin relève-t-il d'un motif refusé ? Casse ignorée : une forge
    sur un système insensible à la casse lirait `.GitLab-CI.yml`."""
    low = path.lower()
    return any(fnmatch.fnmatchcase(low, str(p).lower()) for p in (*CI_PATHS, *extra))


def _path(value) -> str:
    path = value.strip().strip("/") if isinstance(value, str) else ""
    parts = path.split("/")
    if not path or len(path) > MAX_PATH or any(
            p in ("", ".", "..") or p.lower() == ".git" for p in parts) \
            or re.search(r"[\x00-\x1f\\]", path):
        raise ext.ToolError("invalid_input", f"invalid file path {str(value)[:100]!r}: a "
                                             f"relative path inside the project is expected.")
    return path


def _files(value) -> list[dict]:
    """[{path, content}] ou [{path, delete: true}], vérifiés et bornés."""
    if not isinstance(value, list) or not value:
        raise ext.ToolError("invalid_input", "`files` must be a non-empty list of "
                                             "{path, content} objects.")
    if len(value) > MAX_FILES:
        raise ext.ToolError("invalid_input", f"at most {MAX_FILES} files per pull request.")
    out, seen, size = [], set(), 0
    for item in value:
        if not isinstance(item, dict):
            raise ext.ToolError("invalid_input", "each file must be an object {path, content}.")
        path = _path(item.get("path"))
        if path.lower() in seen:
            raise ext.ToolError("invalid_input", f"the file {path!r} is given twice.")
        seen.add(path.lower())
        if item.get("delete") is True:
            out.append({"path": path, "content": None})
            continue
        content = item.get("content")
        if not isinstance(content, str):
            raise ext.ToolError("invalid_input", f"`content` of {path!r} must be the whole "
                                                 f"text of the file (or `delete`: true).")
        size += len(content.encode("utf-8", "surrogatepass"))
        if size > MAX_BYTES:
            raise ext.ToolError("invalid_input", f"the files exceed {MAX_BYTES} bytes.")
        out.append({"path": path, "content": content})
    return out


def _branch(title: str, project: str, base: str, files: list[dict]) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].strip("-") or "change"
    digest = hashlib.sha256(repr((project, base, [(f["path"], f["content"]) for f in files]))
                            .encode("utf-8", "surrogatepass")).hexdigest()[:8]
    return f"{PREFIX}{slug}-{digest}"


def _sha(doc, *keys) -> str:
    """Un sha rendu par la forge, vérifié avant d'entrer dans un chemin."""
    value = _field(doc, *keys)
    if not SHA.match(value):
        raise ext.ToolError("failed", f"the forge answered an invalid `{'.'.join(keys)}`.")
    return value


def _field(doc, *keys) -> str:
    for k in keys:
        doc = doc.get(k) if isinstance(doc, dict) else None
    if not isinstance(doc, str) or not doc:
        raise ext.ToolError("failed", f"the forge answered without the expected "
                                      f"`{'.'.join(keys)}`.")
    return doc


def _url(doc, key: str) -> str:
    url = doc.get(key) if isinstance(doc, dict) else None
    return url if isinstance(url, str) and url.startswith(("https://", "http://")) else ""


def _said(raw: bytes) -> str:
    """Ce que la forge dit d'un refus (son `message`), court."""
    try:
        doc = json.loads(raw)
    except ValueError:
        return ""
    msg = doc.get("message") or doc.get("error") if isinstance(doc, dict) else None
    return " ".join(str(msg).split())[:300] if msg else ""


# ── la forge ────────────────────────────────────────────────────────────

class Forge:
    """Les appels d'une proposition, sur un projet, au jeton de service."""

    def __init__(self, cfg: dict, project: str, transport=None):
        self.kind, self.project, self.transport = cfg["forge"], project, transport
        self.private = cfg.get("allow_private") is True
        root = str(cfg["url"]).rstrip("/")
        token = str(cfg["token"])
        if self.kind == "gitlab":
            api = root + "/api/v4"
            self.root = f"{api}/projects/{quote(project, safe='')}"
            self.auth = {"PRIVATE-TOKEN": token}
        elif self.kind == "github":
            host = urlsplit(root).hostname or ""
            api = "https://api.github.com" if host in ("github.com", "www.github.com") \
                else root + "/api/v3"
            self.root = f"{api}/repos/{project}"
            self.auth = {"Authorization": f"Bearer {token}",
                         "X-GitHub-Api-Version": "2022-11-28"}
        else:
            self.root = f"{root}/api/v1/repos/{project}"
            self.auth = {"Authorization": f"token {token}"}

    async def call(self, method: str, path: str, body=None, quiet=()):
        """→ (statut, document JSON ou None). Un statut de `quiet` est
        rendu tel quel ; une autre erreur lève ToolError."""
        content = json.dumps(body).encode() if body is not None else None
        status, raw = await ext.post(
            self.root + path, content, "application/json" if content else "",
            timeout=TIMEOUT, user_agent=USER_AGENT, allow_private=self.private,
            limit=2_000_000, transport=self.transport, method=method, auth=self.auth,
            accept="application/json")
        if status in quiet:
            return status, None
        if status >= 400:
            code = {401: "unavailable", 403: "not_allowed", 404: "not_accessible",
                    429: "too_many_requests"}.get(status, "not_accessible"
                                                   if status >= 500 else "failed")
            said = _said(raw)
            raise ext.ToolError(code, f"the forge refused {method} {path.split('?')[0]} "
                                      f"(HTTP {status}{': ' + said if said else ''}).")
        try:
            return status, json.loads(raw) if raw.strip() else None
        except ValueError:
            return status, None

    async def default_branch(self) -> str:
        _, doc = await self.call("GET", "")
        return _field(doc, "default_branch")

    def _branch_path(self, branch: str, read: bool = False) -> str:
        if self.kind == "gitlab":
            return "/repository/branches/" + quote(branch, safe="")
        if self.kind == "github":
            return ("/git/ref/heads/" if read else "/git/refs/heads/") + quote(branch, safe="/")
        return "/branches/" + quote(branch, safe="/")

    async def has_branch(self, branch: str) -> bool:
        status, _ = await self.call("GET", self._branch_path(branch, read=True), quiet=(404,))
        return status != 404

    async def drop(self, branch: str) -> None:
        """La branche créée, supprimée après un échec (au mieux)."""
        try:
            await self.call("DELETE", self._branch_path(branch), quiet=(404,))
        except ext.ToolError:
            pass

    async def after(self, branch: str, step):
        """Une étape après la création de la branche : en cas d'échec, la
        branche est supprimée — la même modification redemandée ne retombe
        pas sur une branche sans PR."""
        try:
            return await step
        except BaseException:
            await asyncio.shield(self.drop(branch))
            raise

    async def existing(self, base: str, files: list[dict]) -> list:
        """Le sha de chaque fichier sur la base (GitHub, Gitea), None s'il
        n'y est pas ; refuse ce qui n'est pas un fichier, et la
        suppression d'un fichier absent."""
        async def sha(f):
            status, doc = await self.call(
                "GET", f"/contents/{quote(f['path'], safe='/')}?ref={quote(base, safe='')}",
                quiet=(404,))
            if status == 404:
                _needs(f["content"] is not None, f)
                return None
            if not isinstance(doc, dict) or doc.get("type") != "file":
                raise ext.ToolError("invalid_input", f"{f['path']!r} is not a regular file "
                                                     f"of the project.")
            return str(doc.get("sha") or "")
        return await asyncio.gather(*(sha(f) for f in files))

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

    async def github(self, base, branch, files, title, description) -> str:
        _, ref = await self.call("GET", "/git/ref/heads/" + quote(base, safe="/"))
        head = _sha(ref, "object", "sha")
        _, commit = await self.call("GET", "/git/commits/" + head)
        base_tree = _sha(commit, "tree", "sha")
        # Le mode de chaque fichier de la base : un script garde son bit x ;
        # un lien ou un sous-module n'est pas un fichier à réécrire.
        _, listing = await self.call("GET", f"/git/trees/{base_tree}?recursive=1")
        modes = {e.get("path"): e.get("mode") for e in (listing or {}).get("tree") or ()
                 if isinstance(e, dict)}
        tree = []
        for f in files:
            mode = modes.get(f["path"])
            if mode is None and (listing or {}).get("truncated"):
                await self.existing(base, [f])      # arbre tronqué : demandé un à un
            elif mode is None:
                _needs(f["content"] is not None, f)
            elif mode not in ("100644", "100755"):
                raise ext.ToolError("invalid_input", f"{f['path']!r} is not a regular file "
                                                     f"of the project.")
            tree.append({"path": f["path"], "mode": mode or "100644", "type": "blob",
                         **({"sha": None} if f["content"] is None
                            else {"content": f["content"]})})
        _, made = await self.call("POST", "/git/trees", {
            "base_tree": base_tree, "tree": tree})
        _, new = await self.call("POST", "/git/commits", {
            "message": title, "tree": _sha(made, "sha"), "parents": [head]})
        await self.call("POST", "/git/refs", {"ref": "refs/heads/" + branch,
                                              "sha": _sha(new, "sha")})
        _, pr = await self.after(branch, self.call("POST", "/pulls", {
            "title": title, "head": branch, "base": base, "body": description,
            "draft": True}))
        return _url(pr, "html_url")

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
        await self.call("POST", "/contents", {
            "branch": base, "new_branch": branch, "message": title, "files": changes})
        _, pr = await self.after(branch, self.call("POST", "/pulls", {
            "title": "WIP: " + title, "head": branch, "base": base, "body": description}))
        return _url(pr, "html_url")


def _needs(there: bool, f: dict) -> None:
    if not there:
        raise ext.ToolError("invalid_input", f"cannot delete {f['path']!r}: no such file "
                                             f"on the base branch.")


# ── l'outil ─────────────────────────────────────────────────────────────

class PullRequest(ext.Tool):
    name = NAME
    offered = frozenset({ext.Offer.WEB})
    max_calls = 1
    timeout = TIMEOUT + 10

    @property
    def enabled(self) -> bool:
        # Réglé : une forge connue, son adresse, un jeton, des projets (une
        # liste de motifs), `blocked_paths` absent ou une liste : un réglage
        # mal typé met l'outil hors service plutôt que de perdre un refus.
        cfg = _config()
        return cfg.get("forge") in FORGES and isinstance(cfg.get("url"), str) \
            and bool(cfg.get("url")) and isinstance(cfg.get("token"), str) \
            and bool(cfg.get("token")) and _strings(cfg.get("projects")) \
            and _strings(cfg.get("blocked_paths", []))

    def prompt(self, present) -> str:
        projects = ", ".join(str(p) for p in _config().get("projects") or ())
        return (
            "Propose a change to a git repository: the proxy creates a new branch, "
            "commits the files and opens a DRAFT pull request (merge request) for a "
            "human to review; nothing is merged. Use it ONLY when the user, or the "
            "incoming message, asks for a change to be proposed; never on your own "
            "initiative. Each entry of `files` replaces the WHOLE file with `content` "
            "(or deletes it with `delete`: true): read the current file first and give "
            "its complete new text, never an excerpt. CI configuration files cannot be "
            "changed. `base` is the branch to start from and to merge into; omit it for "
            "the default branch. `title`: one line; `description`: what changes and "
            f"why, in Markdown. One pull request per answer. Allowed projects: {projects}.")

    def parameters(self, present) -> dict:
        return {"type": "object", "properties": {
            "project": {"type": "string", "description": (
                "Path of the project on the forge: group/subgroup/project, owner/repo.")},
            "base": {"type": "string", "description": "Branch to start from; omit for the default branch."},
            "title": {"type": "string", "description": "Title of the pull request, one line."},
            "description": {"type": "string", "description": "Body of the pull request, in Markdown."},
            "files": {"type": "array", "description": f"The files to write, {MAX_FILES} at most.",
                      "items": {"type": "object", "properties": {
                          "path": {"type": "string", "description": "Path in the project, e.g. src/app.py."},
                          "content": {"type": "string", "description": "The complete new text of the file."},
                          "delete": {"type": "boolean", "description": "true to delete the file instead."},
                      }, "required": ["path"]}},
        }, "required": ["project", "title", "files"]}

    def summary(self, args: dict, result=None) -> dict:
        out = {"type": self.name, "project": str(args.get("project") or ""),
               "title": str(args.get("title") or "")}
        if result is not None and result.meta.get("url"):
            out["url"] = result.meta["url"]
        return out

    async def run(self, args: dict, call, transport=None):
        """`transport` : pour les tests — vers la forge."""
        cfg = _config()
        project = str(args.get("project") or "").strip().strip("/")
        if not valid_project(project, cfg.get("forge")):
            raise ext.ToolError("invalid_input", "`project` must be a path like group/project.")
        # Casse ignorée, comme les `projects` des sources de dépôts.
        if not any(fnmatch.fnmatchcase(project.lower(), p.lower())
                   for p in cfg.get("projects") or ()):
            raise ext.ToolError("not_allowed", f"the project {project!r} is not one this "
                                               f"proxy may propose changes to.")
        title = " ".join(str(args.get("title") or "").split())
        if not title or len(title) > MAX_TITLE:
            raise ext.ToolError("invalid_input", f"`title` is required, {MAX_TITLE} "
                                                 f"characters at most.")
        description = str(args.get("description") or "").strip()
        if len(description) > MAX_DESCRIPTION:
            raise ext.ToolError("invalid_input", f"`description` is longer than "
                                                 f"{MAX_DESCRIPTION} characters.")
        base = str(args.get("base") or "").strip()
        if base and not valid_ref(base):
            raise ext.ToolError("invalid_input", "`base` must be a branch name.")
        files = _files(args.get("files"))
        extra = cfg.get("blocked_paths") or ()
        if refused := [f["path"] for f in files if blocked(f["path"], extra)]:
            raise ext.ToolError("not_allowed", "these files cannot be changed by this tool "
                                               "(CI configuration or a protected path): "
                                               + ", ".join(refused) + ".")
        now, bounds = time.monotonic(), ((f"client:{call.client}", PER_ACCOUNT), ("", PER_HOUR))
        for key, bound in bounds:
            if delay := _delay(key, bound, now):
                raise ext.ToolError("too_many_requests", f"at most {bound} pull requests "
                                    f"per hour; try again in {delay / 60:.0f} min.")
        for key, _ in bounds:
            _HITS[key].append(now)
        who = call.client[:12] or "open proxy"
        footer = (f"\n\n---\nOpened by the llm-proxy `pull_request` extension for "
                  f"account `{who}`: review before merging.")
        on = Forge(cfg, project, transport)
        try:
            async with asyncio.timeout(TIMEOUT):
                base = base or await on.default_branch()
                branch = _branch(title, project, base, files)
                if await on.has_branch(branch):
                    raise ext.ToolError("failed", f"the branch {branch!r} already exists: "
                                                  f"this change was already proposed.")
                url = await getattr(on, on.kind)(base, branch, files, title,
                                                 description + footer)
        except TimeoutError:
            raise ext.ToolError("timeout", f"the forge took more than {TIMEOUT} s.")
        log.info("pull request ouverte : %s, branche %s vers %s, compte %s — %s",
                 project, branch, base, who, url or "(sans lien)")
        return ext.Result(f"[{project}] Draft pull request opened: "
                          f"{url or '(no link returned)'}\n"
                          f"Branch {branch} into {base}, {len(files)} file(s).",
                          meta={"project": project, "url": url, "branch": branch})


TOOL = PullRequest()
