"""
`code_execution` : le modèle écrit un programme, le proxy le fait tourner
dans un BAC À SABLE et lui rend le code de sortie, la sortie et la liste
des fichiers produits.

Le proxy n'exécute RIEN lui-même : il appelle en HTTP le service
`executor` du docker-compose (services/executor/ dans le dépôt), le seul à
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

Des fichiers peuvent y ENTRER : `files`, des URL que le PROXY télécharge
— le bac n'a pas de réseau — sous le garde-fou commun (net.download,
[tools.net]) et ses propres bornes, puis passe à l'exécuteur, qui les
dépose dans le dossier de travail avant le programme. Ils y restent pour
les appels suivants de la conversation, et ne repartent pas chez
l'utilisateur (sauf modifiés par le programme : ce sont alors des
fichiers produits). Un fichier qui n'a pas pu entrer n'empêche pas
l'exécution — le texte dit lequel, et pourquoi ; si AUCUN n'entre, rien
n'est exécuté : le programme était écrit pour eux. Leur contenu vient du
web et sera lu par un programme du modèle : rien de plus à isoler que ce
que le bac isole, mais ce que le programme en IMPRIME est lu par le
modèle — un texte hostile de plus, comme une page de web_fetch.

Un programme qui sort en erreur, ou que son délai tue, n'est PAS une
erreur de l'outil : c'est un résultat, que le modèle lit et corrige. Les
codes d'erreur ne disent que les pannes d'ICI : arguments inutilisables
(`invalid_input`), exécuteur non configuré, injoignable ou en panne
(`unavailable`), tous les bacs occupés (`too_many_requests`) — et, quand
aucun des fichiers demandés n'a pu entrer, le code de leur refus
(`not_allowed`, `not_accessible`, `unsupported`…).

Sans liaison à l'API Responses ni à l'API Messages : l'outil se déclare
par {"type": "code_execution"} sur /v1/chat/completions (ou
[chat].always) et s'appelle par POST /v1/tools/code_execution.
"""

import asyncio
import base64
import binascii
import mimetypes
import time
from dataclasses import dataclass
from email.message import Message
from urllib.parse import unquote, urlsplit

import httpx

from .. import config, files
from ..settings import log
from . import net
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

# Les fichiers d'ENTRÉE (`files`) : des URL téléchargées ici, déposées
# dans le bac. Par appel : leur nombre (0 = le paramètre n'est ni offert
# ni accepté), la taille de chacun, leur taille à eux tous, et le temps
# donné à TOUS les téléchargements. Un fichier au-delà est refusé, jamais
# coupé. L'exécuteur a ses propres bornes (SANDBOX_MAX_INPUT*), qu'il
# applique sans croire celles-ci : les tenir au moins aussi hautes.
MAX_FILES = config.integer("tools.code_execution.max_files", 8)
MAX_FILE_BYTES = config.integer("tools.code_execution.max_file_bytes",
                                20_000_000)
MAX_FILES_BYTES = config.integer("tools.code_execution.max_files_bytes",
                                 40_000_000)
DOWNLOAD_TIMEOUT = config.num("tools.code_execution.download_timeout", 60)
# Téléchargements menés de front.
PARALLEL = 4
# Ce que le dépôt ajoute chez l'exécuteur, au pire : écrire (30 s), retirer
# ce qui est resté à moitié écrit (15 s).
INPUT_MARGIN = 45
USER_AGENT = ("llm-proxy code_execution "
              "(+https://github.com/c4software/llm-proxy)")

NAME = "code_execution"

# La description renvoie le modèle à `web_fetch` et à `ocr` pour ce qui
# n'est qu'à LIRE : vrai seulement là où ils lui sont présentés aussi
# (`present` de spec).
_READERS = ("web_fetch", "ocr")

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


@dataclass
class Input:
    """Un fichier d'entrée demandé par le modèle, et ce qu'il en est
    advenu : téléchargé (`data`, et son `name` dans le bac), ou refusé
    (`why` : la raison, en anglais, pour le modèle ; `code` : le code
    d'erreur du contrat)."""
    url: str
    name: str = ""          # demandé par le modèle, puis le nom RETENU
    data: bytes = b""
    code: str = ""
    why: str = ""

    def refuse(self, code: str, why: str) -> None:
        # Les messages de net.download commencent souvent par l'URL, que
        # la ligne du texte porte déjà.
        self.code, self.why = code, why.removeprefix(self.url + " ")


