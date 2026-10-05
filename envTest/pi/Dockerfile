# pi (pi.dev) seul. Sa configuration — les deux providers qui visent le
# proxy — est générée au démarrage depuis models.json.tpl et l'environnement,
# dans PI_CODING_AGENT_DIR, à l'intérieur du conteneur : ~/.pi de l'hôte
# n'est jamais touché.
#
# PI_VERSION : la dernière par défaut. Pour rejouer la version validée à la
# main le 05/10/2026 :
#   docker compose build --build-arg PI_VERSION=0.87.1 pi
#
# L'extension des outils web du proxy (scénario 6) vit dans un autre dépôt,
# public : c4software/llmsetup, tools/llm-proxy-web.ts. Elle est prise à la
# construction, à une révision ÉPINGLÉE — le commit entier dans l'URL raw de
# GitHub, donc un contenu qui ne peut pas changer — et son sha256 est
# vérifié : une image construite demain porte le fichier lu aujourd'hui.
# La mettre à jour, c'est changer les DEUX valeurs ensemble :
#   LLMSETUP_REV          git ls-remote https://github.com/c4software/llmsetup HEAD
#                         (ou le commit voulu, en entier)
#   LLM_PROXY_WEB_SHA256  curl -fsSL https://raw.githubusercontent.com/c4software/llmsetup/<commit>/tools/llm-proxy-web.ts | sha256sum
# puis `docker compose build pi`. Un sha256 qui ne correspond pas arrête la
# construction en affichant celui du fichier reçu. Pour un essai sans
# toucher au fichier : --build-arg LLMSETUP_REV=… --build-arg LLM_PROXY_WEB_SHA256=…
# Elle est posée dans /pi/extensions, HORS de /work (que scenarios.sh vide)
# et hors de $PI_CODING_AGENT_DIR/extensions (que pi chargerait d'office) :
# seul le scénario 6 la charge, par -e, les cinq autres restent ceux d'août.
FROM node:22-slim
ARG PI_VERSION=latest
ARG LLMSETUP_REV=93698d56c65d66cdf722473efcc8c8f9a668c19d
ARG LLM_PROXY_WEB_SHA256=ca0ce7783aa0ec9b9ef424bb0f21ff7449146b3195517e6312d01bc7011850e7
RUN npm install -g --ignore-scripts "@earendil-works/pi-coding-agent@${PI_VERSION}" \
 && mkdir -p /work /pi/extensions \
 && node -e 'const [url, dest, sum] = process.argv.slice(1); \
      fetch(url).then(async (r) => { \
        if (!r.ok) throw new Error("HTTP " + r.status + " pour " + url); \
        const body = Buffer.from(await r.arrayBuffer()); \
        const got = require("crypto").createHash("sha256").update(body).digest("hex"); \
        if (got !== sum) throw new Error("sha256 reçu " + got + ", attendu " + sum + " (" + url + ")"); \
        require("fs").writeFileSync(dest, body); \
      }).catch((e) => { console.error("llm-proxy-web.ts : " + e.message); process.exit(1); });' \
      "https://raw.githubusercontent.com/c4software/llmsetup/${LLMSETUP_REV}/tools/llm-proxy-web.ts" \
      /pi/extensions/llm-proxy-web.ts "${LLM_PROXY_WEB_SHA256}" \
 && chown -R node:node /work /pi
USER node
WORKDIR /work
ENV PI_CODING_AGENT_DIR=/pi/agent
COPY --chown=node:node models.json.tpl entrypoint.sh scenarios.sh ./
ENTRYPOINT ["./entrypoint.sh"]
CMD ["./scenarios.sh"]
