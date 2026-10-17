# Client HTTP nu (bibliothèque standard) : les requêtes qu'aucun agent
# n'envoie de lui-même, comme `{"type": "web_search"}` sur
# /v1/chat/completions. Version de Python libre : rien n'en dépend.
FROM python:3-slim
WORKDIR /work
COPY scenarios.py ./
USER nobody
# Sans tampon : chaque PASS/FAIL s'affiche au moment où il tombe.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
CMD ["python3", "scenarios.py"]
