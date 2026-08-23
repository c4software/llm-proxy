# pi (pi.dev) seul. Sa configuration — les deux providers qui visent le
# proxy — est générée au démarrage depuis models.json.tpl et l'environnement,
# dans PI_CODING_AGENT_DIR, à l'intérieur du conteneur : ~/.pi de l'hôte
# n'est jamais touché.
FROM node:22-slim
RUN npm install -g --ignore-scripts @earendil-works/pi-coding-agent \
 && mkdir -p /work /pi && chown node:node /work /pi
USER node
WORKDIR /work
ENV PI_CODING_AGENT_DIR=/pi/agent
COPY --chown=node:node models.json.tpl entrypoint.sh scenarios.sh ./
ENTRYPOINT ["./entrypoint.sh"]
CMD ["./scenarios.sh"]
