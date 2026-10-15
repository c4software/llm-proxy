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
# Le dossier d'agent du scénario 6 : la même configuration, plus le serveur
# MCP du proxy (exposure direct : ses outils déclarés au modèle, pas
# seulement joignables par codemode). L'URL est écrite ici — pi ne lit les
# variables que dans `headers` et `env` ; la clé reste "${PROXY_API_KEY}",
# que pi lit dans l'environnement (et refuse si elle n'y est pas). `timeout`
# en SECONDES (défaut 60) : les outils longs du proxy vont jusqu'à 7 min.
mkdir -p "$PI_CODING_AGENT_DIR-mcp"
cp "$PI_CODING_AGENT_DIR/models.json" "$PI_CODING_AGENT_DIR-mcp/models.json"
printf '{ "mcpServers": { "llmproxy": {\n    "url": "%s/mcp",\n    "headers": { "Authorization": "Bearer ${PROXY_API_KEY}" },\n    "exposure": "direct",\n    "timeout": 600 } } }\n' \
    "$PROXY_URL" > "$PI_CODING_AGENT_DIR-mcp/mcp.json"
export PROXY_API_KEY="${PROXY_API_KEY:-unused}"
exec "$@"
