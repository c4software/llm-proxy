"""
Les DÉCLARATIONS des outils natifs du NAVIGATEUR (Runs.BROWSER) : leurs
prompts, schémas et familles, ici, au même contrat que les outils du
proxy (contract.py) ; la page les reçoit par /ui/session et y attache
leur exécution (web/static/chat/tools/). Un module par groupe de la page,
dans l'ordre du menu Outils :

  page.py      questions, fichiers, artifacts, dossier ouvert  (chat/tools/page/)
  device.py    l'appareil                                      (chat/tools/device/)
  sandbox.py   les bacs de code                                (chat/tools/sandbox/)
"""

from . import device, page, sandbox

TOOLS = (*page.TOOLS, *device.TOOLS, *sandbox.TOOLS)
FAMILIES = (*page.FAMILIES, *device.FAMILIES, *sandbox.FAMILIES)
