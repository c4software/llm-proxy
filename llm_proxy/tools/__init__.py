"""
Les outils HÉBERGÉS du proxy : ceux qu'il exécute lui-même, au lieu de
rendre la main au client. Un client de l'API Responses (Codex CLI)
déclare `{"type": "web_search"}` en comptant qu'OpenAI fera la recherche ;
derrière ce proxy il n'y a pas d'OpenAI — c'est donc ici qu'elle se fait.
Même chose pour un client de l'API Messages (Claude Code) et son outil
serveur `{"type": "web_search_20250305"}`, qu'Anthropic exécuterait — ou
`{"type": "web_fetch_20250910"}`, pour un client du SDK.

Un outil = un OBJET qui tient le contrat de contract.py (docs/outils.md
le décrit membre par membre, avec un exemple complet) :
  name                    le nom de la fonction présentée au modèle
  enabled                 actif ? ([tools.<nom>].enabled pour ceux d'ici)
  spec(present) → dict    la fonction, à la forme chat/completions ;
                          `present` : les outils présentés avec lui
  async run(args, call) → Result
                          l'exécution. `call` (Call) porte ce qui ne
                          vient pas du modèle : réglages du client, route,
                          modèle. Rend un Result — `text` pour le modèle,
                          `error` (un code d'ERRORS, None = succès),
                          `sources`, `meta`, `files` — ou lève ToolError(code,
                          message). Une erreur est un texte «Error: …»
                          que le modèle lit, et s'adapte
  summary(args, result) → dict
                          ce que le client affiche de l'appel
  timeout, max_calls      son délai et son nombre d'appels par réponse,
                          s'il a les siens (None = ceux de [tools])
  responses, anthropic    ses LIAISONS aux protocoles, en données : quels
                          types d'outil l'activent, quel élément ou quel
                          bloc rend compte de l'appel. Sans liaison,
                          l'outil reste exécutable par /v1/tools et
                          présentable sur /v1/chat/completions

Ajouter un outil : une classe, `register(...)`, une table [tools.<nom>]
dans config.example.toml — voir docs/outils.md, « Écrire un outil ».
mcp.py, lui, est un FOURNISSEUR : il apporte au registre, après l'import
(au démarrage de l'application, puis à chaque découverte), les outils des
serveurs MCP de la configuration.

Ce module porte ce qui est commun : le REGISTRE (register, enabled),
l'exécution bornée (Hosted.run : nombre d'appels, délai, taille du
résultat — rien n'en remonte, tout échec est un Result), la ligne de
STATISTIQUES de chaque exécution (stats.record_tool, depuis Hosted.run :
des mesures, jamais le contenu) et la MÉMOIRE des résultats. Cette
mémoire est, avec celle des échanges cachés de chat_api (même logique,
pour un client /v1/chat/completions), tout ce que le proxy conserve entre
deux requêtes — et elle ne garde que du TEXTE : le client renvoie au tour
suivant l'élément `web_search_call` SANS son résultat (OpenAI le garde
côté serveur), et il faut le rendre au modèle à l'identique — sinon il
perd ce qu'il a lu, et le préfixe change sous un backend à cache. Bornée
(entrées, durée), en mémoire : un redémarrage du proxy l'oublie, le
modèle reçoit alors un mot qui le dit. CLOISONNÉE par client : une entrée
ne se relit qu'avec le condensé de la clé qui l'a rangée (`owner`),
l'identifiant de l'élément ne suffit pas. La surface Anthropic n'y range
RIEN : son client renvoie le résultat avec l'appel (blocs
`web_search_tool_result` et `web_fetch_tool_result`), le texte se
reconstruit de là.

La boucle qui relance le backend après un appel vit dans app.py ; la
traduction des éléments, dans responses_api.py, anthropic_api.py et
chat_api.py — qui ne connaissent de ce paquet que l'objet `Hosted` qu'on
leur passe, et le contrat.
"""

import asyncio
import dataclasses
import hashlib
import json
import os
import re
import time
from collections import OrderedDict

from .. import config, stats
from ..settings import log
from . import (mcp, net, ocr, transcribe, web_fetch,  # noqa: F401
               web_search, webcache)
# Le contrat, tel que le reste du proxy et un outil l'importent d'ici.
from .contract import (ERRORS, Anthropic, Artifact, Call,  # noqa: F401
                       Responses, Result, Source, Tool, ToolError, failure)

# Le registre : tous les outils que le proxy SAIT héberger, actifs ou non,
# dans l'ordre où ils sont présentés au modèle.
REGISTRY: list[Tool] = []


