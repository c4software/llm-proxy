# omp (oh-my-pi, https://github.com/can1357/oh-my-pi, fork de pi). Le dossier
# d'agent reste VIDE : le provider est chargé par -e à chaque appel.
#
# Binaire autonome des releases GitHub, pas npm (le paquet demande bun),
# sha256 vérifié contre SHA256SUMS.txt (install.mjs). Base node : les
# scénarios 4 et 5 font écrire et lancer du Node au modèle.
# OMP_VERSION : la dernière par défaut ; une version précise :
#   docker compose build --build-arg OMP_VERSION=18.3.2 omp
#
# Extension du provider : c4software/llmsetup, tools/llm-proxy.ts, à une
# révision ÉPINGLÉE, sha256 vérifié. La mettre à jour, c'est changer les DEUX
# valeurs ensemble :
#   LLMSETUP_REV          git ls-remote https://github.com/c4software/llmsetup HEAD
#                         (ou le commit voulu, en entier)
#   LLM_PROXY_SHA256      curl -fsSL https://raw.githubusercontent.com/c4software/llmsetup/<commit>/tools/llm-proxy.ts | sha256sum
# puis `docker compose build omp`. Un sha256 faux arrête la construction en
# affichant celui du fichier reçu. install.mjs y remplace l'adresse du proxy
# codée en dur par LLM_PROXY_URL et LLM_PROXY_API_KEY.
# Posée dans /omp/extensions, HORS de /work (que scenarios.sh vide) et de
# $PI_CODING_AGENT_DIR/extensions (qu'omp chargerait d'office).
FROM node:22-slim
ARG OMP_VERSION=latest
ARG LLMSETUP_REV=93698d56c65d66cdf722473efcc8c8f9a668c19d
ARG LLM_PROXY_SHA256=b0b551d11a50312782cf13a4aa2b8c5fbac4f3bcbf0453a1d006f2740781298a
COPY install.mjs /tmp/install.mjs
RUN mkdir -p /work /omp/agent \
 && node /tmp/install.mjs "${OMP_VERSION}" "${LLMSETUP_REV}" "${LLM_PROXY_SHA256}" \
 && rm /tmp/install.mjs \
 && chown -R node:node /work /omp
USER node
WORKDIR /work
ENV PI_CODING_AGENT_DIR=/omp/agent
COPY --chown=node:node entrypoint.sh scenarios.sh ./
ENTRYPOINT ["./entrypoint.sh"]
CMD ["./scenarios.sh"]
