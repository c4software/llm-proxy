# Un client HTTP nu : Python et sa bibliothèque standard (urllib), rien à
# installer, aucune configuration. Ce banc n'est pas un agent : il envoie
# les requêtes qu'aucun client agentique n'écrit de lui-même — la
# déclaration `{"type": "web_search"}` sur /v1/chat/completions — et lit
# ce que le proxy répond (voir scenarios.py).
# PROXY_URL, PROXY_API_KEY et MODELS viennent du docker-compose (env_file).
# Pas de version à fixer à la construction : rien de ce qui est vérifié ne
# dépend de celle de Python.
FROM python:3-slim
WORKDIR /work
COPY scenarios.py ./
USER nobody
# Sans tampon : chaque PASS/FAIL s'affiche au moment où il tombe.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
CMD ["python3", "scenarios.py"]