def register(tool: Tool) -> Tool:
    """Ajoute un outil au registre. Un fournisseur qui en apporte
    plusieurs (découverts au démarrage) les enregistre un à un. Le nom
    est la clé : deux outils ne le partagent pas."""
    if not tool.name or any(t.name == tool.name for t in REGISTRY):
        raise ValueError(f"outil hébergé sans nom, ou nom déjà pris : "
                         f"{tool.name!r}")
    REGISTRY.append(tool)
    return tool


register(web_search.TOOL)
register(web_fetch.TOOL)
register(ocr.TOOL)
register(transcribe.TOOL)

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


def enabled() -> list[Tool]:
    return [t for t in REGISTRY if t.enabled]


def kinds() -> frozenset:
    """Tous les types d'outil que le registre sait héberger, actifs ou
    non : ce qu'une requête chat/completions peut déclarer dans `tools`.
    Plus ceux des serveurs MCP de la configuration, dont les outils
    n'entrent au registre qu'une fois découverts (mcp.kinds)."""
    return frozenset(k for t in REGISTRY for k in t.kinds) | mcp.kinds()


# Sel du condensé des clés clientes : tiré à chaque démarrage, jamais
# écrit. La mémoire ne survit pas au processus, son cloisonnement non plus
# n'a pas à le faire — et un condensé sorti d'ici ne dit rien de la clé.
_OWNER_SALT = os.urandom(16)


def owner(token: str) -> str:
    """Le client, pour la mémoire : un condensé de la clé qu'il présente
    au proxy, JAMAIS la clé. «» (pas de clé) reste «» : c'est le client
    unique d'un proxy ouvert."""
    if not token:
        return ""
    return hashlib.blake2b(token.encode("utf-8", "surrogatepass"),
                           key=_OWNER_SALT, digest_size=16).hexdigest()


class Memory:
    """Résultats des appels hébergés, par client et identifiant d'élément :
    ce qu'il faut pour rejouer l'appel À L'IDENTIQUE au tour suivant (nom,
    arguments tels que le modèle les a écrits, résultat). LRU bornée, avec
    durée.

    `owner` : le client (voir owner() — un condensé, «» pour un proxy
    ouvert). Il fait partie de la CLÉ : un identifiant d'élément présenté
    par un autre client ne trouve rien, exactement comme un identifiant
    inconnu. La borne, elle, est commune à tous les clients."""

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


MEMORY = Memory(CACHE_ENTRIES, CACHE_TTL)


class Hosted:
    """Ce que les trois surfaces reçoivent de ce paquet : quels outils
    présenter au modèle pour ce que le client déclare, l'exécution bornée
    d'un appel, et la mémoire des résultats."""

    def __init__(self, tools=None, memory: Memory | None = None):
        self.tools = list(enabled() if tools is None else tools)
        self.memory = MEMORY if memory is None else memory
        self.by_name = {t.name: t for t in self.tools}
        # Résultat rendu au modèle pour un appel rejoué que la mémoire a perdu.
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
                  client: str = "", session: str = "") -> Result:
        """Exécute la fonction `name`. `used` : appels déjà exécutés pour
        cette réponse AU COMPTE dont relève l'outil — le sien s'il en a un
        (own()), sinon le compte commun ; `limit` : voir cap(). `session` :
        l'identifiant de conversation de la surface, «» si elle n'en a
        pas. `options` : ce que le CLIENT
        a réglé sur son outil, par nom de fonction (les listes de domaines
        et la taille de contenu des outils serveur Anthropic) — l'outil
        les reçoit dans `call.settings`, à côté des arguments du modèle,
        qui ne peut donc pas s'en affranchir. `endpoint` / `model` /
        `client` : la route par où l'appel arrive, le modèle PRÉFIXÉ de
        la conversation (aucun pour l'appel direct) et le condensé de la
        clé du client — le reste de `call`.
        Ne lève jamais : tout échec est un Result, avec son code.

        C'est LE point par où passent tous les chemins (boucles de
        /v1/responses, /v1/messages et /v1/chat/completions, appel direct
        /v1/tools) : la ligne de statistiques de l'exécution s'écrit donc
        ici, une fois. Elle ne retient que des mesures — nom, route,
        modèle, issue, durée, taille du résultat — jamais les arguments
        ni le résultat."""
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
                session))
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
        # Le délai de l'outil s'il a le sien (Tool.timeout), sinon le commun.
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
        log.info("outil hébergé %s(%s) → %d car. en %.1fs%s", name,
                 str(arguments)[:160], len(result.text),
                 time.monotonic() - started,
                 f" — {result.error} : {result.text[:200]}"
                 if result.error else "")
        return result
