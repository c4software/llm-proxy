# L'image du PROXY. Contexte de build : la RACINE du dépôt, pas ce
# dossier — l'image embarque data/config.example.toml, resté à la racine
# avec le data/ du déploiement (docker-compose.yml : `context: .`,
# `dockerfile: proxy/Dockerfile`). À la main :
#   docker build -f proxy/Dockerfile .
# Ce que le contexte emporte est borné par le .dockerignore de la racine.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# ffmpeg : l'outil hébergé `transcribe` convertit en WAV l'audio que le
# modèle de transcription ne lit pas ([tools.transcribe].convert). Sans
# lui le proxy tourne, et refuse ces fichiers.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

COPY proxy/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY proxy/main.py .
COPY proxy/llm_proxy/ llm_proxy/
# Le modèle de configuration seulement : data/ est un VOLUME, le proxy y
# écrit config.toml au premier démarrage s'il est absent, puis stats.db.
COPY data/config.example.toml data/

RUN useradd --create-home --uid 10001 appuser \
 && chown -R appuser:appuser /app/data
USER appuser

VOLUME ["/app/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status==200 else 1)"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
