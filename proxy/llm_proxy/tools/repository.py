"""
`repository` : LIRE un projet d'une forge (GitLab, GitHub, Gitea/Forgejo,
ou tout dépôt git en HTTPS) — son arborescence, un fichier, une recherche
— sans rien exécuter du projet.

Le travail est partagé pour que le contenu d'un dépôt ne soit jamais
décodé dans le proxy :

  le PROXY    choisit la source (storage/repos.py : celles du compte, puis
              les partagées), télécharge l'archive avec son jeton, et la
              garde (compressée) en cache. Il ne l'ouvre pas.
  un BAC      de l'exécuteur de code (le conteneur `executor` de la pile,
              par sa route /v1/execute, comme code_execution — l'outil
              code_execution lui-même peut rester inactif), un par compte
              (session « repository »), sans réseau : il reçoit l'archive,
              la décompresse et répond aux commandes, par le script
              repository_bac.py.

  tree   l'arborescence (sous `path`), tailles comprises
  read   un fichier texte, lignes numérotées (`offset`, `limit`)
  grep   une expression régulière dans les fichiers texte, sous `path`,
         filtrés par `include`

Le modèle ne choisit qu'une source (par son nom), un projet et une
référence ; le jeton ne quitte jamais le proxy, ni vers le bac ni vers le
modèle. Aucune règle d'adresse : une source peut viser une forge du réseau
interne (choix du déploiement, docs/depots.md) — seules les listes de REFUS
de [tools.net] valent.

  gitlab   GET <url>/api/v4/projects/<projet>/repository/archive.tar.gz
  github   GET <api>/repos/<projet>/tarball[/<ref>] (api.github.com, ou
           <url>/api/v3 pour GitHub Enterprise)
  gitea    GET <url>/api/v1/repos/<projet>/archive/<ref>.tar.gz (Forgejo
           aussi) ; sans ref, la branche par défaut lue avant
  git      `git fetch --depth 1` de <url>/<projet>.git puis `git archive`,
           sous `prlimit` (le pack borné), jeton en Authorization Basic,
           redirections refusées. git lit le pack DANS le proxy : seule
           exception à « rien n'est décodé ici » (docs/depots.md).

Les forges passent par forge.get (net.download) : adresse résolue et
épinglée, corps borné, et le jeton envoyé au seul saut qui reste sur
l'origine de la source (jamais à l'hôte d'une redirection).

CE QUI EST GARDÉ, et combien de temps ([repos].cache_ttl, 30 min) :
l'archive dans le cache du proxy, 30 min après sa DERNIÈRE lecture
(purgée activement, webcache.sweep) ; le projet décompressé dans le bac
« repository » du compte, détruit par l'exécuteur après la même
inactivité (`idle` demandé à chaque appel). Un bac neuf n'a rien : le
script le dit, et le proxy redépose l'archive de son cache, ou la
retélécharge. forget(compte) efface les deux (source supprimée ou
modifiée, bouton « Oublier »). Incognito (et donc le passe plat des
webhooks) : un bac d'un seul appel, détruit après lui, et rien dans le
cache. La clé : compte, source (adresse, forge, utilisateur, condensé du
jeton), projet, référence. Deux commandes simultanées sur le même projet
attendent le même téléchargement.
"""

import asyncio
import base64
import hashlib
import json
import os
import signal
import tempfile
from pathlib import Path
from urllib.parse import quote

from ..core import config
from ..core.settings import INCOGNITO
from ..storage import repos
from . import code_execution, forge, net
from .contract import Call, Result, Tool, ToolError, ToolName
from .webcache import Cache

ENABLED = config.flag("repos.enabled", False)
NAME = ToolName.REPOSITORY
# Octets au plus d'une archive (compressée) : c'est aussi un fichier
# d'entrée du bac — SANDBOX_MAX_INPUT_BYTES de l'exécuteur au moins aussi haut.
MAX_BYTES = config.integer("repos.max_bytes", 20_000_000)
# Octets au plus d'un projet décompressé dans le bac (qui doit tenir dans
# son /work, SANDBOX_WORK_SIZE).
MAX_UNPACKED = config.integer("repos.max_unpacked_bytes", 100_000_000)
# Délai (s) du téléchargement entier (ou du fetch git).
TIMEOUT = forge.TIMEOUT
# 30 min après la DERNIÈRE lecture (sliding), purgé activement
# (webcache.sweep) ; le bac des dépôts reçoit la même durée d'inactivité.
CACHE = Cache(config.num("repos.cache_ttl", 1800), 32,
              config.integer("repos.cache_bytes", 200_000_000), sliding=True)
