"""
Les outils HÉBERGÉS du proxy : ceux qu'il exécute lui-même, au lieu de
rendre la main au client. Un client de l'API Responses (Codex CLI)
déclare `{"type": "web_search"}` en comptant qu'OpenAI fera la recherche ;
derrière ce proxy il n'y a pas d'OpenAI — c'est donc ici qu'elle se fait.
Même chose pour un client de l'API Messages (Claude Code) et son outil
serveur `{"type": "web_search_20250305"}`, qu'Anthropic exécuterait.

Un outil = un module de ce dossier, qui expose :
  NAME        le nom de la fonction présentée au modèle
  KINDS       les types d'outil Responses qui l'activent
  ITEM_TYPE   l'élément Responses qui rend compte de l'appel au client
  DEFINITION  la fonction, à la forme chat/completions
  ENABLED     lu dans [tools.<nom>] de config.toml
  action(args) → dict      ce que l'élément dit de l'appel (requête, URL)
  async run(args) → str    l'exécution ; ne lève pas : une erreur est un
                           texte «Error: …» rendu au modèle, qui s'adapte

Et, pour un outil dont le résultat n'est pas qu'un texte ou dont
l'élément n'a pas d'`action` (`image_generation`), FACULTATIFS :
  item(args, résultat) → dict   les champs de l'élément terminé, à la
                           place d'`action` — `status` compris. `run` peut
                           alors rendre une SOUS-CLASSE de `str` : le
                           texte du modèle, qui porte ce que l'élément
                           doit montrer au client (l'image). Tout le
                           reste n'y voit qu'un texte ; la mémoire range
                           `str(résultat)`, le texte nu
  replay(élément) → (arguments, résultat)   l'appel reconstruit d'un
                           élément rejoué que la mémoire a perdu, là où
                           les autres le sont depuis leur `action`
  options(outil) → dict    ce que le client a réglé sur SA déclaration
                           de l'outil, passé à `run` en mots-clés
  RUN_TIMEOUT, MAX_CALLS   délai d'une exécution et nombre d'appels de
                           cette fonction par réponse, à la place des
                           réglages communs
  DIRECT = False           pas d'appel direct par /v1/tools

Ajouter un outil : un module, une ligne dans MODULES, une table
[tools.<nom>] dans config.example.toml.

La surface Anthropic ne se sert que de `web_search`, et lui demande en
plus `definition(fetch=False)` (la fonction sans renvoi à `web_fetch`,
qu'elle ne présente pas), `parse` et `render` (texte ↔ liste structurée,
pour les blocs `web_search_result`), et que `run` accepte les listes de
domaines du client.

Ce module porte ce qui est commun : le registre, l'exécution bornée
(délai, taille du résultat), la ligne de STATISTIQUES de chaque exécution
(stats.record_tool, depuis Hosted.run : des mesures, jamais le contenu)
et la MÉMOIRE des résultats. Cette mémoire
est la seule chose que le proxy conserve entre deux requêtes : le client
renvoie au tour suivant l'élément `web_search_call` SANS son résultat
(OpenAI le garde côté serveur), et il faut le rendre au modèle à
l'identique — sinon il perd ce qu'il a lu, et le préfixe change sous un
backend à cache. Bornée (entrées, durée), en mémoire : un redémarrage du
proxy l'oublie, le modèle reçoit alors un mot qui le dit. La surface
Anthropic n'y range RIEN : son client renvoie le résultat avec l'appel
(blocs `web_search_tool_result`), le texte se reconstruit de là.

La boucle qui relance le backend après un appel vit dans app.py ; la
traduction des éléments, dans responses_api.py et anthropic_api.py — qui
ne connaissent de ce paquet que l'objet `Hosted` qu'on leur passe.
"""

import asyncio
import json
import time
from collections import OrderedDict

from .. import config, stats
from ..settings import log
from . import image_generation, web_fetch, web_search

MODULES = (web_search, web_fetch, image_generation)

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


def enabled() -> list:
    return [m for m in MODULES if m.ENABLED]


def direct(modules) -> list:
    """Ceux de `modules` qui s'appellent aussi par /v1/tools : tous, sauf
    ceux dont le résultat n'est pas un texte (`DIRECT = False`)."""
    return [m for m in modules if getattr(m, "DIRECT", True)]


class Memory:
    """Résultats des appels hébergés, par identifiant d'élément : ce qu'il
    faut pour rejouer l'appel À L'IDENTIQUE au tour suivant (nom, arguments
    tels que le modèle les a écrits, résultat). LRU bornée, avec durée."""

    def __init__(self, entries: int, ttl: float):
        self.entries, self.ttl = entries, ttl
        self._data: OrderedDict[str, tuple[float, dict]] = OrderedDict()

    def store(self, item_id: str, name: str, arguments: str, result: str) -> None:
        self._data[item_id] = (time.monotonic(), {
            "name": name, "arguments": arguments, "result": result})
        self._data.move_to_end(item_id)
        while len(self._data) > self.entries:
            self._data.popitem(last=False)

    def recall(self, item_id: str) -> dict | None:
        entry = self._data.get(item_id)
        if entry is None:
            return None
        if time.monotonic() - entry[0] > self.ttl:
            del self._data[item_id]
            return None
        self._data.move_to_end(item_id)
        return entry[1]

    def __len__(self) -> int:
        return len(self._data)


MEMORY = Memory(CACHE_ENTRIES, CACHE_TTL)


