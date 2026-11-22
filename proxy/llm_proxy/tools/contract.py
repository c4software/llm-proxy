"""
Le CONTRAT d'un outil — un seul, quel que soit le moteur qui l'exécute
(Tool.runs) : ce qu'un outil DÉCLARE (nom, prompt, schéma, où il est
offert ; pour ceux du navigateur, leur Family), ce qu'il reçoit, ce qu'il
rend. Tout le reste du proxy (tools.Hosted, les trois surfaces,
/v1/tools, les statistiques, la session de la page) ne connaît d'un outil
que ce qui est écrit ici — décrit membre par membre dans docs/outils.md.

Un outil `Runs.PROXY` s'exécute ici (`run`). Un outil `Runs.BROWSER`
(BrowserTool) n'a ici que sa déclaration : la page la reçoit par
/ui/session et l'exécute en JS (chat/tools/, le comportement : son run ou
sa carte, et pour sa famille `available`, `enabled`, `state`).

Ce module n'importe rien du paquet : un outil l'importe sans cycle.

CE QU'UN OUTIL DU PROXY FAIT LIRE AU MODÈLE, toujours en anglais (ceux
du navigateur, écrits pour le chat de la page, sont en français) : son prompt
(`Tool.prompt`, lu à CHAQUE requête où l'outil est présenté), les
descriptions de ses paramètres (`Tool.parameters`), `Result.text`, le
texte d'une erreur («Error: <message>»), et les textes que tools/
__init__.py écrit en son nom (limite d'appels, délai, panne, arguments
illisibles, «[truncated]», EXPIRED). Ce qui est en vigueur se relit par
GET /v1/tools.
"""

import copy
import re
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping


class ToolName(StrEnum):
    """Les outils NATIFS, liste FERMÉE, ceux du proxy comme ceux du
    navigateur : le nom de la fonction présentée au modèle. Le reste du
    proxy les nomme par elle, jamais par une chaîne ; chacun est déclaré
    une fois (tools/__init__.py le vérifie au démarrage). Les outils
    FOURNIS (serveurs MCP, extensions) n'y sont pas."""
    WEB_SEARCH = "web_search"
    WEB_FETCH = "web_fetch"
    OCR = "ocr"
    WEB_GREP = "web_grep"
    TRANSCRIBE = "transcribe"
    CODE_EXECUTION = "code_execution"
    IMAGE_GENERATION = "image_generation"
    MEMORY = "memory"
    WEBHOOK = "webhook"
    REPOSITORY = "repository"
    # Ceux que le NAVIGATEUR exécute (Runs.BROWSER, déclarés dans tools/page/,
    # exécutés par chat/tools/ de la page).
    ASK_USER = "ask_user"
    SAVE_FILE = "save_file"
    SHOW_ARTIFACT = "show_artifact"
    EDIT_ARTIFACT = "edit_artifact"
    READ_ARTIFACT = "read_artifact"
    GREP_ARTIFACT = "grep_artifact"
    REQUEST_ARTIFACT_DEBUG = "request_artifact_debug"
    INSPECT_ARTIFACT = "inspect_artifact"
    RUN_IN_ARTIFACT = "run_in_artifact"
    FOLDER_LIST = "folder_list"
    FOLDER_READ = "folder_read"
    FOLDER_SEARCH = "folder_search"
    FOLDER_WRITE = "folder_write"
    FOLDER_EDIT = "folder_edit"
    DEVICE_CONTEXT = "device_context"
    GET_LOCATION = "get_location"
    NOTIFY = "notify"
    CLIPBOARD_WRITE = "clipboard_write"
    CLIPBOARD_READ = "clipboard_read"
    ADD_TO_CALENDAR = "add_to_calendar"
    OPEN_LINK = "open_link"
    SHARE = "share"
    CAPTURE_SCREEN = "capture_screen"
    TAKE_PHOTO = "take_photo"
    SCAN_CODE = "scan_code"
    PICK_COLOR = "pick_color"
    KEEP_SCREEN_ON = "keep_screen_on"
    SPEAK = "speak"
    RUN_JAVASCRIPT = "run_javascript"
    CHECK_TYPESCRIPT = "check_typescript"
    RUN_PHP = "run_php"
    RUN_SQL = "run_sql"
    RUN_POSTGRES = "run_postgres"
    RUN_TOOLS_PROGRAM = "run_tools_program"
    RUN_SHELL = "run_shell"
    SHELL_READ = "shell_read"
    SHELL_GREP = "shell_grep"
    SHELL_EDIT = "shell_edit"
    SHELL_WRITE = "shell_write"

