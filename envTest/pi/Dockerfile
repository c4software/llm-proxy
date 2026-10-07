# pi (pi.dev) seul. Sa configuration — les deux providers qui visent le
# proxy — est générée au démarrage depuis models.json.tpl et l'environnement,
# dans PI_CODING_AGENT_DIR, à l'intérieur du conteneur : ~/.pi de l'hôte
# n'est jamais touché.
#
# PI_VERSION : la dernière par défaut. Pour rejouer la version validée à la
# main le 05/10/2026 :
#   docker compose build --build-arg PI_VERSION=0.87.1 pi
#
# Aucune extension : les outils web viennent du proxy, d'office
# ([chat].always). L'extension llm-proxy-web.ts de llmsetup, que cette image
# téléchargeait jusqu'au 07/10/2026, a été retirée.
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
