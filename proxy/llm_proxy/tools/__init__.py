"""
Les outils HÉBERGÉS du proxy : ceux qu'il exécute lui-même, au lieu de
rendre la main au client. Un client de l'API Responses déclare
`{"type": "web_search"}` en comptant qu'OpenAI fera la recherche ; un
client de l'API Messages, `{"type": "web_search_20250305"}` ou
`{"type": "web_fetch_20250910"}`, qu'Anthropic exécuterait. Derrière ce
proxy il n'y a ni l'un ni l'autre : c'est donc ici qu'elle se fait.

Un outil = un objet qui tient le contrat de contract.py (décrit membre par
membre dans docs/outils.md, « Écrire un outil »). mcp.py est un
FOURNISSEUR : il apporte au registre, au démarrage puis à chaque
découverte, les outils des serveurs MCP de la configuration.

Ce module porte ce qui est commun : le REGISTRE, l'exécution bornée
(Hosted.run : nombre d'appels, délai, taille du résultat — tout échec est
un Result), la ligne de statistiques de chaque exécution (des mesures,
jamais le contenu) et la MÉMOIRE des résultats. Le client Responses
renvoie au tour suivant l'élément `web_search_call` SANS son résultat
(OpenAI le garde côté serveur) : il faut le rendre au modèle à
l'identique, sinon il perd ce qu'il a lu et le préfixe change sous un
backend à cache. Cette mémoire ne garde que du texte, bornée, en mémoire
(un redémarrage l'oublie, le modèle reçoit alors EXPIRED), et CLOISONNÉE
par client (`owner`). La surface Anthropic n'y range rien : son client
renvoie le résultat avec l'appel.

La boucle qui relance le backend vit dans tool_loop.py ; la traduction
des éléments, dans responses_api.py, anthropic_api.py et chat_api.py, qui
ne connaissent de ce paquet que `Hosted` et le contrat.
"""

import asyncio
import contextvars
import dataclasses
import json
import re
import time
from collections import OrderedDict

from ..core import config
from ..core.settings import INCOGNITO, log
from ..storage import stats
from . import (  # noqa: F401
    contract,
    code_execution,
    image_generation,
    mcp,
    memory,
    net,
    ocr,
    transcribe,
    web_fetch,
    web_grep,
    web_search,
    webcache,
    repository,
    webhook,
    extensions,
    page,
)
from .contract import (  # noqa: F401
    ERRORS,
    Anthropic,
    BrowserTool,
    Family,
    FamilyKey,
    Offer,
    Artifact,
    Call,
    Responses,
    Result,
    Source,
    Tool,
    ToolError,
    ToolName,
    Runs,
    failure,
    faults,
)

# Le registre : tous les outils que le proxy SAIT exécuter, actifs ou non,
# dans l'ordre où ils sont présentés au modèle.
REGISTRY: list[Tool] = []
# Les outils du NAVIGATEUR (Runs.BROWSER) : leurs déclarations, servies à
# la page (ui/page_tools.py) ; et leurs familles, dans l'ordre du menu.
PAGE: list[Tool] = []
FAMILIES: list[Family] = []


def register(tool: Tool, provider: str = "") -> Tool:
    """Ajoute un outil, s'il tient le contrat (faults) : au REGISTRE pour
    un outil du proxy, à PAGE pour un outil du navigateur. Le nom est la
    clé, pour les deux moteurs : deux outils ne le partagent pas.
    `provider` : celui qui FOURNIT l'outil (« mcp », « extension ») ; vide,
    c'est un outil natif (ToolName)."""
    errors = faults(tool, provider)
    if any(t.name == getattr(tool, "name", None) for t in (*REGISTRY, *PAGE)):
        errors.append(f"outil {tool.name!r} : nom déjà pris")
    if errors:
        raise ValueError("outil hébergé non conforme :\n  " + "\n  ".join(errors))
    (PAGE if tool.runs == Runs.BROWSER else REGISTRY).append(tool)
    return tool


