"""
`repository` : LIRE un projet d'une forge (GitLab, GitHub, Gitea/Forgejo,
ou tout dépôt git en HTTPS) — son arborescence, un fichier, une recherche
— sans rien exécuter. Le proxy télécharge le projet avec le jeton d'une
source (storage/repos.py : celles du compte, puis les partagées de
l'admin), le garde en mémoire un moment, et répond aux commandes :

  tree   l'arborescence (sous `path`), tailles comprises
  read   un fichier texte, lignes numérotées (`offset`, `limit`)
  grep   une expression régulière (tools/pattern.py : bornée en durée)
         dans les fichiers texte, sous `path`, filtrés par `include`

Le modèle ne choisit qu'une source (par son nom), un projet et une
référence ; le jeton ne quitte jamais le proxy. Aucune règle d'adresse :
une source peut viser une forge du réseau interne (choix du déploiement,
docs/depots.md) — seules les listes de REFUS de [tools.net] valent.

  gitlab   GET <url>/api/v4/projects/<projet>/repository/archive.tar.gz
  github   GET <api>/repos/<projet>/tarball[/<ref>] (api.github.com, ou
           <url>/api/v3 pour GitHub Enterprise)
  gitea    GET <url>/api/v1/repos/<projet>/archive/<ref>.tar.gz (Forgejo
           aussi) ; sans ref, la branche par défaut lue avant
  git      `git fetch --depth 1` de <url>/<projet>.git puis `git archive`,
           sous `prlimit` (le pack borné), jeton en Authorization Basic,
           redirections refusées, listes de refus de [tools.net] appliquées

Les forges passent par net.download : adresse résolue et épinglée, corps
borné, et le jeton envoyé au seul saut qui reste sur l'origine de la
source (jamais à l'hôte d'une redirection). Le résultat est toujours une
archive tar, lue par _unpack avec les mêmes bornes.

Le projet est gardé (CACHE), par compte, source, adresse, forge, jeton,
projet et référence, [repos].cache_ttl secondes : les commandes suivantes
ne le retéléchargent pas, et une source modifiée ne sert pas l'ancien.
Deux commandes simultanées sur le même projet attendent le même
téléchargement. Une conversation incognito ne garde rien (webcache.Cache) :
chaque commande retélécharge.
"""

import asyncio
import base64
import fnmatch
import hashlib
import io
import json
import os
import re
import signal
import tarfile
import tempfile
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

from ..core import config
from ..storage import repos
from . import net
from . import pattern as patterns
from .contract import Call, Result, Tool, ToolError, ToolName
from .webcache import Cache

ENABLED = config.flag("repos.enabled", False)
NAME = ToolName.REPOSITORY
# Octets au plus d'une archive téléchargée (compressée), et d'un projet
# décompressé, gardé en mémoire.
MAX_BYTES = config.integer("repos.max_bytes", 50_000_000)
MAX_UNPACKED = config.integer("repos.max_unpacked_bytes", 200_000_000)
# Délai (s) du téléchargement entier (ou du fetch git).
TIMEOUT = config.num("repos.timeout", 120)
CACHE = Cache(config.num("repos.cache_ttl", 1800), 32,
              config.integer("repos.cache_bytes", 400_000_000))
# Un fichier plus gros n'est pas gardé (listé, mais ni lu ni cherché).
MAX_FILE_BYTES = 2_000_000
# Entrées au plus d'une archive (fichiers, dossiers, liens).
MAX_ENTRIES = 100_000
READ_LINES = 400
MAX_READ_CHARS = 60_000
MAX_LINE_CHARS = 2000
TREE_LINES = 400
GREP_MATCHES = 200
# Délai (s) de TOUTE une recherche, fichiers compris.
GREP_BUDGET = 10.0
USER_AGENT = "llm-proxy repository (+https://github.com/c4software/llm-proxy)"
COMMANDS = ("tree", "read", "grep")
# Un projet : « groupe/sous-groupe/projet », « propriétaire/dépôt ».
PROJECT = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_.\-]*(/[A-Za-z0-9_.][A-Za-z0-9_.\-]*)*\Z")
_REF = re.compile(r"[A-Za-z0-9_.\-/]{1,200}\Z")


def valid_project(project: str) -> bool:
    return bool(PROJECT.match(project)) and not {".", ".."} & set(project.split("/"))


