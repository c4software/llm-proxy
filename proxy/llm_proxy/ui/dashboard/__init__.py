"""
Le tableau de bord, sous /ui : la page (statique, versionnée), son
service worker, l'entrée (clé ou OIDC) et la sortie, la session, les
comptes et leurs clés, le chat de la page (qui EST /v1/chat/completions
sous la session), les images, les statistiques de la personne connectée
et sa mémoire. Le doublon de certaines routes de l'API sous /ui n'existe
que pour l'auth : un fetch de navigateur porte la session, pas d'en-tête
Authorization.

Rangé en parties :
  page.py        la page, son service worker, connexion et déconnexion
  accounts.py    OIDC, sauvegarde, clés, utilisateurs
  chat.py        session, modèles, chat, images, voix, usage
  memory.py      la mémoire de l'outil memory
"""

from .page import (  # noqa: F401
    UI_CHAT,
    UI_CHAT_MODEL,
    UI_HTML,
    router,
)
from .accounts import (  # noqa: F401
    admin_only,
    ui_login_mode,
)
from .chat import (  # noqa: F401
    chat_tool_groups,
    ui_chat,
)
# Chaque partie inscrit ses routes sur le routeur commun (page.router) en
# s'important : toutes le sont, même celles dont rien n'est réexporté.
from . import memory  # noqa: F401,E402
