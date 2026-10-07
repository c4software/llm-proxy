#!/bin/sh
# Rien à générer : omp n'a pas de fichier de providers ici, ses modèles lui
# viennent de l'extension /omp/extensions/llm-proxy.ts, qui interroge
# /v1/models du proxy à chaque démarrage. Elle lit ses propres variables —
# l'adresse du proxy SANS /v1, et sa clé. Dérivées ici de PROXY_URL et
# PROXY_API_KEY plutôt que dans le docker-compose : une seule source, y
# compris sous `docker compose run -e PROXY_URL=…`.
set -eu
mkdir -p "$PI_CODING_AGENT_DIR"
export LLM_PROXY_URL="${LLM_PROXY_URL:-$PROXY_URL}"
export LLM_PROXY_API_KEY="${LLM_PROXY_API_KEY:-${PROXY_API_KEY:-unused}}"
exec "$@"