class FamilyKey(StrEnum):
    """Les FAMILLES natives des outils du navigateur (Family), liste
    FERMÉE : ce que le menu Outils de la page coche ou non. Une clé ne
    change pas : les conversations gardent celles qui sont cochées."""
    ASK = "ask"
    SAVE = "save"
    ARTIFACTS = "artifacts"
    ARTIFACT_DEBUG_OFFER = "artifact_debug_offer"
    ARTIFACT_DEBUG = "artifact_debug"
    FOLDER = "folder"
    CONTEXT = "context"
    LOCATION = "location"
    NOTIFY = "notify"
    CLIPBOARD = "clipboard"
    CALENDAR = "calendar"
    LINKS = "links"
    SHARE = "share"
    SCREEN = "screen"
    CAMERA = "camera"
    CODES = "codes"
    COLOR = "color"
    WAKE = "wake"
    SPEAK = "speak"
    JAVASCRIPT = "compute"
    PHP = "php"
    SQL = "sql"
    POSTGRES = "postgres"
    LINUX = "linux"
    PROGRAMS = "programs"


class Runs(StrEnum):
    """Où un outil S'EXÉCUTE (Tool.runs) : `proxy`, son `run` Python ;
    `browser`, la page — le proxy n'en a que la DÉCLARATION, qu'il sert
    à la page (/ui/session), et l'exécution est en JS (chat/tools/)."""
    PROXY = "proxy"
    BROWSER = "browser"


class Offer(StrEnum):
    """Où un outil est OFFERT (Tool.offered) : `api`, aux clients de l'API
    (/v1/chat/completions, /v1/responses, /v1/messages, /v1/tools, /mcp) ;
    `web`, au chat de la page et aux réponses des webhooks entrants (qui
    passent par lui). Une présentation, pas une autorisation : la page et
    les webhooks n'ont pas de droits de clé (docs/securite.md)."""
    API = "api"
    WEB = "web"


# Les codes d'erreur, liste FERMÉE. Un outil n'en rend pas d'autre (un
# code inconnu est ramené à `unavailable` par l'exécuteur) ; chaque
# surface les traduit dans son vocabulaire.
ERRORS = (
    "invalid_input",        # arguments du modèle inutilisables
    "not_allowed",          # cible refusée par une règle (domaines, adresse)
    "not_accessible",       # cible injoignable, introuvable, en erreur
    "unsupported",          # contenu que l'outil ne sait pas rendre
    "too_many_requests",    # la cible demande de ralentir
    "timeout",              # délai de l'exécution dépassé
    "limit",                # limite d'appels de la réponse atteinte
    "failed",               # l'outil a tourné et rapporte lui-même un échec
    "unavailable",          # l'outil lui-même est en panne, ou non configuré
)


@dataclass(frozen=True)
class Source:
    """Une page que le résultat cite : de quoi faire une annotation, un
    bloc de résultat de recherche. Seule `url` est obligatoire."""
    url: str
    title: str = ""
    date: str = ""          # AAAA-MM-JJ, ou «»
    snippet: str = ""


@dataclass(frozen=True)
class Artifact:
    """Un FICHIER produit par un outil : le graphique ou le tableur sorti
    d'une exécution de code. `name` : un nom de fichier, sans chemin ;
    `media_type` : son type MIME ; `data` : ses octets."""
    name: str
    media_type: str
    data: bytes


@dataclass(frozen=True)
class Result:
    """Ce qu'un outil rend — le SEUL format d'échange entre un outil et le
    reste du proxy.

    `text`    : ce que le modèle lit. Seul ce texte est mémorisé et
                rejoué : tout ce que le modèle doit savoir y est écrit.
    `error`   : un code d'ERRORS, None pour un succès. Le texte d'une
                erreur commence par «Error: » (voir failure) ; le code
                voyage à côté, il ne se déduit jamais du texte.
    `sources` : les pages citées, pour le client (annotations, blocs).
    `meta`    : des faits pour l'affichage du client (URL réellement lue,
                titre…). Rien que le modèle doive lire, rien de gardé.
    `files`   : des fichiers produits (Artifact), rendus au client en
                LIENS (files.py). Aucune mémoire ne les garde : `text`
                doit donc les NOMMER pour que le modèle sache qu'ils
                existent."""
    text: str
    error: str | None = None
    sources: tuple[Source, ...] = ()
    meta: Mapping = field(default_factory=dict)
    files: tuple[Artifact, ...] = ()