def family_faults(family, provider: str = "", page_names=None, families=None) -> list[str]:
    """Ce qu'une famille a de travers : une clé hors de FamilyKey (native)
    ou prise ; sans outil ou sans libellé ; un outil qui n'est pas un outil
    du navigateur inscrit, ou qui est déjà dans une autre famille.
    `page_names`, `families` : à la place de PAGE et FAMILIES, pour
    vérifier avant d'inscrire (une extension, tout ou rien)."""
    if not isinstance(family, Family):
        return [f"{family!r} : pas une Family"]
    where = f"famille {family.key!r}"
    out = []
    native = set(FamilyKey)
    if not provider and family.key not in native:
        out.append(f"{where} : clé hors de FamilyKey (une famille native)")
    elif provider and (family.key in native or not isinstance(family.key, str)
                       or not contract.PROVIDED_NAME.match(family.key)):
        out.append(f"{where} : clé invalide pour une famille fournie")
    families = FAMILIES if families is None else families
    if any(f.key == family.key for f in families):
        out.append(f"{where} : clé déjà prise")
    if not family.tools or not family.label:
        out.append(f"{where} : sans outil ou sans libellé")
    if not provider and family.guidance:
        out.append(f"{where} : une famille native a sa consigne au catalogue "
                   f"(family.{family.key}.guidance), pas en argument")
    page_names = {t.name for t in PAGE} if page_names is None else set(page_names)
    taken = {n for f in families for n in f.tools}
    for name in family.tools:
        if name not in page_names:
            out.append(f"{where} : {name!r} n'est pas un outil du navigateur inscrit")
        elif name in taken or list(family.tools).count(name) > 1:
            out.append(f"{where} : {name!r} est déjà dans une famille")
    return out


def register_family(family: Family, provider: str = "") -> Family:
    """Ajoute une famille d'outils du navigateur, après ses outils."""
    if errors := family_faults(family, provider):
        raise ValueError("famille non conforme :\n  " + "\n  ".join(errors))
    FAMILIES.append(family)
    return family


register(web_search.TOOL)
register(web_fetch.TOOL)
register(ocr.TOOL)
register(web_grep.TOOL)
register(transcribe.TOOL)
register(code_execution.TOOL)
register(image_generation.TOOL)
register(memory.TOOL)
register(webhook.TOOL)
register(repository.TOOL)
for _tool in page.TOOLS:
    register(_tool)
for _family in page.FAMILIES:
    register_family(_family)

# Chaque outil natif de ToolName est inscrit, chaque famille de FamilyKey
# aussi, et chaque outil du navigateur est dans une famille : une entrée
# sans outil est une erreur de démarrage, pas un outil qui manque en
# silence.
if missing := set(ToolName) - {t.name for t in (*REGISTRY, *PAGE)}:
    raise ValueError(f"ToolName sans outil : {sorted(missing)}")
if missing := set(FamilyKey) - {f.key for f in FAMILIES}:
    raise ValueError(f"FamilyKey sans famille : {sorted(missing)}")
if loose := {t.name for t in PAGE} - {n for f in FAMILIES for n in f.tools}:
    raise ValueError(f"outils du navigateur sans famille : {sorted(loose)}")

# Appels d'outils hébergés exécutés pour UNE réponse. Au-delà, le modèle
# reçoit une erreur qui lui demande de conclure.
MAX_CALLS = config.integer("tools.max_calls", 8)
# Délai d'une exécution, tout compris (chaque outil a aussi le sien, plus
# court, sur ses requêtes).
RUN_TIMEOUT = config.num("tools.run_timeout", 60)
# Taille d'un résultat rendu au modèle.
MAX_RESULT_CHARS = config.integer("tools.max_result_chars", 24_000)
CACHE_ENTRIES = config.integer("tools.cache_entries", 512)
CACHE_TTL = config.num("tools.cache_ttl", 24 * 3600)

EXPIRED = ("[result no longer available: the proxy was restarted or the "
           "entry expired; run the tool again if you still need it]")


