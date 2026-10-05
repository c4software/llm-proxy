#!/bin/sh
# Génère la configuration Codex : un provider `llm-proxy` en API Responses
# (wire_api = "responses"), que le proxy traduit vers /v1/chat/completions.
# La clé est lue par Codex dans PROXY_API_KEY (env_key) ; le modèle est
# passé à chaque appel par scenarios.sh (-m).
set -eu
mkdir -p "$CODEX_HOME"
cat > "$CODEX_HOME/config.toml" <<TOML
model_provider = "llm-proxy"

[model_providers.llm-proxy]
name = "llm-proxy"
base_url = "${PROXY_URL}/v1"
wire_api = "responses"
env_key = "PROXY_API_KEY"
TOML
exec "$@"