def failure(code: str, message: str) -> Result:
    """Le résultat d'un échec : «Error: <message>» pour le modèle, qui
    s'adapte, et le code à côté. `message` : une phrase en anglais,
    ponctuation comprise."""
    return Result(f"Error: {message}", code)


class ToolError(Exception):
    """Levée par `run` pour un échec PRÉVU : l'exécuteur la rend en
    failure(code, message). Toute autre exception est une panne de
    l'outil — `unavailable`, sans son message."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass(frozen=True)
class Call:
    """Ce qu'un outil sait de l'appel et qui ne vient PAS du modèle — qui
    ne peut donc pas s'en affranchir.

    `settings` : ce que le CLIENT a réglé sur cet outil dans sa requête.
                 Clés connues : `allowed_domains`, `blocked_domains`
                 (listes), `max_chars` (taille du contenu rendu). Un
                 outil ignore celles qui ne le concernent pas.
    `endpoint` : la route par où l'appel arrive («/v1/responses»…).
    `model`    : le modèle PRÉFIXÉ de la conversation, «» pour l'appel
                 direct.
    `client`   : le condensé de la clé du client (identity.owner), «» pour
                 un proxy ouvert — jamais la clé. STABLE d'un démarrage
                 à l'autre : un outil peut y attacher ce qu'il garde.
    `session`  : l'identifiant de la CONVERSATION, de quoi retrouver un
                 état d'un appel au suivant (le bac de code_execution).
                 «» = la surface n'en a pas : un état par appel.
    `hosts`    : les sites connus de la conversation (provenance.py),
                 None hors conversation."""
    settings: Mapping = field(default_factory=dict)
    endpoint: str = ""
    model: str = ""
    client: str = ""
    session: str = ""
    # Les SITES déjà apparus dans la conversation (tools/provenance.py) :
    # un outil qui lit une URL du modèle n'ouvre que ceux-là. None = la
    # route ne voit pas de conversation (/v1/tools, /mcp) : pas de contrôle.
    hosts: frozenset | None = None


# ── liaisons aux protocoles ─────────────────────────────────────────────
# Comment un protocole NOMME l'outil et rend compte de son appel : des
# données, posées sur l'outil. Sans liaison, l'outil reste exécutable par
# /v1/tools et présentable sur /v1/chat/completions.

@dataclass(frozen=True)
class Responses:
    """API Responses. `kinds` : les types d'outil qui l'activent dans
    `tools` (`{"type": "web_search"}`) ; `item` : l'élément qui rend
    compte de l'appel au client (`web_search_call`), dont l'`action` est
    le `summary` de l'outil."""
    kinds: tuple[str, ...]
    item: str


@dataclass(frozen=True)
class Anthropic:
    """API Messages. `prefix` : celui du type de l'outil serveur, toutes
    versions datées (`web_search` pour `web_search_20250305`) — c'est
    aussi celui de son compteur d'usage (`web_search_requests`) ;
    `block` : le bloc qui rend le résultat au client
    (`web_search_tool_result`), dont anthropic_api connaît le format."""
    prefix: str
    block: str


