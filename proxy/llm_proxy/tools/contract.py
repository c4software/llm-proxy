"""
Le CONTRAT d'un outil hébergé : ce qu'un outil doit offrir, ce qu'il
reçoit, ce qu'il rend. Tout le reste du proxy (tools.Hosted, les trois
surfaces, /v1/tools, les statistiques) ne connaît d'un outil que ce qui
est écrit ici — décrit membre par membre dans docs/outils.md.

Ce module n'importe rien du paquet : un outil l'importe sans cycle.

CE QU'UN OUTIL FAIT LIRE AU MODÈLE, toujours en anglais : son prompt
(`Tool.prompt`, lu à CHAQUE requête où l'outil est présenté), les
descriptions de ses paramètres (`Tool.parameters`), `Result.text`, le
texte d'une erreur («Error: <message>»), et les textes que tools/
__init__.py écrit en son nom (limite d'appels, délai, panne, arguments
illisibles, «[truncated]», EXPIRED). Ce qui est en vigueur se relit par
GET /v1/tools.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping


class ToolName(StrEnum):
    """Les outils NATIFS du proxy, liste FERMÉE : le nom de la fonction
    présentée au modèle. Le reste du proxy les nomme par elle, jamais par
    une chaîne ; chacun est implémenté une fois (tools/__init__.py le
    vérifie au démarrage). Les outils FOURNIS (les serveurs MCP) n'y
    sont pas : leurs noms se découvrent."""
    WEB_SEARCH = "web_search"
    WEB_FETCH = "web_fetch"
    OCR = "ocr"
    WEB_GREP = "web_grep"
    TRANSCRIBE = "transcribe"
    CODE_EXECUTION = "code_execution"
    IMAGE_GENERATION = "image_generation"
    MEMORY = "memory"

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
    `client`   : le condensé de la clé du client (tools.owner), «» pour
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


NATIVE = frozenset(ToolName)

# Les membres publics de l'interface : un outil n'en a pas d'autres.
MEMBERS = frozenset(n for n in dir(Tool) if not n.startswith("_"))


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
    # Ce qui est à écrire.
    for method in ("prompt", "run"):
        if getattr(type(tool), method) is getattr(Tool, method):
            out.append(f"{where} : {method}() non écrit")
    # Les liaisons aux protocoles.
    if tool.responses is not None and not (
            isinstance(tool.responses, Responses) and tool.responses.kinds):
        out.append(f"{where} : `responses` doit être un Responses, avec ses kinds")
    if tool.anthropic is not None and not isinstance(tool.anthropic, Anthropic):
        out.append(f"{where} : `anthropic` doit être un Anthropic")
    return out
