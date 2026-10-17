#!/bin/sh
# ${PROXY_URL} et ${MODELS_JSON} sont substitués ici ; "$PROXY_API_KEY" est
# laissé tel quel : pi le lit dans l'environnement.
set -eu
mkdir -p "$PI_CODING_AGENT_DIR"
entries=""
for m in $MODELS; do
  entry="{\"id\": \"$m\", \"name\": \"$m\", \"reasoning\": false, \"input\": [\"text\"], \"contextWindow\": 131072, \"maxTokens\": 8192, \"cost\": {\"input\": 0, \"output\": 0, \"cacheRead\": 0, \"cacheWrite\": 0}}"
  entries="${entries:+$entries, }$entry"
done
sed -e "s|\${PROXY_URL}|${PROXY_URL}|g" -e "s|\${MODELS_JSON}|${entries}|g" \
    /work/models.json.tpl > "$PI_CODING_AGENT_DIR/models.json"
# Dossier d'agent du scénario 6 : plus le serveur MCP du proxy (exposure
# direct : outils déclarés au modèle, pas seulement par codemode). L'URL est
# écrite ici, pi ne lit les variables que dans `headers` et `env`. `timeout`
# en SECONDES (défaut 60) : les outils longs du proxy vont jusqu'à 7 min.
mkdir -p "$PI_CODING_AGENT_DIR-mcp"
cp "$PI_CODING_AGENT_DIR/models.json" "$PI_CODING_AGENT_DIR-mcp/models.json"
printf '{ "mcpServers": { "llmproxy": {\n    "url": "%s/mcp",\n    "headers": { "Authorization": "Bearer ${PROXY_API_KEY}" },\n    "exposure": "direct",\n    "timeout": 600 } } }\n' \
    "$PROXY_URL" > "$PI_CODING_AGENT_DIR-mcp/mcp.json"
export PROXY_API_KEY="${PROXY_API_KEY:-unused}"
exec "$@"
