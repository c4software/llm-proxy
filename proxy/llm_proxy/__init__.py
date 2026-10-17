"""
llm-proxy — proxy OpenAI-compatible multi-backends devant Albert (DINUM)
et des serveurs locaux (llama.cpp).

Organisation du paquet :
  config.py    chargement de data/config.toml (source unique de vérité)
  settings.py  la table [proxy], en constantes typées
  backends.py  déclaration des backends et routage par préfixe de modèle
  albert.py    limiteur de quotas Albert (fenêtres minute et jour)
  stats.py     compteurs persistés (SQLite) et Usage API OpenAI
  anthropic_api.py  surface Anthropic (Claude Code) : traduction
               Messages ↔ chat/completions, flux SSE compris
  app.py       l'application FastAPI : démarrage, assemblage des routes,
               /healthz, /v1/tools, /mcp, le catch-all
  auth.py      clés, session de la page, middleware d'auth
  relaying.py  le relais vers les backends (Call, gate, forward)
  catalog.py   le catalogue des modèles (/v1/models, types)
  surfaces.py  /v1/chat/completions, /v1/messages, /v1/responses
  tool_loop.py la boucle des outils hébergés
  usage_api.py l'Usage API (/v1/organization/usage/*)
  ui_routes.py les routes du tableau de bord (/ui/*)
  httputil.py  forme des erreurs, corps JSON
  web/         le tableau de bord (HTML/CSS/JS statiques)
"""
