# omp (oh-my-pi, https://github.com/can1357/oh-my-pi, fork de pi) seul. Son
# dossier d'agent est PI_CODING_AGENT_DIR — la même variable que pi —, à
# l'intérieur du conteneur : ~/.omp de l'hôte n'est jamais touché. Il reste
# VIDE : ni configuration ni extension découverte d'office, ce que le banc
# charge — le provider — l'est par -e, à chaque appel (scenarios.sh).
#
# omp n'est pas pris sur npm (le paquet @oh-my-pi/pi-coding-agent demande
# bun) mais dans les releases GitHub, comme le fait son script d'installation
# officiel : un binaire autonome, `omp-linux-x64`, sha256 vérifié contre le
# SHA256SUMS.txt de la release (install.mjs). node:22-slim reste la base :
# les scénarios 4 et 5 font écrire et lancer du Node au modèle.
# OMP_VERSION : la dernière par défaut, comme les autres images. Pour rejouer
# la version validée à la main le 05/10/2026 :
#   docker compose build --build-arg OMP_VERSION=18.3.2 omp
#
# L'extension du provider vit dans un autre dépôt, public :
# c4software/llmsetup, tools/llm-proxy.ts (les modèles du proxy, lus sur
# /v1/models, sous le nom « albert »). Elle est prise à la construction, à
# une révision ÉPINGLÉE, sha256 vérifié : une image construite demain porte
# le fichier lu aujourd'hui. La mettre à jour, c'est changer les DEUX
# valeurs ensemble :
#   LLMSETUP_REV          git ls-remote https://github.com/c4software/llmsetup HEAD
#                         (ou le commit voulu, en entier)
#   LLM_PROXY_SHA256      curl -fsSL https://raw.githubusercontent.com/c4software/llmsetup/<commit>/tools/llm-proxy.ts | sha256sum
# puis `docker compose build omp`. (L'autre extension, llm-proxy-web.ts —
# recherche web et lecture de page —, était prise de même jusqu'au
# 07/10/2026 : retirée, les outils web viennent du proxy, d'office.) Un sha256 qui ne correspond pas arrête la
# construction en affichant celui du fichier reçu.
# llm-proxy.ts a, à cette révision, l'adresse du proxy en dur : install.mjs
# y remplace deux lignes pour qu'il lise LLM_PROXY_URL et LLM_PROXY_API_KEY
# (détail et raison dans install.mjs).
# Elle est posée dans /omp/extensions, HORS de /work (que scenarios.sh
# vide) et hors de $PI_CODING_AGENT_DIR/extensions (qu'omp chargerait
# d'office).
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
