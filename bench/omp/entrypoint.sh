#!/bin/sh
# Les modèles viennent de l'extension /omp/extensions/llm-proxy.ts (/v1/models).
# Ses variables (adresse SANS /v1, clé) sont dérivées ici plutôt que dans le
# docker-compose : une seule source, y compris sous `run -e PROXY_URL=…`.
set -eu
mkdir -p "$PI_CODING_AGENT_DIR"
export LLM_PROXY_URL="${LLM_PROXY_URL:-$PROXY_URL}"
export LLM_PROXY_API_KEY="${LLM_PROXY_API_KEY:-${PROXY_API_KEY:-unused}}"
# mcp.json du scénario 6, dans un second dossier d'agent. omp lit les
# variables dans `headers`. `timeout` en MILLISECONDES (défaut 30 s) : les
# outils longs du proxy vont jusqu'à 7 min.
mkdir -p "$PI_CODING_AGENT_DIR-mcp"
printf '{ "mcpServers": { "llmproxy": {\n    "url": "%s/mcp",\n    "headers": { "Authorization": "Bearer ${LLM_PROXY_API_KEY}" },\n    "timeout": 600000 } } }\n' \
    "$LLM_PROXY_URL" > "$PI_CODING_AGENT_DIR-mcp/mcp.json"
exec "$@"
