{
  "providers": {
    "llm-proxy": {
      "baseUrl": "${PROXY_URL}/v1",
      "api": "openai-completions",
      "apiKey": "$PROXY_API_KEY",
      "models": [${MODELS_JSON}]
    },
    "llm-proxy-anthropic": {
      "baseUrl": "${PROXY_URL}",
      "api": "anthropic-messages",
      "apiKey": "$PROXY_API_KEY",
      "models": [${MODELS_JSON}]
    }
  }
}
