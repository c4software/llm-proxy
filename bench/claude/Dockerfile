# Non root : Claude Code refuse --dangerously-skip-permissions à root.
# CLAUDE_CODE_VERSION : la dernière par défaut ; une version précise :
#   docker compose build --build-arg CLAUDE_CODE_VERSION=2.1.287 claude
FROM node:22-slim
ARG CLAUDE_CODE_VERSION=latest
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && mkdir -p /work && chown node:node /work
USER node
WORKDIR /work
# Aucun trafic hors proxy.
ENV DISABLE_TELEMETRY=1 \
    DISABLE_ERROR_REPORTING=1 \
    DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
# settings.json : CLAUDE_CODE_ATTRIBUTION_HEADER=0 retire le bloc
# d'attribution du prompt, qui varie d'une requête à l'autre et ferait
# manquer le cache du backend.
COPY --chown=node:node settings.json /home/node/.claude/settings.json
COPY --chown=node:node scenarios.sh ./
ENTRYPOINT []
CMD ["./scenarios.sh"]