class Hosted:
    """Ce que responses_api et anthropic_api reçoivent de ce paquet :
    quelles fonctions présenter au modèle pour un type d'outil du client,
    comment rendre compte d'un appel, et la mémoire des résultats."""

    def __init__(self, modules=None, memory: Memory | None = None):
        self.modules = list(enabled() if modules is None else modules)
        self.memory = MEMORY if memory is None else memory
        self.by_name = {m.NAME: m for m in self.modules}
        # Résultat rendu au modèle pour un appel rejoué que la mémoire a perdu.
        self.expired = EXPIRED

    def __bool__(self) -> bool:
        return bool(self.modules)

    def for_kind(self, kind: str) -> list:
        return [m for m in self.modules if kind in m.KINDS]

    def for_item(self, item: dict):
        """Le module qui a produit cet élément rejoué (web_search_call…),
        d'après son action — pour le reconstruire si la mémoire l'a perdu.
        Un module à `replay` est seul de son type d'élément : pas d'action
        à départager."""
        for m in self.modules:
            if m.ITEM_TYPE != item.get("type"):
                continue
            if hasattr(m, "replay"):
                return m
            if isinstance(item.get("action"), dict) \
                    and m.action({}).get("type") == item["action"].get("type"):
                return m
        return None

    def cap(self, limit=None) -> int:
        """Appels exécutés au plus pour une réponse : MAX_CALLS, ou la
        limite que le client a demandée (`max_uses` d'un outil serveur
        Anthropic) si elle est plus basse — jamais plus haute."""
        if isinstance(limit, int) and not isinstance(limit, bool):
            return max(min(limit, MAX_CALLS), 0)
        return MAX_CALLS

    async def run(self, name: str, arguments: str, used: int,
                  limit: int | None = None, options: dict | None = None,
                  same: int = 0, endpoint: str = "", model: str = "") -> str:
        """Exécute la fonction `name`. `used` : appels déjà exécutés pour
        cette réponse, `same` : ceux de CETTE fonction (un module peut
        avoir sa limite, `MAX_CALLS`) ; `limit` : voir cap(). `options` : ce que le CLIENT
        a réglé sur son outil, par nom de fonction (les listes de domaines
        de l'outil serveur Anthropic) — passé au module en plus des
        arguments du modèle, qui ne peut donc pas s'en affranchir.
        `endpoint` / `model` : la route par où l'appel arrive et le modèle
        PRÉFIXÉ de la conversation (aucun pour l'appel direct) — ils ne
        servent qu'à la ligne de statistiques.
        Ne lève jamais : tout échec est un texte.

        C'est LE point par où passent les trois chemins (boucles de
        /v1/responses et /v1/messages, appel direct /v1/tools) : la ligne
        de statistiques de l'exécution s'écrit donc ici, une fois. Elle
        ne retient que des mesures — nom, route, modèle, issue, durée,
        taille du résultat — jamais les arguments ni le résultat."""
        module = self.by_name.get(name)
        if module is None:
            # Pas de ligne de statistiques : ce nom n'est celui d'aucun
            # outil, c'est un texte venu du modèle ou du client.
            return f"Error: unknown tool {name}."
        started = time.monotonic()
        result = self._refusal(module, used, limit, same)
        outcome = "limit"
        if result is None:
            result = await self._execute(module, arguments, options)
            outcome = "error" if result.startswith("Error:") else "ok"
        try:
            stats.record_tool(name, endpoint, model, outcome,
                              time.monotonic() - started if outcome != "limit"
                              else 0.0, len(result))
        except Exception:  # les statistiques ne cassent jamais un outil
            log.exception("stats : exécution de %s non enregistrée", name)
        return result

    def _refusal(self, module, used: int, limit, same: int) -> str | None:
        """Le refus par limite d'appels, ou None s'il reste de la marge :
        la limite de la réponse (cap()), puis celle du module."""
        cap = self.cap(limit)
        if used >= cap:
            return (f"Error: the limit of {cap} web tool calls for one "
                    f"answer is reached. Answer now with what you already have.")
        own = getattr(module, "MAX_CALLS", None)
        if own is not None and same >= own:
            return (f"Error: the limit of {own} {module.NAME} calls for one "
                    f"answer is reached. Answer now with what you already have.")
        return None

    async def _execute(self, module, arguments: str, options) -> str:
        name = module.NAME
        try:
            args = json.loads(arguments or "{}")
        except (ValueError, TypeError):  # TypeError : pas une chaîne
            args = None
        if not isinstance(args, dict):
            return "Error: the tool arguments are not a JSON object."
        # Le délai commun coupe une recherche qui traîne ; une génération
        # d'image a le sien, bien plus long.
        timeout = getattr(module, "RUN_TIMEOUT", RUN_TIMEOUT)
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                module.run(args, **(options or {}).get(name, {})), timeout)
        except asyncio.TimeoutError:
            result = f"Error: {name} timed out after {int(timeout)} s."
        except Exception as exc:  # un outil ne doit jamais casser la réponse
            log.exception("outil hébergé %s en échec", name)
            result = f"Error: {name} failed ({type(exc).__name__})."
        if len(result) > MAX_RESULT_CHARS:
            result = result[:MAX_RESULT_CHARS] + "\n[truncated]"
        # Le texte d'une erreur est journalisé : sur la surface Anthropic
        # le client n'en reçoit qu'un code, seul le modèle lit le détail.
        log.info("outil hébergé %s(%s) → %d car. en %.1fs%s", name,
                 str(arguments)[:160], len(result), time.monotonic() - started,
                 f" — {result[:200]}" if result.startswith("Error:") else "")
        return result
