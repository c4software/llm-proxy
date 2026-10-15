# pi (pi.dev) seul. Sa configuration — les deux providers qui visent le
# proxy — est générée au démarrage depuis models.json.tpl et l'environnement,
# dans PI_CODING_AGENT_DIR, à l'intérieur du conteneur : ~/.pi de l'hôte
# n'est jamais touché.
#
# PI_VERSION : la dernière par défaut. Pour rejouer la version validée à la
# main le 05/10/2026 :
#   docker compose build --build-arg PI_VERSION=0.87.1 pi
#
# Aucune extension : les outils du proxy viennent de son serveur MCP
# (POST /mcp), que pi joint nativement — entrypoint.sh écrit le mcp.json
# dans un SECOND dossier d'agent, $PI_CODING_AGENT_DIR-mcp, que seul le
# scénario 6 utilise. (Jusqu'au 07/10/2026 : l'extension llm-proxy-web.ts ;
# jusqu'au 15/10/2026 : les outils d'office, [chat].always.)
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
