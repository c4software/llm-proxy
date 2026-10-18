"""
L'OUTIL hébergé que devient chaque outil d'un serveur MCP (McpTool) :
il tient le contrat de tools/contract.py, son état propre déclaré dans
`extra`.
"""

import asyncio
import copy
from typing import TYPE_CHECKING

from ..contract import Call, Result, Tool, ToolError

from .protocol import (
    GRACE,
    HEADER_MISMATCH,
    INVALID_PARAMS,
    KIND,
    METHOD_NOT_FOUND,
    Failure,
    header_value,
    render,
)

if TYPE_CHECKING:          # l'annotation seule : server.py importe ce module
    from .server import Server


class McpTool(Tool):
    """Un outil d'un serveur MCP, en outil hébergé. L'objet vit autant
    que le proxy : sa définition suit la liste du serveur (`define`), et
    `live` dit s'il y figure encore."""

    # Son état, propre à un outil fourni : ajouté à l'interface (Tool.extra).
    extra = frozenset({"server", "remote", "live", "description", "schema", "params", "define"})

    def __init__(self, server: "Server", remote: str, name: str):
        self.server, self.remote, self.name = server, remote, name
        self.live = False
        self.description, self.schema, self.params = "", {"type": "object"}, ()

    @property
    def enabled(self) -> bool:
        return self.live and self.server.enabled

    @property
    def timeout(self) -> float:
        """Celui du serveur, tenu dans run(), plus de quoi rendre son
        erreur."""
        return self.server.timeout + GRACE

    @property
    def kinds(self) -> tuple[str, ...]:
        """Ce qui le déclare dans `tools` d'une requête chat/completions :
        son nom, `mcp:<serveur>` (tous les outils du serveur) ou `mcp`
        (tous ceux de tous les serveurs)."""
        return (self.name, f"{KIND}:{self.server.name}", KIND)

    def define(self, description: str, schema: dict, params: tuple) -> None:
        self.description, self.schema, self.params = description, schema, params

    def prompt(self, present) -> str:
        """La description du SERVEUR, bornée (`description_chars`) : un
        texte tiers, que le proxy n'écrit pas."""
        return self.description

    def parameters(self, present) -> dict:
        """Le schéma du serveur — une copie : la définition vit autant
        que le proxy."""
        return copy.deepcopy(self.schema)

    def _headers(self, args: dict) -> dict:
        """Les `Mcp-Param-<nom>` de cet appel : la valeur lue au chemin
        exact de la propriété annotée ; absente ou nulle, pas d'en-tête."""
        headers = {}
        for path, name in self.params:
            value = args
            for step in path:
                value = value.get(step) if isinstance(value, dict) else None
            if value is None:
                continue
            if not isinstance(value, (str, int)) or (
                    isinstance(value, int) and abs(value) >= 2 ** 53):
                raise ToolError("invalid_input", (
                    f"`{'.'.join(path)}` must be a string, an integer or "
                    f"a boolean."))
            headers[f"Mcp-Param-{name}"] = header_value(value)
        return headers

    async def run(self, args: dict, call: Call) -> Result:
        """Les arguments partent tels que le modèle les a écrits : c'est
        le serveur qui les valide (aucun validateur JSON Schema ici), et
        son refus revient en erreur que le modèle lit."""
        server = self.server
        try:
            async with asyncio.timeout(server.timeout):
                result = await server.request(
                    "tools/call", {"name": self.remote, "arguments": args},
                    self._headers(args))
        except TimeoutError:
            raise ToolError("timeout", f"{self.name} timed out after "
                                       f"{int(server.timeout)} s.")
        except Failure as exc:
            # Outil inconnu, paramètres ou en-têtes refusés : la liste a
            # peut-être changé chez le serveur — la redemander sans
            # attendre la période.
            if exc.rpc in (METHOD_NOT_FOUND, INVALID_PARAMS, HEADER_MISMATCH):
                server.changed()
            raise ToolError(exc.code, exc.message)
        return render(result, server.name, self.remote)