def valid_ref(ref) -> bool:
    return isinstance(ref, str) and bool(_REF.match(ref)) and ".." not in ref \
        and not ref.startswith("-")


# ── le téléchargement ───────────────────────────────────────────────────

def _bounded(limit: int):
    """La borne de net.download : laisse passer une redirection, dit un
    statut refusé avec les mots de la forge, et refuse une archive trop
    grosse au lieu de la couper."""
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
            raise ToolError("unsupported", f"the archive is larger than {limit} bytes, "
                                           f"the limit of this proxy.")
        return limit + 1
    return bound


async def _download(url: str, auth: dict, limit: int = 0, transport=None,
                    accept: str = "*/*") -> bytes:
    _, _, body = await net.download(
        url, timeout=TIMEOUT, limit=_bounded(limit or MAX_BYTES), user_agent=USER_AGENT,
        accept=accept, transport=transport, auth=auth, allow_private=True, domains=False)
    return body


async def _archive(source: dict, project: str, ref: str, transport) -> bytes:
    forge, base, token = source["forge"], source["url"].rstrip("/"), source.get("token") or ""
    if forge == "gitlab":
        url = f"{base}/api/v4/projects/{quote(project, safe='')}/repository/archive.tar.gz"
        if ref:
            url += "?sha=" + quote(ref, safe="")
        return await _download(url, {"PRIVATE-TOKEN": token} if token else {}, 0, transport)
    if forge == "github":
        host = urlsplit(base).hostname or ""
        api = "https://api.github.com" if host in ("github.com", "api.github.com") \
            else f"{base}/api/v3"
        auth = {"Authorization": f"Bearer {token}"} if token else {}
        url = f"{api}/repos/{project}/tarball" + (f"/{quote(ref, safe='/')}" if ref else "")
        return await _download(url, auth, 0, transport, "application/vnd.github+json")
    # gitea / forgejo
    auth = {"Authorization": f"token {token}"} if token else {}
    if not ref:
        raw = await _download(f"{base}/api/v1/repos/{project}", auth, 1_000_000, transport)
        try:
            ref = json.loads(raw)["default_branch"]
        except (ValueError, KeyError, TypeError):
            ref = None
        if not valid_ref(ref):
            raise ToolError("not_accessible", "could not read the default branch; pass `ref`.")
    return await _download(f"{base}/api/v1/repos/{project}/archive/"
                           f"{quote(ref, safe='/')}.tar.gz", auth, 0, transport)


def _git_env(source: dict) -> tuple[dict, list[str]]:
    """L'environnement de git : MINIMAL (ni GIT_TRACE, ni configuration de
    l'hôte, ni HTTP(S)_PROXY : comme net, qui ne passe pas par un proxy
    d'environnement), le jeton en en-tête par la configuration
    d'environnement — jamais sur la ligne de commande (visible de `ps`) ni
    dans l'URL —, les redirections refusées (le jeton ne les suit pas).
    → (env, secrets à masquer dans un message d'erreur)."""
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "SSL_CERT_FILE", "SSL_CERT_DIR")}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true", "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null", "HOME": "/nonexistent"})
    # fetch.unpackLimit 1 : toujours un pack (un seul fichier, borné par
    # prlimit), jamais des objets libres — sous 100 objets, git les écrirait
    # un par un, chacun sous la borne et leur somme sans elle.
    pairs = [("http.followRedirects", "false"), ("fetch.unpackLimit", "1")]
    hidden = []
    token = source.get("token") or ""
    if token:
        basic = base64.b64encode(f"{source.get('username') or 'git'}:{token}".encode()).decode()
        pairs.append(("http.extraHeader", f"Authorization: Basic {basic}"))
        hidden = [token, basic]
    env["GIT_CONFIG_COUNT"] = str(len(pairs))
    for i, (k, v) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"] = k, v
    return env, hidden


