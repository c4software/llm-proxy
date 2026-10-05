# Codex CLI seul. Sa configuration — le provider qui vise le proxy, en
# API Responses — est générée au démarrage dans CODEX_HOME, à l'intérieur
# du conteneur : ~/.codex de l'hôte n'est jamais touché.
#
# Le paquet npm n'a ni script d'installation ni dépendance : le binaire
# (Rust, lié statiquement) vient d'une dépendance optionnelle par
# plateforme, que --ignore-scripts n'écarte pas.
# CODEX_VERSION : la dernière par défaut. Pour rejouer la version validée à
# la main le 05/10/2026 :
#   docker compose build --build-arg CODEX_VERSION=0.157.1 codex
FROM node:22-slim
ARG CODEX_VERSION=latest
RUN npm install -g --ignore-scripts "@openai/codex@${CODEX_VERSION}" \
 && mkdir -p /work /codex && chown node:node /work /codex
USER node
WORKDIR /work
ENV CODEX_HOME=/codex
COPY --chown=node:node entrypoint.sh scenarios.sh ./
ENTRYPOINT ["./entrypoint.sh"]
CMD ["./scenarios.sh"]