# Les DROITS de la clé qui fait la requête (accounts.py, posés par le
# middleware d'auth comme stats.CLIENT) : None = tous les outils actifs ;
# sinon les seuls qu'elle nomme — un outil par son nom, ou les outils d'un
# serveur MCP par `mcp:<serveur>` (ceux qu'il découvrira compris), ou de
# tous par `mcp`. Un outil refusé est pour cette requête un outil inactif :
# ni présenté, ni listé, ni exécutable.
GRANTED: contextvars.ContextVar[frozenset | None] = contextvars.ContextVar(
    "tool_rights", default=None)


def granted(tool: Tool, rights: frozenset | None) -> bool:
    """`tool` est-il permis par ces droits (None : tout l'est) ?"""
    if rights is None or tool.name in rights:
        return True
    return any(k in rights for k in tool.kinds
               if k == mcp.KIND or k.startswith(mcp.KIND + ":"))


def active(tool: Tool, offer: Offer = Offer.API) -> bool:
    """L'outil est-il en service ICI : actif (Tool.enabled), son extension
    allumée (extensions.active : l'interrupteur de la page), et offert à ce
    canal (Tool.offered) ? Ignore les droits de la requête."""
    return bool(tool.enabled) and extensions.active(tool) and offer in tool.offered


def enabled(offer: Offer = Offer.API) -> list[Tool]:
    """Les outils en service pour ce canal (`offer` : Offer.WEB pour le
    chat de la page et les webhooks), et permis à la clé de la requête
    (GRANTED)."""
    rights = GRANTED.get()
    return [t for t in REGISTRY if active(t, offer) and granted(t, rights)]


def serving() -> dict[str, list[str]]:
    """Les outils en service sur au moins un canal → leurs canaux, pour le
    journal de démarrage et /healthz. Ignore les droits de la requête."""
    out = {}
    for t in REGISTRY:
        if offers := [o.value for o in Offer if active(t, o)]:
            out[t.name] = offers
    return out


def grantable() -> list[dict]:
    """Ce qu'on peut permettre à une clé, pour la page : chaque outil
    natif ou d'extension offert à l'API (en service ou non : `enabled`),
    puis chaque serveur MCP de la configuration avec les outils qu'il a
    fait connaître. Un outil offert à la page seule n'y est pas : les
    droits d'une clé ne s'y appliquent pas. Ignore les droits de la
    requête."""
    out = [{"name": t.name, "enabled": active(t), "family": t.family}
           for t in REGISTRY
           if not isinstance(t, mcp.McpTool) and Offer.API in t.offered]
    servers: dict[str, list] = {}
    for t in REGISTRY:
        if isinstance(t, mcp.McpTool):
            servers.setdefault(t.server.name, []).append(
                {"name": t.name, "enabled": active(t)})
    for name in (s.name for s in mcp.SERVERS):
        servers.setdefault(name, [])
    return out + [{"name": f"{mcp.KIND}:{name}", "server": name,
                   "tools": found} for name, found in servers.items()]


def kinds() -> frozenset:
    """Tous les types d'outil que le registre sait héberger, actifs ou
    non : ce qu'une requête chat/completions peut déclarer dans `tools`.
    Plus ceux des serveurs MCP de la configuration, dont les outils
    n'entrent au registre qu'une fois découverts (mcp.kinds)."""
    return frozenset(k for t in REGISTRY for k in t.kinds) | mcp.kinds()


class Memory:
    """Résultats des appels hébergés, par client et identifiant d'élément :
    de quoi rejouer l'appel À L'IDENTIQUE au tour suivant. LRU bornée, avec
    durée.

    `owner` (voir owner()) fait partie de la CLÉ : un identifiant présenté
    par un autre client ne trouve rien. La borne est commune à tous."""

    def __init__(self, entries: int, ttl: float):
        self.entries, self.ttl = entries, ttl
        self._data: OrderedDict[tuple[str, str], tuple[float, dict]] = \
            OrderedDict()

    def store(self, item_id: str, name: str, arguments: str, result: str,
              owner: str = "") -> None:
        key = (owner, item_id)
        self._data[key] = (time.monotonic(), {
            "name": name, "arguments": arguments, "result": result})
        self._data.move_to_end(key)
        while len(self._data) > self.entries:
            self._data.popitem(last=False)

    def recall(self, item_id: str, owner: str = "") -> dict | None:
        key = (owner, item_id)
        entry = self._data.get(key)
        if entry is None:
            return None
        if time.monotonic() - entry[0] > self.ttl:
            del self._data[key]
            return None
        self._data.move_to_end(key)
        return entry[1]

    def __len__(self) -> int:
        return len(self._data)

    def clear(self) -> None:
        self._data.clear()


