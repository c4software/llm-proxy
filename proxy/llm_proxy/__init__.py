"""
llm-proxy — proxy OpenAI-compatible multi-backends devant Albert (DINUM)
et des serveurs locaux (llama.cpp).

Organisation du paquet, par domaine (chaque sous-paquet dit, dans son
__init__.py, ce qu'il contient ; docs/architecture.md, fichier par fichier) :
  app.py       l'application FastAPI : assemblage des routes et middlewares
  startup.py   démarrage et arrêt (lifespan), ce qu'il dit au journal
  core/        configuration (data/config.toml), réglages [proxy], HTTP commun
  access/      clés, session de la page, OIDC, comptes, partage équitable
  upstream/    backends, quotas d'Albert, catalogue des modèles, relais
  surfaces/    /v1/chat/completions, /v1/responses, /v1/messages, leurs
               traductions, la boucle des outils hébergés, le catch-all
  tools/       les outils hébergés (contrat, registre, outils)
  exposure/    les outils hébergés tels quels : /v1/tools, /mcp
  storage/     ce que data/ garde : stats, conversations, mémoire, fichiers,
               sauvegardes
  ui/          le tableau de bord (/ui/*) et ses fichiers (web/), l'Usage
               API, /healthz
"""
