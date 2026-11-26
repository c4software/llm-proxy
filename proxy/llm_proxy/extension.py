"""
Le CONTRAT PUBLIC des extensions : le seul module que le .py d'une
extension importe (`from llm_proxy import extension as ext`). Une
extension fournit des outils au même contrat que les outils natifs
(docs/outils.md, « Écrire un outil ») : un nom, un prompt (`Tool.prompt`,
la description de sa fonction), un schéma (`Tool.parameters`), et une
exécution — `Tool.run` pour un outil du proxy, le .js de l'extension pour
un outil du navigateur (BrowserTool).

Ce qui est ici est stable (API, version du contrat) ; le reste du paquet
est interne et peut changer. Le chargement : tools/extensions.py ; la
page : docs/extensions.md.

  Tool, Result, Call, ToolError, Source, Artifact, ERRORS,
  Offer, Runs           le contrat ; Offer : où un outil est offert
                        (Tool.offered : api, web) ; Runs : où il
                        s'exécute (Tool.runs : proxy, browser)
  BrowserTool, Family   un outil du navigateur (sa déclaration ; son
                        exécution est dans <nom>.js) et sa famille du
                        menu Outils
  download, post        le réseau du proxy : listes de refus de
                        [tools.net], adresses publiques seulement
                        (tools/net.py) ; post(method=…, auth=…) pour
                        une API qui demande un jeton et rend ses statuts
  settings(name)        la table [extensions.<name>] de la configuration
  logger(name)          le journal du proxy, au nom de l'extension
  API                   la version de ce contrat ; une extension qui
                        déclare REQUIRES plus haut n'est pas chargée
"""

import logging

from .core import config
from .core.settings import log
from .tools.contract import (ERRORS, Artifact, BrowserTool, Call, Family, Offer, Result, Runs,
                             Source, Tool, ToolError)
from .tools.extensions import API
from .tools.net import download, post

__all__ = ["API", "ERRORS", "Artifact", "BrowserTool", "Call", "Family", "Offer", "Result", "Runs",
           "Source", "Tool", "ToolError",
           "download", "logger", "post", "settings"]


def settings(name: str) -> dict:
    """La table [extensions.<name>] (vide si absente), ${VAR} résolus ;
    aucune autre table de la configuration. Sans `active`, que le proxy
    gère (l'interrupteur de la page)."""
    table = (config.get("extensions") or {}).get(name)
    return {k: v for k, v in table.items() if k != "active"} if isinstance(table, dict) else {}


def logger(name: str) -> logging.Logger:
    """Le journal du proxy, au nom de l'extension : ses lignes sortent avec
    celles du proxy."""
    return log.getChild(f"ext.{name}")
