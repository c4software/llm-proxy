"""
`code_execution` : le modèle écrit un programme, le proxy le fait tourner
dans un BAC À SABLE et lui rend le code de sortie, la sortie et la liste
des fichiers produits.

Le proxy n'exécute RIEN lui-même : il appelle en HTTP le service
`executor` du docker-compose (executor/ à la racine du dépôt), le seul à
avoir un moteur de conteneurs — sur un réseau interne, sans sortie, sans
aucun secret ni volume du proxy. Cet outil en est le client, et écrit le
texte que lit le modèle. Entre les deux, un jeton partagé
([tools.code_execution].token).

Un bac PAR CONVERSATION. `call.session` (posé par la surface : la mémoire
des échanges cachés de chat_api) et `call.client` nomment le bac ; il
garde ses FICHIERS d'un appel au suivant — pas ses variables, chaque
appel est un processus neuf. Sans session (appel direct par /v1/tools),
le bac ne vit que le temps de l'appel. Un bac expire (inactivité, durée
de vie, place à faire) : le suivant est neuf, et le texte le dit au
modèle, qui sinon chercherait ses fichiers.

Les fichiers que le programme écrit dans son dossier de travail
reviennent dans `Result.files` : la surface les range (files.py) et
ajoute leurs liens à la réponse. Le modèle n'en reçoit que les NOMS — pas
d'URL à recopier de travers, ni de jeton dans la mémoire. Un fichier que
le magasin ne garderait pas (trop gros, pas d'adresse publique) n'est pas
annoncé comme rendu.

Un programme qui sort en erreur, ou que son délai tue, n'est PAS une
erreur de l'outil : c'est un résultat, que le modèle lit et corrige. Les
codes d'erreur ne disent que les pannes d'ICI : arguments inutilisables
(`invalid_input`), exécuteur non configuré, injoignable ou en panne
(`unavailable`), tous les bacs occupés (`too_many_requests`).

Sans liaison à l'API Responses ni à l'API Messages : l'outil se déclare
par {"type": "code_execution"} sur /v1/chat/completions (ou
[chat].always) et s'appelle par POST /v1/tools/code_execution.
"""

import base64
import binascii
import mimetypes

import httpx

from .. import config, files
from ..settings import log
from .contract import Artifact, Call, Result, Tool, ToolError

ENABLED = config.flag("tools.code_execution.enabled", False)
# Base de l'exécuteur : une adresse de CONFIGURATION, privée (le nom du
# service sur le réseau du compose).
URL = config.text("tools.code_execution.url", "").rstrip("/")
TOKEN = config.text("tools.code_execution.token", "")
# Délai (s) d'UN programme, demandé à l'exécuteur — qui a le sien, plus
# haut (SANDBOX_TIMEOUT), et ne va jamais au-delà.
TIMEOUT = config.num("tools.code_execution.timeout", 30)
# Appels par réponse, comptés à part des outils web (Tool.max_calls).
MAX_CALLS = config.integer("tools.code_execution.max_calls", 8)
# Caractères de sortie rendus au modèle : le début et la fin.
MAX_OUTPUT_CHARS = config.integer("tools.code_execution.max_output_chars",
                                  12_000)
MAX_CODE_CHARS = 200_000
# Ce que l'exécuteur ajoute au délai du programme, au pire : créer le bac
# (60 s), le délai de garde (10 s), lister (15 s) et archiver (30 s) les
# fichiers. Le délai de l'outil (Tool.timeout) couvre le tout.
MARGIN = 120

NAME = "code_execution"

# Ce que le modèle écrit → le langage de l'exécuteur (sandbox.LANGS).
# Les langages COMPILÉS ont leur valeur, plutôt que de passer par `bash` :
# le modèle donne son source comme pour Python, l'exécuteur le compile
# hors du dossier de travail (ni le source ni le binaire ne partent chez
# l'utilisateur) et l'exécute, dans le même délai. `bash` reste là pour un
# projet à plusieurs fichiers, avec make, cargo ou `go build`.
LANGUAGES = {
    "python": "python", "python3": "python", "py": "python",
    "bash": "bash", "shell": "bash", "sh": "sh",
    "javascript": "node", "js": "node", "node": "node", "nodejs": "node",
    "c": "c", "cpp": "cpp", "c++": "cpp", "cxx": "cpp",
    "go": "go", "golang": "go", "rust": "rust", "rs": "rust",
}
# Ceux que la fonction annonce (`enum`) ; les autres écritures sont
# acceptées sans être promises.
OFFERED = ("python", "bash", "javascript", "c", "cpp", "go", "rust")
_KILLED = {137: "killed — most likely out of memory",
           139: "segmentation fault",
           126: "command cannot be executed",
           127: "command not found"}