MEMORY = Memory(CACHE_ENTRIES, CACHE_TTL)


class Hosted:
    """Ce que les trois surfaces reçoivent de ce paquet : quels outils
    présenter au modèle pour ce que le client déclare, l'exécution bornée
    d'un appel, et la mémoire des résultats."""

    def __init__(self, tools=None, memory: Memory | None = None,
                 offer: Offer = Offer.API):
        """`offer` : le canal de la requête (Offer.WEB pour le chat de la
        page et les webhooks) ; `tools` le remplace (tests)."""
        self.tools = list(enabled(offer) if tools is None else tools)
        self.memory = MEMORY if memory is None else memory
        self.by_name = {t.name: t for t in self.tools}
        self.expired = EXPIRED

    def __bool__(self) -> bool:
        return bool(self.tools)

    def for_kind(self, kind: str) -> list:
        """Les outils que ce type DÉCLARE dans `tools` d'une requête
        chat/completions (Tool.kinds)."""
        return [t for t in self.tools if kind in t.kinds]

    def for_responses(self, kind: str) -> list:
        """Les outils que ce type d'outil Responses active : ceux qui ont
        la liaison (sans élément, rien à rendre au client)."""
        return [t for t in self.tools
                if t.responses and kind in t.responses.kinds]

    def for_item(self, item: dict):
        """L'outil qui a produit cet élément Responses rejoué
        (web_search_call…), d'après son action — pour le reconstruire si
        la mémoire l'a perdu."""
        for t in self.tools:
            if t.responses and t.responses.item == item.get("type") \
                    and isinstance(item.get("action"), dict) \
                    and t.summary({}).get("type") == item["action"].get("type"):
                return t
        return None

    def for_server(self, kind: str):
        """L'outil que remplace ce type d'outil serveur Anthropic, toutes
        versions datées (`web_search_20250305` → préfixe `web_search`)."""
        return next((t for t in self.tools if t.anthropic and re.fullmatch(
            re.escape(t.anthropic.prefix) + r"_\d+", kind)), None)

    def own(self, name: str) -> bool:
        """Cet outil a-t-il son PROPRE compte d'appels (Tool.max_calls) ?
        Ses appels ne pèsent alors pas sur le plafond commun, ni ceux des
        autres sur le sien."""
        tool = self.by_name.get(name)
        return tool is not None and tool.max_calls is not None

    def cap(self, limit=None, name: str | None = None) -> int:
        """Appels exécutés au plus pour une réponse : le plafond de
        l'outil `name` s'il a le sien (Tool.max_calls), sinon MAX_CALLS,
        commun — ou la limite que le client a demandée (`max_uses` d'un
        outil serveur Anthropic) si elle est plus basse, jamais plus
        haute."""
        ceiling = self.by_name[name].max_calls if name and self.own(name) \
            else MAX_CALLS
        if isinstance(limit, int) and not isinstance(limit, bool):
            return max(min(limit, ceiling), 0)
        return max(ceiling, 0)

    async def run(self, name: str, arguments: str, used: int,
                  limit: int | None = None, options: dict | None = None,
                  endpoint: str = "", model: str = "",
                  client: str = "", session: str = "",
                  hosts: frozenset | None = None) -> Result:
        """Exécute la fonction `name`. `used` : appels déjà exécutés pour
        cette réponse AU COMPTE dont relève l'outil (own()) ; `limit` : voir
        cap(). `options` : ce que le CLIENT a réglé sur son outil, par nom
        de fonction — l'outil le reçoit dans `call.settings`, à part des
        arguments du modèle, qui ne peut donc pas s'en affranchir. Le reste
        (`endpoint`, `model` préfixé, `client`, `session`) complète `call`.
        Ne lève jamais : tout échec est un Result, avec son code.

        Tous les chemins passent par ici : la ligne de statistiques s'écrit
        donc ici, une fois — des mesures, jamais les arguments ni le
        résultat."""
        tool = self.by_name.get(name)
        if tool is None:
            # Pas de ligne de statistiques : ce nom n'est celui d'aucun
            # outil, c'est un texte venu du modèle ou du client.
            return failure("invalid_input", f"unknown tool {name}.")
        started = time.monotonic()
        result = self._refusal(tool, used, limit)
        if result is None:
            result = await self._execute(tool, arguments, Call(
                (options or {}).get(name, {}), endpoint, model, client,
                session, hosts))
        # L'issue des statistiques : le code, pas le texte. `limit` : le
        # refus d'ici, ou celui que l'outil prononce lui-même — sans durée.
        outcome = "ok" if result.error is None \
            else "limit" if result.error == "limit" else "error"
        try:
            stats.record_tool(name, endpoint, model, outcome,
                              time.monotonic() - started if outcome != "limit"
                              else 0.0, len(result.text))
        except Exception:  # les statistiques ne cassent jamais un outil
            log.exception("stats : exécution de %s non enregistrée", name)
        return result

    def _refusal(self, tool: Tool, used: int, limit) -> Result | None:
        """Le refus par limite d'appels (cap()), ou None s'il reste de la
        marge. Le texte nomme ce qui est compté : les appels de CET outil
        s'il a son compte, sinon ceux du plafond commun (« web tool
        calls » pour un outil web, « tool calls » pour un autre)."""
        cap = self.cap(limit, tool.name)
        if used >= cap:
            counted = f"{tool.name} calls" if self.own(tool.name) \
                else f"{tool.family} tool calls".lstrip()
            return failure("limit", (
                f"the limit of {cap} {counted} for one "
                f"answer is reached. Answer now with what you already have."))
        return None

    async def _execute(self, tool: Tool, arguments: str, call: Call) -> Result:
        name = tool.name
        try:
            args = json.loads(arguments or "{}")
        except (ValueError, TypeError):  # TypeError : pas une chaîne
            args = None
        if not isinstance(args, dict):
            return failure("invalid_input",
                           "the tool arguments are not a JSON object.")
        started = time.monotonic()
        delay = RUN_TIMEOUT if tool.timeout is None else tool.timeout
        try:
            result = await asyncio.wait_for(tool.run(args, call), delay)
            if not isinstance(result, Result):
                raise TypeError(f"{name}.run n'a pas rendu un Result")
        except ToolError as exc:  # l'échec prévu : son code, son message
            result = failure(exc.code, exc.message)
        except asyncio.TimeoutError:
            result = failure(
                "timeout", f"{name} timed out after {int(delay)} s.")
        except Exception as exc:  # un outil ne doit jamais casser la réponse
            log.exception("outil hébergé %s en échec", name)
            result = failure("unavailable",
                             f"{name} failed ({type(exc).__name__}).")
        if result.error is not None and result.error not in ERRORS:
            log.warning("outil hébergé %s : code d'erreur %r hors contrat, "
                        "rendu `unavailable`", name, result.error)
            result = dataclasses.replace(result, error="unavailable")
        if len(result.text) > MAX_RESULT_CHARS:
            # Seul le texte est borné : `sources`, `meta` et `files` disent
            # ce que l'outil a trouvé ou produit, coupé ou non.
            result = dataclasses.replace(
                result, text=result.text[:MAX_RESULT_CHARS] + "\n[truncated]")
        # Le texte d'une erreur est journalisé : sur la surface Anthropic
        # le client n'en reçoit qu'un code, seul le modèle lit le détail.
        # Incognito : ni arguments (la recherche, l'URL, le code) ni texte
        # dans les logs — le nom, la taille, la durée et le code seuls.
        hidden = INCOGNITO.get()
        log.info("outil hébergé %s(%s) → %d car. en %.1fs%s", name,
                 "incognito" if hidden else str(arguments)[:160],
                 len(result.text), time.monotonic() - started,
                 (f" — {result.error}" if hidden
                  else f" — {result.error} : {result.text[:200]}")
                 if result.error else "")
        return result
