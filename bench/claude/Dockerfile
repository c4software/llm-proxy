# Claude Code seul, sans rien d'autre. Utilisateur non root : Claude Code
# refuse --dangerously-skip-permissions à root, et les scénarios en ont
# besoin (aucune question posée, tout s'exécute).
# CLAUDE_CODE_VERSION : la dernière par défaut. Pour rejouer la version
# validée à la main le 05/10/2026 (recherche web) :
#   docker compose build --build-arg CLAUDE_CODE_VERSION=2.1.287 claude
FROM node:22-slim
ARG CLAUDE_CODE_VERSION=latest
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && mkdir -p /work && chown node:node /work
USER node
WORKDIR /work
# Aucun trafic hors proxy : pas de télémétrie, pas de mise à jour, pas de
# rapport d'erreur. Le reste (ANTHROPIC_*) vient du docker-compose.
ENV DISABLE_TELEMETRY=1 \
    DISABLE_ERROR_REPORTING=1 \
    DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
# ~/.claude/settings.json DU CONTENEUR (jamais celui de l'hôte) :
# CLAUDE_CODE_ATTRIBUTION_HEADER=0 retire l'en-tête d'attribution et le
# bloc qui l'accompagne dans le prompt — variable d'une requête à
# l'autre, il décalerait le préfixe et ferait manquer le cache du backend.
COPY --chown=node:node settings.json /home/node/.claude/settings.json
COPY --chown=node:node scenarios.sh ./
ENTRYPOINT []
CMD ["./scenarios.sh"]