async def _git_run(args: list[str], env: dict, hidden: list[str],
                   limit: int = 0) -> bytearray:
    """Une commande git ; sa sortie (bornée à `limit` octets, 0 = ignorée),
    lue en même temps que son erreur (bornée aussi). Sous `prlimit` : un
    fichier qu'elle écrit (le pack du fetch) ne dépasse pas MAX_BYTES. Tuée
    avec ses enfants au délai, à l'annulation ou au dépassement."""
    proc = await asyncio.create_subprocess_exec(
        "prlimit", f"--fsize={MAX_BYTES}", "git", *args, env=env, start_new_session=True,
        stdin=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE if limit else asyncio.subprocess.DEVNULL)
    out, err = bytearray(), bytearray()

    async def collect(stream, into: bytearray, cap: int, too_big: str):
        while chunk := await stream.read(1 << 16):
            if len(into) + len(chunk) > cap:
                if too_big:
                    raise ToolError("unsupported", too_big)
                continue                          # l'erreur : la fin est jetée
            into += chunk

    try:
        await asyncio.gather(
            collect(proc.stderr, err, 64_000, ""),
            *([collect(proc.stdout, out, limit,
                       f"the project is larger than {limit} bytes once unpacked.")]
              if limit else []))
        await proc.wait()
    finally:
        if proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
    if proc.returncode != 0:
        lines = err.decode("utf-8", "replace").strip().splitlines()
        detail = next((x for x in lines if x.startswith("fatal:")),
                      lines[-1] if lines else f"exit code {proc.returncode}")[:300]
        for s in hidden:
            detail = detail.replace(s, "***")
        raise ToolError("not_accessible", f"git failed: {detail}")
    return out


