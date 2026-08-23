#!/bin/sh
# Génère la configuration pi depuis le gabarit : ${PROXY_URL} et ${MODEL}
# sont substitués ici ; "$PROXY_API_KEY" est laissé tel quel — c'est pi
# qui le lit dans l'environnement (sa syntaxe pour une clé en variable).
set -eu
mkdir -p "$PI_CODING_AGENT_DIR"
sed -e "s|\${PROXY_URL}|${PROXY_URL}|g" -e "s|\${MODEL}|${MODEL}|g" \
    /work/models.json.tpl > "$PI_CODING_AGENT_DIR/models.json"
exec "$@"
