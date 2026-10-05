# Codex CLI seul. Sa configuration — le provider qui vise le proxy, en
# API Responses — est générée au démarrage dans CODEX_HOME, à l'intérieur
# du conteneur : ~/.codex de l'hôte n'est jamais touché.
FROM node:22-slim
RUN npm install -g --ignore-scripts @openai/codex \
 && mkdir -p /work /codex && chown node:node /work /codex
USER node
WORKDIR /work
ENV CODEX_HOME=/codex
COPY --chown=node:node entrypoint.sh scenarios.sh ./
ENTRYPOINT ["./entrypoint.sh"]
CMD ["./scenarios.sh"]
