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
  app.py       l'application FastAPI
  web/         le tableau de bord (HTML/CSS/JS statiques)
"""