def _announced(r) -> int:
    length = r.headers.get("content-length", "")
    return int(length) if length.isdigit() else 0


def input_name(asked: str, url: str, r) -> str:
    """Le nom d'un fichier d'entrée dans le bac, assaini (files.safe_name :
    ni chemin, ni caractère qui ait un sens pour un shell). Dans l'ordre :
    celui que le modèle a DEMANDÉ ; le dernier élément du chemin de l'URL
    telle qu'il l'a écrite, s'il a une extension — le modèle écrit son
    programme dans le même appel, il doit pouvoir prévoir le nom ; celui
    de Content-Disposition ; à défaut le dernier élément tel quel, ou
    «file», avec l'extension du type annoncé."""
    last = unquote(urlsplit(url).path).rstrip("/").rsplit("/", 1)[-1]
    name = asked or ("." in last.strip(".") and last)
    if not name:
        header = Message()
        try:
            header["content-disposition"] = r.headers.get(
                "content-disposition", "")
            name = header.get_filename() or ""
        except (ValueError, LookupError):   # un filename* mal encodé
            name = ""
    if not name:
        kind = r.headers.get("content-type", "").split(";")[0].strip().lower()
        name = (last or "file") + (
            mimetypes.guess_extension(kind) or "" if kind else "")
    # Ni point (caché) ni tiret (une option) en tête : l'exécuteur les
    # refuse.
    return files.safe_name(name).lstrip("-.") or "file"


async def download(asked: list[Input], settings, transport=None) -> None:
    """Télécharge les fichiers d'entrée, PARALLEL à la fois, sous le
    garde-fou commun (net.download : adresses publiques, listes de
    domaines, à chaque redirection) et dans DOWNLOAD_TIMEOUT pour tous.
    Complète chaque Input : `data` et `name`, ou `code` et `why`. Un
    fichier trop gros — annoncé ou constaté —, ou qui ferait dépasser le
    total de l'appel, est REFUSÉ, jamais coupé : la moitié d'un tableur ne
    s'ouvre pas, celle d'un CSV se lit sans erreur. Ne lève pas."""
    deadline = time.monotonic() + DOWNLOAD_TIMEOUT
    gate = asyncio.Semaphore(PARALLEL)
    taken = 0       # octets des fichiers déjà retenus

    def limit(r, body: bytes) -> int:
        # Rien d'une réponse en erreur ni d'un fichier annoncé trop gros ;
        # sinon un octet au-delà de ce qui reste permis : de quoi savoir
        # que le fichier le dépasse.
        if not 200 <= r.status_code < 300 or _announced(r) > MAX_FILE_BYTES:
            return 0
        return min(MAX_FILE_BYTES, MAX_FILES_BYTES - taken) + 1

    async def one(item: Input) -> None:
        nonlocal taken
        async with gate:
            left = deadline - time.monotonic()
            try:
                if left <= 0:
                    raise asyncio.TimeoutError
                _, r, body = await asyncio.wait_for(net.download(
                    item.url, settings, timeout=left, limit=limit,
                    user_agent=USER_AGENT, transport=transport), left)
            except asyncio.TimeoutError:
                return item.refuse("not_accessible", (
                    f"took too long to download (all the files of a call "
                    f"have {int(DOWNLOAD_TIMEOUT)} s)."))
            except ToolError as exc:
                return item.refuse(exc.code, exc.message)
        if not 200 <= r.status_code < 300:  # un 3xx sans Location
            return item.refuse("not_accessible",
                               f"returned HTTP {r.status_code}.")
        if max(_announced(r), len(body)) > MAX_FILE_BYTES:
            return item.refuse("unsupported", (
                f"is larger than {MAX_FILE_BYTES} bytes, the limit for a "
                f"file."))
        if not body:
            return item.refuse("unsupported", "is empty.")
        if taken + len(body) > MAX_FILES_BYTES:
            return item.refuse("unsupported", (
                f"would take the files of this call over {MAX_FILES_BYTES} "
                f"bytes in total."))
        taken += len(body)
        item.data, item.name = body, input_name(item.name, item.url, r)

    await asyncio.gather(*(one(item) for item in asked))
    # Deux fichiers du même nom : le second est renommé (data-2.csv), dans
    # l'ordre de la demande — le texte dit au modèle sous quel nom.
    seen = set()
    for item in asked:
        if not item.why:
            stem, dot, ext = item.name.rpartition(".")
            n, name = 1, item.name
            while name.lower() in seen:
                n += 1
                name = f"{stem}-{n}.{ext}" if stem else f"{item.name}-{n}"
            seen.add(name.lower())
            item.name = name