class Tool:
    """Un outil hébergé. Une classe de base légère : `name`, `prompt` et
    `run` sont à écrire — `parameters` aussi dès que l'outil prend des
    arguments —, le reste a un défaut.

    L'INTERFACE est fermée : un outil n'a pas d'autre membre public que
    ceux de Tool (une aide est `_privée`, ou une fonction du module) ; un
    outil fourni qui porte son propre état le déclare dans `extra`.
    register() refuse le reste (faults)."""

    # Le nom de la fonction présentée au modèle, et celui de /v1/tools.
    name = ""
    # Actif ? Les outils du paquet le lisent dans [tools.<nom>].enabled.
    enabled = True
    responses: Responses | None = None
    anthropic: Anthropic | None = None
    # Délai (s) d'UNE exécution, propre à l'outil ; None = le délai
    # commun, [tools].run_timeout. L'exécuteur l'applique.
    timeout: float | None = None
    # Appels exécutés au plus pour UNE réponse, propres à l'outil et
    # comptés À PART des autres ; None = le plafond commun,
    # [tools].max_calls, que tous ces outils-là partagent.
    max_calls: int | None = None
    # Ce que le refus par limite d'appels dit des appels comptés au
    # plafond commun : «the limit of 8 <family> tool calls». «» = rien.
    family = ""
    # Où l'outil est offert (Offer) : présenté, listé et exécutable là
    # seulement. Défaut : partout. Un outil du navigateur : web seulement.
    offered: frozenset = frozenset(Offer)
    # Où il s'exécute (Runs) : ici (`run`), ou dans la page (BrowserTool).
    runs = Runs.PROXY
    # Des valeurs que le prompt cite et que l'exécution applique (une
    # borne, un délai) : écrites UNE fois, ici ; la page les reçoit avec la
    # déclaration d'un outil du navigateur (PageTool.settings).
    settings: Mapping = MappingProxyType({})
    # Les membres publics qu'un outil FOURNI ajoute à l'interface (l'état
    # d'un outil MCP : son serveur, son schéma…). Un outil natif n'en a pas.
    extra: frozenset[str] = frozenset()

    @property
    def kinds(self) -> tuple[str, ...]:
        """Les types qui DÉCLARENT l'outil dans `tools` d'une requête
        chat/completions : ceux de sa liaison Responses, ou son nom."""
        return self.responses.kinds if self.responses else (self.name,)

    def prompt(self, present) -> str:
        """Le PROMPT de l'outil : le texte que le modèle lit pour savoir
        quand et comment l'appeler — la `description` de sa fonction.
        En anglais. `present` : les noms des outils hébergés présentés
        AVEC lui dans cette requête, le sien compris — un prompt ne
        renvoie qu'à ce que le modèle peut appeler."""
        raise NotImplementedError(
            f"{type(self).__name__}.prompt : un outil écrit son prompt")

    def parameters(self, present) -> dict:
        """Le schéma JSON des arguments. Ses `description` font partie du
        prompt : le modèle les lit, elles s'écrivent comme lui. `present` :
        comme pour prompt. Rend un schéma à lui, jamais un objet partagé :
        les surfaces l'envoient tel quel. Défaut : aucun argument."""
        return {"type": "object", "properties": {}}

    def spec(self, present) -> dict:
        """La fonction présentée au modèle, à la forme chat/completions,
        ASSEMBLÉE de `name`, `prompt` et `parameters`. Un outil ne la
        réécrit pas."""
        return {"type": "function", "function": {
            "name": self.name,
            "description": self.prompt(present),
            "parameters": self.parameters(present)}}

    async def run(self, args: dict, call: Call) -> Result:
        """L'exécution. `args` : les arguments du modèle, un objet JSON
        déjà lu, RIEN de validé. Rend un Result, ou lève ToolError."""
        raise NotImplementedError

    def summary(self, args: dict, result: Result | None = None) -> dict:
        """Ce que le client affiche de l'appel (l'`action` d'un élément
        Responses) : un `type`, et ce qui le distingue. `result` None =
        l'appel seul, sans son issue (un élément rejoué)."""
        return {"type": self.name}

    def render(self, args: dict, sources) -> str:
        """Le texte du modèle, REFAIT depuis des sources — pour un
        protocole dont le client rejoue les sources et non le texte (le
        bloc `web_search_tool_result` d'Anthropic). À écrire par un outil
        lié à un tel bloc, et par lui seul."""
        raise NotImplementedError


class BrowserTool(Tool):
    """Un outil que le NAVIGATEUR exécute : sa déclaration, rien d'autre.
    `description` : son prompt ; `params` : {nom: schéma JSON} ;
    `required` : les paramètres obligatoires ; `settings` : voir Tool.
    Sa fonction est la même que celle d'un outil du proxy (Tool.spec)."""
    runs = Runs.BROWSER
    offered = frozenset({Offer.WEB})

    def __init__(self, name: str, description: str, params: Mapping | None = None,
                 required=(), settings: Mapping | None = None):
        self.name = name
        self._description = description
        self._params = dict(params or {})
        self._required = tuple(required)
        self.settings = MappingProxyType(dict(settings or {}))

    def prompt(self, present) -> str:
        return self._description

    def parameters(self, present) -> dict:
        out = {"type": "object", "properties": copy.deepcopy(self._params)}
        if self._required:
            out["required"] = list(self._required)
        return out


@dataclass(frozen=True)
class Family:
    """Une FAMILLE d'outils du navigateur : ce que le menu Outils de la
    page coche ou non, par conversation — ou, `fixed`, toujours active et
    hors du menu. `tools` : les noms de ses outils (BrowserTool) ;
    `on` : cochée d'office dans une conversation neuve ; `authored` : ses
    résultats sont écrits par le MODÈLE ou son code (un bac, un artifact),
    ils ne font connaître aucun site (provenance) ; `guidance` : une
    consigne ajoutée au prompt système quand elle est active, FIGÉE (le
    prompt système ne change pas d'un tour à l'autre). Son comportement
    (`available`, `enabled`, `state`) est en JS, à côté de ses outils."""
    key: str
    label: str
    tools: tuple[str, ...]
    hint: str = ""
    on: bool = False
    fixed: bool = False
    authored: bool = False
    guidance: str = ""


