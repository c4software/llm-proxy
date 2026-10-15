#!/bin/sh
# Génère la configuration Codex : un provider `llm-proxy` en API Responses
# (wire_api = "responses"), que le proxy traduit vers /v1/chat/completions.
# La clé est lue par Codex dans PROXY_API_KEY (env_key) : la variable doit
# être NON VIDE, même devant un proxy ouvert — vide ou absente, Codex
# s'arrête sur « Missing environment variable » avant toute requête
# (model-provider-info, api_key(), lu à la version 0.157.1). Le modèle est
# passé à chaque appel par scenarios.sh (-m) ; `model` (le premier de
# MODELS) ne sert qu'à un essai à la main, sans -m.
# `web_search` n'a rien à recevoir ici : Codex le déclare de lui-même.
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
# Le CODEX_HOME du scénario 10 : la même configuration, plus le serveur MCP
# du proxy (POST /mcp). Ses outils sont approuvés d'office — `codex exec`
# ne peut rien demander, il refuserait chaque appel — et leur délai porté à
# 600 s (les outils longs du proxy). Les autres scénarios ne l'ont pas.
mkdir -p "$CODEX_HOME-mcp"
cat "$CODEX_HOME/config.toml" - > "$CODEX_HOME-mcp/config.toml" <<TOML

[mcp_servers.llmproxy]
url = "${PROXY_URL}/mcp"
bearer_token_env_var = "PROXY_API_KEY"
default_tools_approval_mode = "approve"
tool_timeout_sec = 600
TOML
exec "$@"
