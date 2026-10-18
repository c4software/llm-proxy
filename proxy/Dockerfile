# Contexte de build : la racine du dépôt (l'image embarque
# data/config.example.toml). À la main :
#   docker build -f proxy/Dockerfile .

# Les runtimes WASM des bacs du navigateur (PHP, SQLite) : construits
# depuis proxy/wasm, versions et empreintes figées par package-lock.json.
FROM node:24-slim AS wasm
WORKDIR /wasm
COPY proxy/wasm/package.json proxy/wasm/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY proxy/wasm/build.mjs proxy/wasm/php.js proxy/wasm/sql.js proxy/wasm/chart.js proxy/wasm/sucrase.js proxy/wasm/typescript.js ./
RUN node build.mjs /vendor

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# ffmpeg : `transcribe` convertit en WAV l'audio que le modèle ne lit
# pas ([tools.transcribe].convert) ; sans lui, ces fichiers sont refusés.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

COPY proxy/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY proxy/main.py .
COPY proxy/llm_proxy/ llm_proxy/
COPY --from=wasm /vendor/ llm_proxy/web/static/vendor/
# Le modèle seul : data/ est un volume, où le proxy crée config.toml
# s'il est absent.
COPY data/config.example.toml data/

RUN useradd --create-home --uid 10001 appuser \
 && chown -R appuser:appuser /app/data
USER appuser

VOLUME ["/app/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status==200 else 1)"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