NATIVE = frozenset(ToolName)

# Les membres publics de l'interface : un outil n'en a pas d'autres.
MEMBERS = frozenset(n for n in dir(Tool) if not n.startswith("_"))
# Le nom d'un outil FOURNI (MCP, extension), et ceux qu'il ne peut pas
# prendre : des types de `tools` qui disent autre chose qu'un outil.
PROVIDED_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
RESERVED = frozenset({"function", "custom", "mcp"})


def faults(tool, provider: str = "") -> list[str]:
    """Ce qu'un outil a de trop ou de travers, au regard du contrat : la
    liste des fautes, vide s'il est conforme. `provider` : celui qui
    FOURNIT l'outil (« mcp ») ; vide, c'est un outil natif, nommé par
    ToolName."""
    if not isinstance(tool, Tool):
        return [f"{tool!r} : n'hérite pas de Tool"]
    where = f"outil {tool.name!r}"
    out = []
    if not tool.name or not isinstance(tool.name, str):
        out.append(f"{where} : sans nom")
    elif not provider and tool.name not in NATIVE:
        out.append(f"{where} : nom hors de ToolName (un outil natif)")
    elif provider and tool.name in NATIVE:
        out.append(f"{where} : fourni par {provider}, mais un nom d'outil natif")
    elif provider and (not PROVIDED_NAME.match(tool.name) or tool.name in RESERVED):
        # Le nom d'une fonction pour tous les backends, et pas un type que
        # les surfaces ou les droits lisent autrement (`function`, `mcp`).
        out.append(f"{where} : nom invalide pour un outil fourni ([A-Za-z0-9_-], 64 "
                   f"caractères au plus, ni {', '.join(sorted(RESERVED))})")
    if tool.extra and not provider:
        out.append(f"{where} : `extra` est réservé aux outils fournis")
    # Les membres : ceux de l'interface, et ce que l'outil fourni déclare.
    allowed = MEMBERS | tool.extra
    own = set(vars(tool))
    for cls in type(tool).__mro__:
        if cls is Tool:
            break
        own |= set(vars(cls))
    for name in sorted(own):
        if not name.startswith("_") and name not in allowed:
            out.append(f"{where} : membre public inconnu {name!r} (pas dans Tool)")
    # Ce qui est à écrire : le prompt ; `run` pour un outil du proxy, et
    # jamais pour un outil du navigateur (la page l'exécute).
    if getattr(type(tool), "prompt") is Tool.prompt:
        out.append(f"{where} : prompt() non écrit")
    written = getattr(type(tool), "run") is not Tool.run
    if tool.runs not in set(Runs):
        out.append(f"{where} : `runs` doit être un Runs")
    elif tool.runs == Runs.PROXY and not written:
        out.append(f"{where} : run() non écrit")
    elif tool.runs == Runs.BROWSER:
        if written:
            out.append(f"{where} : run() écrit, mais le navigateur l'exécute")
        if set(tool.offered) != {Offer.WEB}:
            out.append(f"{where} : un outil du navigateur n'est offert qu'au web")
        if tool.responses or tool.anthropic or tool.timeout is not None \
                or tool.max_calls is not None or tool.extra:
            out.append(f"{where} : un outil du navigateur n'a ni liaison, ni timeout, "
                       f"ni max_calls, ni extra")
    # Les liaisons aux protocoles.
    if tool.responses is not None and not (
            isinstance(tool.responses, Responses) and tool.responses.kinds):
        out.append(f"{where} : `responses` doit être un Responses, avec ses kinds")
    if tool.anthropic is not None and not isinstance(tool.anthropic, Anthropic):
        out.append(f"{where} : `anthropic` doit être un Anthropic")
    if not isinstance(tool.settings, Mapping):
        out.append(f"{where} : `settings` doit être un dict")
    if not isinstance(tool.offered, (set, frozenset)) or not tool.offered \
            or not set(tool.offered) <= set(Offer):
        out.append(f"{where} : `offered` doit être un ensemble non vide de "
                   f"{', '.join(Offer)}")
    return out