async def _git(source: dict, project: str, ref: str) -> bytes:
    """`git fetch --depth 1` de la référence (un commit aussi), puis
    `git archive` : une archive tar, comme celle d'une forge, sans checkout
    sur le disque."""
    url = source["url"].rstrip("/") + f"/{project}" + ("" if project.endswith(".git") else ".git")
    net.refused(url)                              # les listes de refus, comme les forges
    env, hidden = _git_env(source)
    with tempfile.TemporaryDirectory(prefix="repo-", ignore_cleanup_errors=True) as tmp:
        try:
            await _git_run(["init", "-q", "--bare", tmp], env, hidden)
        except FileNotFoundError:
            raise ToolError("unavailable", "git (or prlimit) is not installed on this proxy.")
        try:
            await _git_run(["-C", tmp, "fetch", "--depth", "1", "--no-tags", "-q", "--", url,
                            ref or "HEAD"], env, hidden)
        except ToolError:
            # Le pack arrêté à MAX_BYTES (prlimit) fait échouer index-pack
            # avec un message obscur : la vraie raison est la taille.
            if any(f.stat().st_size >= MAX_BYTES
                   for f in Path(tmp, "objects", "pack").glob("*") if f.is_file()):
                raise ToolError("unsupported", f"the repository is larger than "
                                               f"{MAX_BYTES} bytes.")
            raise
        return await _git_run(["-C", tmp, "archive", "--format=tar", "--prefix=src/",
                               "FETCH_HEAD"], env, hidden, MAX_UNPACKED + MAX_UNPACKED // 10)


def _unpack(data: bytes) -> dict[str, bytes | None]:
    """Une archive (UN dossier racine) → ses fichiers réguliers, chemins sans
    ce dossier ; None pour un fichier trop gros (listé, non gardé). Liens,
    chemins absolus ou en `..` : ignorés."""
    files: dict[str, bytes | None] = {}
    total = entries = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
            for m in tar:
                entries += 1
                if entries > MAX_ENTRIES:
                    raise ToolError("unsupported", f"the project has more than "
                                                   f"{MAX_ENTRIES} entries.")
                # Tout membre compte, gardé ou non : il est décompressé pour
                # être sauté.
                total += m.size
                if total > MAX_UNPACKED:
                    raise ToolError("unsupported", f"the project is larger than "
                                                   f"{MAX_UNPACKED} bytes once unpacked.")
                parts = m.name.split("/")[1:]
                if not m.isfile() or not parts or any(p in ("", ".", "..") for p in parts):
                    continue
                path = "/".join(parts)
                if m.size > MAX_FILE_BYTES:
                    files[path] = None
                else:
                    f = tar.extractfile(m)
                    files[path] = f.read() if f else b""
    except (tarfile.TarError, EOFError, OSError):
        raise ToolError("unsupported", "the forge returned an unreadable archive.")
    return files


async def fetch(source: dict, project: str, ref: str, transport=None) -> dict[str, bytes | None]:
    """Le projet → {chemin: octets, ou None}. Lève ToolError."""
    try:
        async with asyncio.timeout(TIMEOUT):
            data = await (_git(source, project, ref) if source["forge"] == "git"
                          else _archive(source, project, ref, transport))
    except TimeoutError:
        raise ToolError("timeout", f"the download took more than {TIMEOUT:g} s.")
    return await asyncio.to_thread(_unpack, data)


# ── les commandes ───────────────────────────────────────────────────────

def _text(data: bytes | None) -> str | None:
    """Le texte d'un fichier, None s'il est binaire (ou trop gros)."""
    if data is None or b"\0" in data[:8000]:
        return None
    return data.decode("utf-8", "replace")


def _under(path: str, folder: str) -> bool:
    return not folder or path == folder or path.startswith(folder + "/")


def _size(n: int | None) -> str:
    if n is None:
        return f">{MAX_FILE_BYTES // 1_000_000} MB, not kept"
    return f"{n} B" if n < 1000 else f"{n / 1000:.1f} kB" if n < 1_000_000 else f"{n / 1e6:.1f} MB"


def tree(files: dict, folder: str, depth: int) -> str:
    entries = sorted(p for p in files if _under(p, folder))
    if not entries:
        raise ToolError("not_accessible", f"no file under {folder!r}.")
    start = len(folder.split("/")) if folder else 0
    out, counts = [], {}
    for p in entries:
        parts = p.split("/")
        if len(parts) - start > depth:
            d = "/".join(parts[:start + depth])
            if d not in counts:
                counts[d] = 0
                out.append(d)
            counts[d] += 1
        else:
            data = files[p]
            out.append(f"{p}  ({_size(None if data is None else len(data))})")
    out = [f"{x}/  ({counts[x]} files)" if x in counts else x for x in out]
    more = len(out) - TREE_LINES
    return f"{len(entries)} files\n" + "\n".join(out[:TREE_LINES]) + (
        f"\n[… {more} more entries; narrow with `path` or `depth`]" if more > 0 else "")


def read(files: dict, path: str, offset: int, limit: int) -> str:
    if path not in files:
        name = path.split("/")[-1]
        near = [p for p in files if p.split("/")[-1] == name]
        raise ToolError("not_accessible", f"no file {path!r}" + (
            f"; did you mean: {', '.join(near[:5])}?" if near else "; use the tree command."))
    text = _text(files[path])
    if text is None:
        raise ToolError("unsupported", f"{path} is binary or larger than "
                                       f"{MAX_FILE_BYTES} bytes.")
    # Découpé sur \n seul, comme grep compte ses lignes ; sans la ligne vide
    # qui suit le dernier saut.
    lines = [line.rstrip("\r") for line in text.split("\n")]
    if lines and lines[-1] == "" and text.endswith("\n"):
        lines.pop()
    first = max(1, offset)
    if lines and first > len(lines):
        raise ToolError("invalid_input", f"{path} has {len(lines)} lines; `offset` is past "
                                         f"its end.")
    body, used = [], 0
    for i, line in enumerate(lines[first - 1:first - 1 + limit], first):
        if len(line) > MAX_LINE_CHARS:            # JS minifié, lockfile : coupé
            line = line[:MAX_LINE_CHARS] + f" [… {len(line) - MAX_LINE_CHARS} characters]"
        row = f"{i:>5}\t{line}"
        if body and used + len(row) > MAX_READ_CHARS:
            break                                  # au moins une ligne : on avance toujours
        used += len(row) + 1
        body.append(row)
    last = first + len(body) - 1
    tail = f"\n[lines {first}-{last} of {len(lines)}; continue with offset={last + 1}]" \
        if last < len(lines) else ""
    return f"{path} ({len(lines)} lines)\n" + "\n".join(body) + tail


def grep(files: dict, pattern, folder: str, include: str) -> str:
    """Les lignes qui répondent au motif compilé (patterns.compile), une
    fois chacune, coupées autour de la correspondance. Chaque recherche est
    bornée par patterns.TIMEOUT : un fichier où elle déborde est compté
    comme non cherché ; l'ensemble est borné par GREP_BUDGET (TimeoutError)."""
    deadline = time.monotonic() + GREP_BUDGET
    found, skipped, slow = [], 0, 0
    for p in sorted(files):
        if not _under(p, folder) or (include and not _glob(p, include)):
            continue
        text = _text(files[p])
        if text is None:
            skipped += 1
            continue
        pos, number = 0, 1
        while pos <= len(text):
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError
            try:
                m = pattern.search(text, pos, timeout=min(patterns.TIMEOUT, left),
                                   concurrent=True)
            except TimeoutError:
                slow += 1
                break
            if m is None or (m.start() == len(text) and text.endswith("\n")):
                break                             # pas de ligne après le dernier saut
            begin = text.rfind("\n", 0, m.start()) + 1
            end = text.find("\n", m.start())
            end = len(text) if end < 0 else end
            number += text.count("\n", pos, begin)
            line = text[begin:end].rstrip("\r")
            found.append(f"{p}:{number}: "
                         f"{patterns.clip(line, m.start() - begin, m.end() - begin).strip()}")
            if len(found) >= GREP_MATCHES:
                return "\n".join(found) + (f"\n[stopped at {GREP_MATCHES} matches; "
                                           f"narrow with `path` or `include`]")
            # La ligne suivante : une ligne n'est rendue qu'une fois.
            pos = end + 1
            number += 1
    too_slow = f"{slow} files where the pattern was too slow, not searched" if slow else ""
    if not found:
        notes = ([f"{skipped} binary or large files not searched"] if skipped else []) \
            + ([too_slow] if too_slow else [])
        return "No match." + (f" ({'; '.join(notes)}.)" if notes else "")
    return "\n".join(found) + (f"\n[{too_slow}]" if too_slow else "")


def _glob(path: str, include: str) -> bool:
    return fnmatch.fnmatch(path.split("/")[-1], include) or fnmatch.fnmatch(path, include)


# ── l'outil ─────────────────────────────────────────────────────────────

# Un téléchargement en cours par clé : [verrou, commandes qui l'attendent].
_loading: dict[tuple, list] = {}


class RepositoryTool(Tool):
    name = NAME
    max_calls = 30
    timeout = TIMEOUT + GREP_BUDGET + 15

    @property
    def enabled(self) -> bool:
        return ENABLED

    def prompt(self, present) -> str:
        return (
            "Read a git repository the user configured access to (GitLab, GitHub, "
            "Gitea or another git server): its file tree, a file, or a regex search "
            "across its files. Nothing is executed. Use it when the task needs the "
            "actual source of a project (where a dependency is pinned, how something "
            "is implemented, a config file). Commands: tree (optional `path` to look "
            "inside a folder, `depth`, default 2), read (`path` of a file; `offset` "
            "and `limit` in lines for long files), grep (`pattern`, a regular "
            "expression, case-insensitive; optional `path` folder and `include` "
            "file-name glob such as *.py or requirements*.txt). `project` is the path "
            "on the forge (group/subgroup/project, owner/repo). `source` names a "
            "configured source, omitted when there is only one; an unknown name "
            "returns the available ones. `ref`: branch, tag or commit; omit for the "
            "default branch. The project is downloaded on first use and kept for a "
            "while: later commands are fast.")

    def parameters(self, present) -> dict:
        return {"type": "object", "properties": {
            "command": {"type": "string", "enum": list(COMMANDS)},
            "project": {"type": "string", "description": (
                "Path of the project on the forge: group/subgroup/project, owner/repo.")},
            "source": {"type": "string", "description": (
                "Name of the configured source; optional when there is only one.")},
            "ref": {"type": "string", "description": "Branch, tag or commit; omit for the default branch."},
            "path": {"type": "string", "description": (
                "read: the file; tree and grep: a folder to limit to (optional).")},
            "pattern": {"type": "string", "description": "grep: a regular expression."},
            "include": {"type": "string", "description": "grep: a file-name glob, e.g. *.py."},
            "depth": {"type": "integer", "description": "tree: levels shown (default 2)."},
            "offset": {"type": "integer", "description": "read: first line (default 1)."},
            "limit": {"type": "integer", "description": f"read: lines (default {READ_LINES})."},
        }, "required": ["command", "project"]}

    def summary(self, args: dict, result: Result | None = None) -> dict:
        return {"type": self.name, "command": str(args.get("command") or ""),
                "project": str(args.get("project") or ""), "path": str(args.get("path") or "")}

    async def run(self, args: dict, call: Call, transport=None) -> Result:
        command = args.get("command")
        if command not in COMMANDS:
            raise ToolError("invalid_input", "`command` must be one of " + ", ".join(COMMANDS) + ".")
        project = str(args.get("project") or "").strip().strip("/")
        if not valid_project(project):
            raise ToolError("invalid_input", "`project` must be a path like group/project.")
        ref = str(args.get("ref") or "").strip()
        if ref and not valid_ref(ref):
            raise ToolError("invalid_input", "`ref` must be a branch, tag or commit name.")
        # Le motif d'abord : une erreur du modèle ne coûte pas un téléchargement.
        pattern = patterns.compile(args.get("pattern")) if command == "grep" else None
        source = await _source(call.client, str(args.get("source") or "").strip(), project)
        files = await _files(call.client, source, project, ref, transport)
        path = str(args.get("path") or "").strip().strip("/")
        if command == "tree":
            depth = args.get("depth") if isinstance(args.get("depth"), int) else 2
            text = tree(files, path, max(1, min(depth, 10)))
        elif command == "read":
            if not path:
                raise ToolError("invalid_input", "`path` of the file to read is required.")
            offset = args.get("offset") if isinstance(args.get("offset"), int) else 1
            limit = args.get("limit") if isinstance(args.get("limit"), int) else READ_LINES
            text = read(files, path, offset, max(1, min(limit, 2000)))
        else:
            try:
                text = await asyncio.to_thread(grep, files, pattern, path,
                                               str(args.get("include") or "").strip())
            except TimeoutError:
                raise ToolError("failed", "the pattern took too long on this project; use a "
                                          "simpler expression, or narrow with `path` or "
                                          "`include`.")
        where = f"{source['name']}:{project}" + (f"@{ref}" if ref else "")
        return Result(f"[{where}]\n{text}", meta={"source": source["name"], "project": project})


async def _source(owner: str, wanted: str, project: str) -> dict:
    sources = await asyncio.to_thread(repos.available, owner)
    if not sources:
        raise ToolError("unavailable", (
            "no repository source is configured for this user; they can add one in "
            "the dashboard, Configuration → Dépôts."))
    if wanted:
        source = next((s for s in sources if s["name"].casefold() == wanted.casefold()), None)
    else:
        source = sources[0] if len(sources) == 1 else None
    if source is None:
        names = ", ".join(json.dumps(s["name"], ensure_ascii=False) for s in sources)
        raise ToolError("invalid_input", (
            f"unknown source {wanted!r}; " if wanted else
            "several sources exist, name one; ") + f"available: {names}.")
    if not repos.permits(source, project):
        raise ToolError("not_allowed", f"the source {source['name']!r} does not allow "
                                       f"the project {project!r}.")
    return source


async def _files(owner: str, source: dict, project: str, ref: str, transport) -> dict:
    """Le projet, du cache ou téléchargé. La clé tient le compte (un projet
    lu avec le jeton d'un compte n'est pas servi à un autre) et ce qui fait
    la source (adresse, forge, utilisateur, condensé du jeton) : une source
    modifiée ne sert pas l'ancien contenu. Le projet tel qu'écrit : une
    forge git peut distinguer la casse."""
    token = hashlib.sha256((source.get("token") or "").encode()).hexdigest()[:16]
    key = (owner, source["id"], source["url"], source["forge"], source.get("username") or "",
           token, project, ref)
    entry = _loading.setdefault(key, [asyncio.Lock(), 0])
    entry[1] += 1
    try:
        async with entry[0]:
            files = CACHE.get(key)
            if files is not None:
                return files
            try:
                files = await fetch(source, project, ref, transport)
            except ToolError as exc:
                if not source["shared"]:
                    await asyncio.to_thread(repos.downloaded, owner, source["id"], exc.message)
                raise
            # Une annulation (l'utilisateur arrête la réponse) n'est pas
            # une erreur de la source : rien n'est noté.
            if not source["shared"]:
                await asyncio.to_thread(repos.downloaded, owner, source["id"])
            CACHE.put(key, files, sum(len(v) for v in files.values() if v))
            return files
    finally:
        entry[1] -= 1
        if not entry[1]:
            _loading.pop(key, None)


TOOL = RepositoryTool()
