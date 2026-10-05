#!/bin/sh
# Génère la configuration pi depuis le gabarit : ${PROXY_URL} et la liste
# des modèles (une entrée par élément de MODELS) sont substitués ici ;
# "$PROXY_API_KEY" est laissé tel quel — c'est pi qui le lit dans
# l'environnement (sa syntaxe pour une clé en variable).
set -eu
mkdir -p "$PI_CODING_AGENT_DIR"
entries=""
for m in $MODELS; do
  entry="{\"id\": \"$m\", \"name\": \"$m\", \"reasoning\": false, \"input\": [\"text\"], \"contextWindow\": 131072, \"maxTokens\": 8192, \"cost\": {\"input\": 0, \"output\": 0, \"cacheRead\": 0, \"cacheWrite\": 0}}"
  entries="${entries:+$entries, }$entry"
done
sed -e "s|\${PROXY_URL}|${PROXY_URL}|g" -e "s|\${MODELS_JSON}|${entries}|g" \
    /work/models.json.tpl > "$PI_CODING_AGENT_DIR/models.json"
# L'extension des outils web (/pi/extensions/llm-proxy-web.ts) lit ses
# propres variables : l'adresse du proxy SANS /v1 et sa clé. Dérivées ici de
# PROXY_URL et PROXY_API_KEY plutôt que dans le docker-compose : une seule
# source, y compris sous `docker compose run -e PROXY_URL=…`.
export LLM_PROXY_URL="${LLM_PROXY_URL:-$PROXY_URL}"
export LLM_PROXY_KEY="${LLM_PROXY_KEY:-${PROXY_API_KEY:-unused}}"
exec "$@"
