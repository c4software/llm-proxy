"""
Les serveurs de la configuration, ensemble : leur chargement, leur
démarrage et leur arrêt avec le proxy, et ce que le reste du proxy en lit
(types de déclaration, état).
"""

import asyncio

from ...core import config
from ...core.settings import log

from .protocol import KIND, STARTUP_WAIT
from .server import Server


def load(table) -> list[Server]:
    """[tools.mcp] → les serveurs : chaque sous-table en est un, sous son
    nom ; les valeurs simples de la table sont les réglages communs."""
    if not isinstance(table, dict):
        return []
    return [Server(name, t) for name, t in table.items() if isinstance(t, dict)]


SERVERS: list[Server] = load(config.get("tools.mcp", {}))


_TASKS: list[asyncio.Task] = []


_RUNNING: list[Server] = []


async def start(servers=None, transport=None) -> None:
    """À appeler au DÉMARRAGE de l'application, avant qu'elle n'annonce
    ses outils. Ouvre les serveurs actifs, lance leur découverte et
    l'attend STARTUP_WAIT secondes au plus : passé ce délai, ou pour un
    serveur éteint, le proxy démarre sans ses outils, qui s'enregistrent
    quand il répond. Ne lève pas pour un serveur injoignable."""
    await stop()
    for server in SERVERS if servers is None else servers:
        if not server.enabled:
            continue
        if server.missing:
            log.warning("MCP %s : en-tête(s) %s sans valeur (variable "
                        "d'environnement absente ?) — non envoyé(s)",
                        server.name, ", ".join(server.missing))
        server.open(transport)
        _RUNNING.append(server)
        _TASKS.append(asyncio.create_task(server.watch(),
                                          name=f"mcp-{server.name}"))
    if not _RUNNING:
        return
    try:
        await asyncio.wait_for(
            asyncio.gather(*(s.first.wait() for s in _RUNNING)), STARTUP_WAIT)
    except asyncio.TimeoutError:
        log.warning("MCP : %s sans réponse après %d s — le proxy démarre, "
                    "leurs outils viendront",
                    ", ".join(s.name for s in _RUNNING if not s.first.is_set()),
                    int(STARTUP_WAIT))


async def stop() -> None:
    """À appeler à l'ARRÊT de l'application : arrête les découvertes,
    ferme les sessions et les clients. Les outils restent au registre,
    désactivés."""
    for task in _TASKS:
        task.cancel()
    await asyncio.gather(*_TASKS, return_exceptions=True)
    for server in _RUNNING:
        await server.close()
    _TASKS.clear()
    _RUNNING.clear()


def kinds() -> frozenset:
    """Les types de déclaration des serveurs de la CONFIGURATION, qu'ils
    aient répondu ou non : `mcp`, et `mcp:<serveur>` pour chacun. Un
    client qui déclare `{"type": "mcp"}` alors qu'aucun serveur n'a encore
    été joint reçoit ainsi le refus du proxy, qui dit pourquoi, et non
    celui d'un backend à qui ce type ne dit rien."""
    if not SERVERS:
        return frozenset()
    return frozenset({KIND, *(f"{KIND}:{s.name}" for s in SERVERS)})


def status() -> list[dict]:
    """L'état des serveurs, pour un tableau de bord ou /healthz : jamais
    les en-têtes, ni la requête de l'URL."""
    return [{"name": s.name, "url": s.where, "enabled": s.enabled,
             "up": s.up, "error": s.error, "protocol": s.version if s.era
             else None, "era": s.era,
             "tools": [t.name for t in s.live()]} for s in SERVERS]
