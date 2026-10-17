# Configuration générée au démarrage (entrypoint.sh) dans CODEX_HOME.
# --ignore-scripts n'écarte pas le binaire : il vient d'une dépendance
# optionnelle par plateforme.
# CODEX_VERSION : la dernière par défaut ; une version précise :
#   docker compose build --build-arg CODEX_VERSION=0.157.1 codex
FROM node:22-slim
ARG CODEX_VERSION=latest
RUN npm install -g --ignore-scripts "@openai/codex@${CODEX_VERSION}" \
 && mkdir -p /work /codex /codex-mcp && chown node:node /work /codex /codex-mcp
USER node
WORKDIR /work
ENV CODEX_HOME=/codex
COPY --chown=node:node entrypoint.sh scenarios.sh ./
ENTRYPOINT ["./entrypoint.sh"]
CMD ["./scenarios.sh"]