def _size(n: int) -> str:
    return f"{n} bytes" if n < 1000 else f"{n / 1000:.1f} kB" \
        if n < 1_000_000 else f"{n / 1_000_000:.1f} MB"


def _clip(text: str, limit: int) -> str:
    """Le début et la fin d'une sortie trop longue : une trace d'erreur
    est à la fin."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return (f"{text[:limit - half]}\n[… {len(text) - limit} characters "
            f"omitted …]\n{text[-half:]}")


def _media_type(name: str, data: bytes) -> str:
    """Une image à ses premiers octets, le reste à son nom."""
    return files.sniff(data) or mimetypes.guess_type(name)[0] \
        or "application/octet-stream"


def render(doc: dict, timeout: float, session: bool) -> Result:
    """La réponse de l'exécuteur → le Result. Le TEXTE d'abord : l'issue,
    l'état du bac, les fichiers, puis la sortie en dernier — si le texte
    est coupé plus loin ([tools].max_result_chars), c'est elle qui l'est.
    Tout ce qui vient du bac est HOSTILE : chaque champ est vérifié."""
    code = doc.get("exit_code")
    if doc.get("timed_out") is True:
        lines = [f"Timed out: the program was killed after {int(timeout)} s."]
    elif isinstance(code, int) and not isinstance(code, bool):
        lines = [f"Exit code: {code}"
                 + (f" ({_KILLED[code]})" if code in _KILLED else "")]
    else:
        raise ToolError("unavailable", "the sandbox returned an unreadable "
                                       "answer. Do not retry now.")
    if not session:
        lines.append("Sandbox: single-use — nothing is kept after this call.")
    elif doc.get("reset") is True:
        lines.append("Sandbox: destroyed by this run — its files are gone, "
                     "the next call starts in an empty one.")
    elif doc.get("fresh") is True:
        lines.append("Sandbox: new and empty — no file from an earlier call "
                     "exists here.")

    kept, lost = [], []
    for f in doc.get("files") if isinstance(doc.get("files"), list) else ():
        name = f.get("name") if isinstance(f, dict) else None
        if not isinstance(name, str) or not name:
            continue
        try:
            data = base64.b64decode(f.get("data") or "", validate=True)
        except (binascii.Error, ValueError, TypeError):
            lost.append((name, "could not be read"))
            continue
        why = files.refusal(len(data))
        if why:
            lost.append((name, why))
            continue
        served = files.safe_name(name)
        kept.append((name, Artifact(served, _media_type(served, data), data)))
    for f in doc.get("skipped") if isinstance(doc.get("skipped"), list) else ():
        if isinstance(f, dict) and isinstance(f.get("name"), str):
            lost.append((f["name"], str(f.get("reason") or "not returned")))
    if kept:
        lines.append("Files delivered to the user (shown with your answer; "
                     "do not write links or paths to them):")
        lines += [f"- {name} ({a.media_type}, {_size(len(a.data))})"
                  for name, a in kept]
    if lost:
        lines.append("Files NOT delivered to the user:")
        lines += [f"- {name[:200]}: {why}" for name, why in lost[:20]]

    output = doc.get("output")
    output = output if isinstance(output, str) else ""
    lines.append("Output:")
    lines.append(_clip(output, MAX_OUTPUT_CHARS) if output.strip()
                 else "(no output)")
    return Result("\n".join(lines), files=tuple(a for _, a in kept), meta={
        "exit_code": code if doc.get("timed_out") is not True else None,
        "timed_out": doc.get("timed_out") is True,
        "fresh": doc.get("fresh") is True,
        "files": [name for name, _ in kept]})


class CodeExecution(Tool):
    name = NAME

    @property
    def enabled(self) -> bool:
        return ENABLED

    @property
    def timeout(self) -> float:
        return TIMEOUT + MARGIN

    @property
    def max_calls(self) -> int:
        return MAX_CALLS

    def spec(self, present) -> dict:
        return {"type": "function", "function": {
            "name": NAME,
            "description": (
                "Run a program in a private sandbox and get back its exit "
                "code and its output (stdout and stderr). Use it to compute, "
                "analyse data, or produce a file (a chart, a spreadsheet, a "
                "document) instead of guessing a result. "
                "Python 3 comes with numpy, pandas, scipy, sympy, "
                "scikit-learn, networkx, matplotlib, seaborn, pillow, "
                "openpyxl, python-docx, pypdf, reportlab; bash has the usual "
                "tools, jq and sqlite3; Node.js has its standard library "
                "only. C, C++ (gcc, g++), Go and Rust programs are given as "
                "one source file, compiled, then run, all within the time "
                "limit — with their standard library only: no Go module, no "
                "Rust crate, no other package. There is NO network and "
                "nothing can be installed. "
                "Each call is a new process: variables and imports are not "
                "kept, but the files of the working directory and of /tmp "
                "are kept between the calls of a conversation. Print what "
                "you want to read. Every file the program creates or changes "
                "in the working directory is delivered to the user "
                "automatically, with your answer: write there only what the "
                "user should get (save a chart with matplotlib's savefig), "
                "keep intermediate files in /tmp, and never write a link or "
                "a path to a delivered file. "
                f"A program is killed after {int(TIMEOUT)} s."),
            "parameters": {
                "type": "object",
                "properties": {
                    "language": {
                        "type": "string",
                        "enum": list(OFFERED),
                        "description": "The language of `code`. For a "
                                       "compiled language, `code` is the "
                                       "whole source file, with its main."},
                    "code": {
                        "type": "string",
                        "description": "The complete program to run."},
                },
                "required": ["language", "code"],
            },
        }}

    def summary(self, args: dict, result: Result | None = None) -> dict:
        return {"type": NAME, "language": str(args.get("language") or "")}

    async def run(self, args: dict, call: Call, transport=None) -> Result:
        language = args.get("language")
        language = LANGUAGES.get(language.strip().lower()) \
            if isinstance(language, str) else None
        if language is None:
            raise ToolError("invalid_input", "`language` must be one of "
                                             + ", ".join(OFFERED) + ".")
        code = args.get("code")
        if not isinstance(code, str) or not code.strip():
            raise ToolError("invalid_input", "`code` is required: the "
                                             "complete program to run.")
        if len(code) > MAX_CODE_CHARS:
            raise ToolError("invalid_input", f"`code` is longer than "
                                             f"{MAX_CODE_CHARS} characters.")
        if not URL or not TOKEN:
            raise ToolError("unavailable",
                            "code execution is not configured on this proxy.")
        try:
            # trust_env=False : l'exécuteur est une adresse du réseau du
            # proxy, un HTTP_PROXY d'environnement n'a pas à s'en mêler.
            async with httpx.AsyncClient(
                    timeout=httpx.Timeout(TIMEOUT + MARGIN - 5, connect=5),
                    transport=transport, trust_env=False) as c:
                r = await c.post(
                    f"{URL}/v1/execute",
                    headers={"Authorization": f"Bearer {TOKEN}"},
                    json={"client": call.client, "session": call.session,
                          "language": language, "code": code,
                          "timeout": TIMEOUT})
        except httpx.HTTPError as exc:
            raise ToolError("unavailable", (
                f"the sandbox service is unreachable ({type(exc).__name__}). "
                f"Do not retry now: answer without running code, and say so."))
        if r.status_code == 429:
            raise ToolError("too_many_requests", (
                "every sandbox is busy. Try again once, later in this "
                "answer, or answer without running code."))
        if r.status_code != 200:
            # Le détail (un message de podman, un jeton refusé) est pour
            # le journal du proxy, pas pour le modèle.
            log.warning("code_execution : l'exécuteur a répondu HTTP %d — %s",
                        r.status_code, r.text[:400])
            raise ToolError("unavailable", (
                f"the sandbox service failed (HTTP {r.status_code}). Do not "
                f"retry now: answer without running code, and say so."))
        try:
            doc = r.json()
        except ValueError:
            doc = None
        if not isinstance(doc, dict):
            raise ToolError("unavailable", "the sandbox returned an "
                                           "unreadable answer. Do not retry now.")
        return render(doc, TIMEOUT, bool(call.session))


TOOL = CodeExecution()
