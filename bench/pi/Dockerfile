# pi (pi.dev). Configuration générée au démarrage depuis models.json.tpl
# (entrypoint.sh).
# PI_VERSION : la dernière par défaut ; une version précise :
#   docker compose build --build-arg PI_VERSION=0.87.1 pi
FROM node:22-slim
ARG PI_VERSION=latest
RUN npm install -g --ignore-scripts "@earendil-works/pi-coding-agent@${PI_VERSION}" \
 && mkdir -p /work /pi \
 && chown -R node:node /work /pi
USER node
WORKDIR /work
ENV PI_CODING_AGENT_DIR=/pi/agent
COPY --chown=node:node models.json.tpl entrypoint.sh scenarios.sh ./
ENTRYPOINT ["./entrypoint.sh"]
CMD ["./scenarios.sh"]