# Un fichier plus gros n'est pas gardé (listé, mais ni lu ni cherché).
MAX_FILE_BYTES = 2_000_000
# Entrées au plus d'une archive (fichiers, dossiers, liens).
MAX_ENTRIES = 100_000
# Dépôts gardés au plus dans le bac d'un compte (les plus récents).
KEEP = 2
READ_LINES = 400
MAX_PATTERN = 500
SESSION = "repository"
COMMANDS = ("tree", "read", "grep")
# Le script du bac, lu une fois (un texte : il n'est pas importé ici).
SCRIPT = Path(__file__).with_name("repository_bac.py").read_text(encoding="utf-8")


# ── le téléchargement ───────────────────────────────────────────────────

async def _archive(source: dict, project: str, ref: str, transport) -> bytes:
    base = forge.api(source)
    if source["forge"] == "gitlab":
        url = f"{base}{forge.project_path(source, project)}/repository/archive.tar.gz"
        if ref:
            url += "?sha=" + quote(ref, safe="")
        return await forge.get(source, url, MAX_BYTES, NAME, transport)
    if source["forge"] == "github":
        url = f"{base}/repos/{project}/tarball" + (f"/{quote(ref, safe='/')}" if ref else "")
        return await forge.get(source, url, MAX_BYTES, NAME, transport,
                               "application/vnd.github+json")
    # gitea / forgejo : l'archive demande une référence
    ref = ref or await forge.default_branch(source, project, NAME, transport)
    return await forge.get(source, f"{base}/repos/{project}/archive/"
                                   f"{quote(ref, safe='/')}.tar.gz", MAX_BYTES, NAME, transport)


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
                       f"the archive is larger than {limit} bytes, the limit of this "
                       f"proxy.")]
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
    `git archive` : une archive tar.gz, comme celle d'une forge, sans
    checkout sur le disque."""
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
        return bytes(await _git_run(["-C", tmp, "archive", "--format=tar.gz", "--prefix=src/",
                                     "FETCH_HEAD"], env, hidden, MAX_BYTES))


async def fetch(source: dict, project: str, ref: str, transport=None) -> bytes:
    """L'archive tar.gz du projet (UN dossier racine), non ouverte. Lève
    ToolError."""
    try:
        async with asyncio.timeout(TIMEOUT):
            if source["forge"] == "git":
                return await _git(source, project, ref)
            return await _archive(source, project, ref, transport)
    except TimeoutError:
        raise ToolError("timeout", f"the download took more than {TIMEOUT:g} s.")


# ── le bac ──────────────────────────────────────────────────────────────

def _code(args: dict) -> str:
    """Le script du bac, ses arguments en tête (un littéral Python : repr
    d'un texte JSON)."""
    return f"ARGS = {json.dumps(args)!r}\n" + SCRIPT


async def _bac(client: str, args: dict, archive: bytes | None, transport=None,
               session: str = SESSION) -> str | None:
    """Une commande dans le bac du compte → son texte ; None : le dépôt n'y
    est pas (bac neuf), à redéposer. `session` «» : un bac d'un seul appel,
    détruit par l'exécuteur après lui (incognito). Lève ToolError."""
    args = {**args, "max_unpacked": MAX_UNPACKED, "max_file": MAX_FILE_BYTES,
            "max_entries": MAX_ENTRIES, "keep": KEEP}
    body = {"client": client, "session": session, "language": "python",
            "timeout": code_execution.TIMEOUT,
            # Le bac des dépôts vit aussi longtemps que le cache, pas les
            # 3 h d'un bac de code (SANDBOX_IDLE).
            "idle": CACHE.ttl}
    if archive is not None:
        name = f"repo-{args['dir']}.tar.gz"
        args["archive"] = name
        body["inputs"] = [{"name": name, "data": base64.b64encode(archive).decode("ascii")}]
    body["code"] = _code(args)
    r = await code_execution.execute(
        body, code_execution.TIMEOUT + code_execution.MARGIN + code_execution.INPUT_MARGIN,
        transport)
    if r.status_code == 413:
        raise ToolError("unsupported", "the archive is larger than the sandbox accepts "
                                       "(SANDBOX_MAX_INPUT_BYTES).")
    if r.status_code == 429:
        raise ToolError("too_many_requests", "every sandbox is busy; try again later.")
    if r.status_code != 200:
        raise ToolError("unavailable", f"the sandbox service failed (HTTP {r.status_code}).")
    doc = r.json()
    if doc.get("rejected"):
        raise ToolError("unsupported", "the sandbox refused the archive ("
                        + str(doc["rejected"][0].get("reason", "?")) + ").")
    out = (doc.get("output") or "").rstrip("\n")
    if doc.get("timed_out"):
        raise ToolError("timeout", "the sandbox took too long on this project.")
    if doc.get("exit_code") == 3:
        return None
    if doc.get("exit_code") == 2:
        code, _, message = out.partition(": ")
        raise ToolError(code if code in ("invalid_input", "not_accessible", "unsupported",
                                         "failed") else "failed", message or out)
    if doc.get("exit_code") != 0:
        raise ToolError("failed", "the sandbox failed: " + out[-300:])
    return out


# ── l'outil ─────────────────────────────────────────────────────────────

# Un téléchargement en cours par clé : [verrou, commandes qui l'attendent].
_loading: dict[tuple, list] = {}


class RepositoryTool(Tool):
    name = NAME
    max_calls = 30
    timeout = TIMEOUT + 2 * (code_execution.TIMEOUT + code_execution.MARGIN
                             + code_execution.INPUT_MARGIN)

    @property
    def enabled(self) -> bool:
        # Les dépôts sont lus dans un bac de l'exécuteur : il doit être
        # configuré (l'outil code_execution peut, lui, rester inactif).
        return ENABLED and bool(code_execution.URL and code_execution.TOKEN)

    def prompt(self, present) -> str:
        return (
            "Read a git repository the user configured access to (GitLab, GitHub, "
            "Gitea or another git server): its file tree, a file, or a regex search "
            "across its files. Nothing of the project is executed. Use it when the "
            "task needs the actual source of a project (where a dependency is "
            "pinned, how something is implemented, a config file). Commands: tree "
            "(optional `path` to look inside a folder, `depth`, default 2), read "
            "(`path` of a file; `offset` and `limit` in lines for long files), grep "
            "(`pattern`, a Python regular expression, case-insensitive; optional "
            "`path` folder and `include` file-name glob such as *.py or "
            "requirements*.txt). `project` is the path on the forge "
            "(group/subgroup/project, owner/repo). `source` names a configured "
            "source, omitted when there is only one; an unknown name returns the "
            "available ones. `ref`: branch, tag or commit; omit for the default "
            "branch. The project is downloaded on first use and kept for a while: "
            "later commands are fast.")

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
            "pattern": {"type": "string", "description": "grep: a Python regular expression."},
            "include": {"type": "string", "description": "grep: a file-name glob, e.g. *.py."},
            "depth": {"type": "integer", "description": "tree: levels shown (default 2)."},
            "offset": {"type": "integer", "description": "read: first line (default 1)."},
            "limit": {"type": "integer", "description": f"read: lines (default {READ_LINES})."},
        }, "required": ["command", "project"]}

    def summary(self, args: dict, result: Result | None = None) -> dict:
        return {"type": self.name, "command": str(args.get("command") or ""),
                "project": str(args.get("project") or ""), "path": str(args.get("path") or "")}

    async def run(self, args: dict, call: Call, transport=None, sandbox=None) -> Result:
        """`transport`, `sandbox` : pour les tests — vers la forge, vers
        l'exécuteur."""
        command = args.get("command")
        if command not in COMMANDS:
            raise ToolError("invalid_input", "`command` must be one of " + ", ".join(COMMANDS) + ".")
        project = str(args.get("project") or "").strip().strip("/")
        if not forge.valid_project(project):
            raise ToolError("invalid_input", "`project` must be a path like group/project.")
        ref = str(args.get("ref") or "").strip()
        if ref and not forge.valid_ref(ref):
            raise ToolError("invalid_input", "`ref` must be a branch, tag or commit name.")
        path = str(args.get("path") or "").strip().strip("/")
        todo = {"command": command, "path": path}
        if command == "tree":
            depth = args.get("depth") if isinstance(args.get("depth"), int) else 2
            todo["depth"] = max(1, min(depth, 10))
        elif command == "read":
            if not path:
                raise ToolError("invalid_input", "`path` of the file to read is required.")
            offset = args.get("offset") if isinstance(args.get("offset"), int) else 1
            limit = args.get("limit") if isinstance(args.get("limit"), int) else READ_LINES
            todo.update(offset=offset, limit=max(1, min(limit, 2000)))
        else:
            pattern = args.get("pattern")
            if not isinstance(pattern, str) or not pattern:
                raise ToolError("invalid_input", "`pattern` is required.")
            if len(pattern) > MAX_PATTERN:
                raise ToolError("invalid_input", f"`pattern` is longer than {MAX_PATTERN} characters.")
            todo.update(pattern=pattern, include=str(args.get("include") or "").strip())
        source = await forge.source(call.client, str(args.get("source") or "").strip(), project)
        key = _key(call.client, source, project, ref)
        todo["dir"] = hashlib.sha256(repr(key).encode()).hexdigest()[:24]
        # Incognito, rien ne survit à la réponse : un bac d'un seul appel
        # (session «»), détruit après lui, l'archive redéposée à chaque
        # commande (le cache ne la garde pas). Sinon, le bac « repository »
        # du compte, et l'archive seulement s'il ne l'a pas encore.
        session = "" if INCOGNITO.get() else SESSION
        generation = _generation.get(call.client, 0)
        text = None if not session else await _bac(call.client, todo, None, sandbox)
        if text is None:
            archive = await _archive_for(key, call.client, source, project, ref, transport)
            if session and _generation.get(call.client, 0) != generation:
                # Effacé pendant ce temps : le projet ne retourne pas dans le bac.
                raise ToolError("failed", "the downloaded repositories were just erased by "
                                          "the user; ask before reading again.")
            text = await _bac(call.client, todo, archive, sandbox, session)
            if text is None:
                raise ToolError("failed", "the sandbox lost the project; try again.")
        where = f"{source['name']}:{project}" + (f"@{ref}" if ref else "")
        return Result(f"[{where}]\n{text}", meta={"source": source["name"], "project": project})


