{
  "providers": {
    "llm-proxy": {
      "baseUrl": "${PROXY_URL}/v1",
      "api": "openai-completions",
      "apiKey": "$PROXY_API_KEY",
      "models": [
        {
          "id": "${MODEL}",
          "name": "${MODEL} (OpenAI)",
          "reasoning": false,
          "input": ["text"],
          "contextWindow": 131072,
          "maxTokens": 8192,
          "cost": { "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0 }
        }
      ]
    },
    "llm-proxy-anthropic": {
      "baseUrl": "${PROXY_URL}",
      "api": "anthropic-messages",
      "apiKey": "$PROXY_API_KEY",
      "models": [
        {
          "id": "${MODEL}",
          "name": "${MODEL} (Anthropic)",
          "reasoning": false,
          "input": ["text"],
          "contextWindow": 131072,
          "maxTokens": 8192,
          "cost": { "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0 }
        }
      ]
    }
  }
}