def render(doc: dict, timeout: float, session: bool, inputs=()) -> Result:
    """La réponse de l'exécuteur → le Result. Le TEXTE d'abord : l'issue,
    l'état du bac, les fichiers, puis la sortie en dernier — si le texte
    est coupé plus loin ([tools].max_result_chars), c'est elle qui l'est.
    `inputs` : les Input de l'appel, téléchargés ou non.
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

    # Les fichiers d'entrée : ceux que l'exécuteur DIT avoir déposés, parmi
    # ceux qui lui ont été envoyés. Sans liste `inputs` dans sa réponse,
    # c'est un exécuteur d'avant le dépôt : il a ignoré le champ et lancé
    # le programme sans eux.
    placed = doc.get("inputs")
    placed = {f.get("name") for f in placed if isinstance(f, dict)} \
        if isinstance(placed, list) else None
    refused = {f.get("name"): str(f.get("reason") or "")
               for f in (doc.get("rejected") or ())
               if isinstance(f, dict)} \
        if isinstance(doc.get("rejected"), list) else {}
    copied, missing = [], []
    for item in inputs:
        if item.why:
            missing.append((item.url, item.why))
        elif placed is None:
            missing.append((item.url, "the sandbox service is too old to "
                                      "receive files."))
        elif item.name in placed:
            copied.append(item)
        else:
            missing.append((item.url, "the sandbox did not accept it ("
                            + (refused.get(item.name) or "could not be "
                               "written")[:100] + ")."))
    if copied:
        lines.append("Files copied into the working directory before the "
                     "run (kept there for the next calls):" if session else
                     "Files copied into the working directory before the "
                     "run:")
        lines += [f"- {item.name} ({_size(len(item.data))}), from "
                  f"{item.url[:300]}" for item in copied]
    if missing:
        lines.append("Files NOT copied — the program ran without them:")
        lines += [f"- {url[:300]}: {why}" for url, why in missing]

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
        "files": [name for name, _ in kept],
        **({"inputs": [item.name for item in copied]} if inputs else {})})


class CodeExecution(Tool):
    name = NAME

    @property
    def enabled(self) -> bool:
        return ENABLED

    @property
    def timeout(self) -> float:
        # Le pire : télécharger les fichiers d'entrée, puis l'exécuteur.
        return TIMEOUT + MARGIN + DOWNLOAD_TIMEOUT + INPUT_MARGIN

    @property
    def max_calls(self) -> int:
        return MAX_CALLS

    def spec(self, present) -> dict:
        readers = [name for name in _READERS if name in present]
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
                + ((
                    "To work on a file that is at a URL (a CSV, a "
                    "spreadsheet, a PDF, an image, an archive), list it in "
                    "`files`: it is downloaded for you and is in the working "
                    "directory when the program starts. It stays there for "
                    "the later calls of the conversation: do not list it "
                    "again. "
                    + (f"To only READ a page or a document, use "
                       f"{' or '.join(readers)} instead. " if readers else "")
                ) if MAX_FILES > 0 else "") +
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
                    **({"files": {
                        "type": "array",
                        "description": (
                            f"Optional. Files to download into the working "
                            f"directory before the program runs: up to "
                            f"{MAX_FILES}, {_size(MAX_FILE_BYTES)} each."),
                        "items": {
                            "type": "object",
                            "properties": {
                                "url": {
                                    "type": "string",
                                    "description": "The http(s) URL of the "
                                                   "file."},
                                "name": {
                                    "type": "string",
                                    "description": (
                                        "The file name to give it, e.g. "
                                        "data.csv. Default: the last "
                                        "segment of the URL. Give one when "
                                        "the URL does not end with a file "
                                        "name.")},
                            },
                            "required": ["url"],
                        }}} if MAX_FILES > 0 else {}),
                },
                "required": ["language", "code"],
            },
        }}

    def summary(self, args: dict, result: Result | None = None) -> dict:
        return {"type": NAME, "language": str(args.get("language") or "")}

    async def run(self, args: dict, call: Call, transport=None,
                  downloads=None) -> Result:
        """`transport`, `downloads` : pour les tests — les transports
        httpx vers l'exécuteur et vers le web."""
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
        inputs = self._inputs(args.get("files"))
        if not URL or not TOKEN:
            raise ToolError("unavailable",
                            "code execution is not configured on this proxy.")
        await download(inputs, call.settings, transport=downloads)
        ready = [item for item in inputs if not item.why]
        if inputs and not ready:
            # Le programme était écrit pour ces fichiers : sans aucun
            # d'eux il ne ferait qu'échouer, en consommant un appel. Le
            # code : celui de leur refus s'il est unique.
            codes = {item.code for item in inputs}
            raise ToolError(
                codes.pop() if len(codes) == 1 else "not_accessible",
                "the program was NOT run: none of its files could be "
                "copied into the sandbox.\n" + "\n".join(
                    f"- {item.url[:300]}: {item.why}" for item in inputs))
        body = {"client": call.client, "session": call.session,
                "language": language, "code": code, "timeout": TIMEOUT}
        if ready:
            # Absent sans fichier : la requête d'avant, pour un exécuteur
            # d'avant.
            body["inputs"] = [
                {"name": item.name,
                 "data": base64.b64encode(item.data).decode("ascii")}
                for item in ready]
        try:
            # trust_env=False : l'exécuteur est une adresse du réseau du
            # proxy, un HTTP_PROXY d'environnement n'a pas à s'en mêler.
            async with httpx.AsyncClient(
                    timeout=httpx.Timeout(
                        TIMEOUT + MARGIN - 5 + (INPUT_MARGIN if ready else 0),
                        connect=5),
                    transport=transport, trust_env=False) as c:
                r = await c.post(
                    f"{URL}/v1/execute",
                    headers={"Authorization": f"Bearer {TOKEN}"}, json=body)
        except httpx.HTTPError as exc:
            raise ToolError("unavailable", (
                f"the sandbox service is unreachable ({type(exc).__name__}). "
                f"Do not retry now: answer without running code, and say so."))
        if r.status_code == 429:
            raise ToolError("too_many_requests", (
                "every sandbox is busy. Try again once, later in this "
                "answer, or answer without running code."))
        if r.status_code == 413 and ready:
            # Les bornes d'ici dépassent celles de l'exécuteur : à régler
            # par celui qui déploie, et à contourner par le modèle.
            log.warning("code_execution : l'exécuteur refuse %d octets de "
                        "fichiers d'entrée (HTTP 413) — [tools.code_execution]"
                        ".max_files_bytes dépasse son "
                        "SANDBOX_MAX_INPUT_TOTAL_BYTES",
                        sum(len(item.data) for item in ready))
            raise ToolError("unsupported", (
                "the program was NOT run: its files are larger, together, "
                "than the sandbox accepts in one call. Pass fewer or "
                "smaller files."))
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
        return render(doc, TIMEOUT, bool(call.session), inputs)

    @staticmethod
    def _inputs(asked) -> list[Input]:
        """`files` du modèle → les Input à télécharger. Chaque entrée : un
        objet {"url", "name"?} — ou une URL nue, acceptée sans être
        promise. Lève ToolError `invalid_input`."""
        if asked is None or asked == []:
            return []
        if MAX_FILES <= 0:
            raise ToolError("invalid_input", "this proxy does not copy files "
                                             "into the sandbox: call again "
                                             "without `files`.")
        if not isinstance(asked, list):
            raise ToolError("invalid_input", "`files` must be a list of "
                                             '{"url": …} objects.')
        if len(asked) > MAX_FILES:
            raise ToolError("invalid_input", f"`files` has more than "
                                             f"{MAX_FILES} entries, the limit "
                                             f"for one call.")
        inputs = []
        for entry in asked:
            url, name = (entry.get("url"), entry.get("name")) \
                if isinstance(entry, dict) else (entry, None)
            if not isinstance(url, str) or not url.strip() \
                    or not isinstance(name, (str, type(None))):
                raise ToolError("invalid_input", (
                    "each entry of `files` must be an object with a `url` "
                    "(an http(s) URL) and, optionally, a `name`."))
            url = url.strip()
            inputs.append(Input("https://" + url if url.startswith("www.")
                                else url, (name or "").strip()))
        return inputs


TOOL = CodeExecution()
