#!/bin/sh
# Provider `llm-proxy` en API Responses, que le proxy traduit.
# PROXY_API_KEY (env_key) doit être NON VIDE, même devant un proxy ouvert :
# sinon Codex s'arrête sur « Missing environment variable ».
# `model` ne sert qu'à un essai à la main : scenarios.sh passe -m.
set -eu
mkdir -p "$CODEX_HOME"
cat > "$CODEX_HOME/config.toml" <<TOML
model = "${MODELS%% *}"
model_provider = "llm-proxy"

[model_providers.llm-proxy]
name = "llm-proxy"
base_url = "${PROXY_URL}/v1"
wire_api = "responses"
env_key = "PROXY_API_KEY"
TOML
# CODEX_HOME du scénario 10 : plus le serveur MCP du proxy. Outils approuvés
# d'office (`codex exec` ne peut rien demander, il refuserait chaque appel),
# délai de 600 s pour les outils longs.
mkdir -p "$CODEX_HOME-mcp"
cat "$CODEX_HOME/config.toml" - > "$CODEX_HOME-mcp/config.toml" <<TOML

[mcp_servers.llmproxy]
url = "${PROXY_URL}/mcp"
bearer_token_env_var = "PROXY_API_KEY"
default_tools_approval_mode = "approve"
tool_timeout_sec = 600
TOML
exec "$@"