# Une génération par compte, avancée par forget : une commande commencée
# avant l'effacement ne remet ni l'archive dans le cache, ni le projet dans
# le bac (_archive_for, run).
_generation: dict[str, int] = {}


async def forget(owner: str, transport=None) -> bool:
    """Tout ce que le compte a téléchargé, effacé : ses archives du cache et
    son bac des dépôts (détruit par l'exécuteur, ses fichiers avec lui). Une
    commande déjà en route n'y remet rien (_generation) ; la suivante
    retélécharge. Appelé à la suppression ou à la modification d'une
    source, et par le bouton « Oublier » de la page. → faux si l'exécuteur
    n'a pas pu être joint : son bac expirera seul ([repos].cache_ttl)."""
    _generation[owner] = _generation.get(owner, 0) + 1
    CACHE.forget(lambda key: key[0] != owner)
    if code_execution.URL and code_execution.TOKEN:
        return await code_execution.destroy(owner, SESSION, transport) is not None
    return True


def _key(owner: str, source: dict, project: str, ref: str) -> tuple:
    """Ce qui fait un projet lu : le compte (un projet lu avec le jeton d'un
    compte n'est pas servi à un autre) et la source (adresse, forge,
    utilisateur, condensé du jeton : une source modifiée ne sert pas
    l'ancien contenu), le projet tel qu'écrit (une forge git peut distinguer
    la casse), la référence."""
    token = hashlib.sha256((source.get("token") or "").encode()).hexdigest()[:16]
    return (owner, source["id"], source["url"], source["forge"], source.get("username") or "",
            token, project, ref)


async def _archive_for(key: tuple, owner: str, source: dict, project: str, ref: str,
                       transport) -> bytes:
    """L'archive du projet, du cache ou téléchargée (une fois pour deux
    commandes simultanées). Gardée en cache seulement si le compte n'a pas
    tout effacé (forget) pendant le téléchargement."""
    generation = _generation.get(owner, 0)
    entry = _loading.setdefault(key, [asyncio.Lock(), 0])
    entry[1] += 1
    try:
        async with entry[0]:
            data = CACHE.get(key)
            if data is not None:
                return data
            try:
                data = await fetch(source, project, ref, transport)
            except ToolError as exc:
                if not source["shared"]:
                    await asyncio.to_thread(repos.used, owner, source["id"], exc.message)
                raise
            # Une annulation (l'utilisateur arrête la réponse) n'est pas
            # une erreur de la source : rien n'est noté.
            if not source["shared"]:
                await asyncio.to_thread(repos.used, owner, source["id"])
            if _generation.get(owner, 0) == generation:
                CACHE.put(key, data, len(data))
            return data
    finally:
        entry[1] -= 1
        if not entry[1]:
            _loading.pop(key, None)


TOOL = RepositoryTool()
