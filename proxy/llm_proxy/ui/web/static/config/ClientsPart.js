/*
  Configuration → Clients : on choisit le client (Claude Code, Codex, pi /
  omp, SDK OpenAI, curl, Open WebUI, tout client OpenAI), le modèle du
  catalogue (/ui/models), et s'il prend aussi les outils du proxy par son
  serveur MCP (/mcp). La page produit les fichiers à poser, avec leur
  chemin, et les étapes. La CLÉ n'y figure jamais : elle vit dans un
  fichier à part (KEYFILE, mode 600, posé une fois), que le profil du shell
  exporte dans une variable (LLM_PROXY_API_KEY par défaut).
*/
import { useCopy, download } from "../lib/files.js";
import { q, envName, KEYFILE, KEY_SETUP, useAccount, useCatalog } from "./shared.js";

const { reactive, computed, watch } = Vue;
const origin = window.location.origin;

export const ClientsPart = {
  props: {
    session: { type: Object, required: true },
    health: { type: Object, default: null },
  },
  emits: ["goto"],
  setup(props) {
    const { account } = useAccount(props);
    const { models, contexts } = useCatalog();
    const { copied, copy } = useCopy();
    const health = computed(() => props.health || {});
    const auth = computed(() => !!props.session.auth_required);
    const anthropicOn = computed(() => !!(health.value.anthropic || {}).enabled);
    const responsesOn = computed(() => !!(health.value.responses || {}).enabled);
    const mcpOn = computed(() => !!(health.value.mcp_server || {}).enabled);

    // ══ CLIENTS ═════════════════════════════════════════════════════════
    const clients = [
      { id: "claude", label: "Claude Code", mcp: true },
      { id: "codex", label: "Codex CLI", mcp: true },
      { id: "pi", label: "pi / omp", mcp: true, mcpOnly: true },
      { id: "python", label: "SDK OpenAI (Python)" },
      { id: "curl", label: "curl" },
      { id: "openwebui", label: "Open WebUI" },
      { id: "generic", label: "Autre client OpenAI" },
    ];
    const form = reactive({
      client: "claude", model: "", small: "", mcp: true, env: "LLM_PROXY_API_KEY",
    });
    watch(models, (ms) => {
      if (!form.model && ms.length) form.model = ms[0];
      if (!form.small && ms.length) form.small = ms[ms.length > 1 ? 1 : 0];
    });
    const client = computed(() => clients.find((c) => c.id === form.client));

    const clientOut = computed(() => {
      const model = form.model || "<backend>/<modèle>";
      const env = envName(form.env) || "LLM_PROXY_API_KEY";
      // La clé : dans KEYFILE (mode 600, hors de tout dépôt de dotfiles),
      // lue par le profil du shell. Proxy ouvert : «unused» suffit (pi et
      // Codex refusent une variable vide).
      const exportLine = auth.value
        ? `export ${env}="$(cat ${KEYFILE} 2>/dev/null || echo unused)"`
        : `export ${env}=unused`;
      const files = [];
      const steps = [];
      const warn = [];
      const mcp = client.value.mcp && form.mcp;
      if (mcp && !mcpOn.value) {
        warn.push("Le serveur MCP du proxy est désactivé ([mcp_server] enabled = false) : "
          + "les outils ne seront pas joignables.");
      }
      const shell = (name, content, note) => files.push({ name, content, note, lang: "sh" });
      if (auth.value && form.client !== "openwebui") {
        shell("La clé, une fois", KEY_SETUP,
          "Saisie masquée, hors de l'historique du shell.");
      }

      switch (form.client) {
        case "claude": {
          if (!anthropicOn.value) warn.push("La surface Anthropic est désactivée sur ce proxy "
            + "([anthropic] enabled = false) : Claude Code ne pourra pas s'y connecter.");
          files.push({
            name: "~/.claude/settings.json", download: "settings.json", lang: "json",
            note: "Les réglages de Claude Code (à fusionner si le fichier existe). "
              + "La clé n'y est pas : Claude Code la lit dans l'environnement.",
            content: JSON.stringify({ env: {
              ANTHROPIC_BASE_URL: origin,
              ANTHROPIC_MODEL: model,
              ...(form.small ? { ANTHROPIC_SMALL_FAST_MODEL: form.small } : {}),
            } }, null, 2) + "\n",
          });
          shell("~/.bashrc (ou ~/.zshrc)",
            `${exportLine}\nexport ANTHROPIC_API_KEY="$${env}"\n`,
            "La clé, par l'environnement.");
          if (mcp) {
            shell("Commande, une fois", `claude mcp add --transport http --scope user llmproxy ${origin}/mcp`
              + (auth.value ? ` \\\n  --header "Authorization: Bearer $${env}"` : "") + "\n",
              "Les outils du proxy (recherche web, lecture de pages, code, mémoire…) dans la "
              + "boucle principale de Claude Code.");
          }
          steps.push("Poser settings.json, ajouter les deux lignes au profil du shell, "
            + "rouvrir le terminal.", "Lancer `claude` : `/status` doit montrer l'URL du proxy.",
            "Les noms claude-* que Claude Code envoie en dur (sous-agents, /model) sont "
            + "traduits par [anthropic.model_map] du proxy.");
          if (mcp) steps.push("`/mcp` dans Claude Code doit lister le serveur llmproxy.");
          break;
        }
        case "codex": {
          if (!responsesOn.value) warn.push("La surface Responses est désactivée sur ce proxy "
            + "([responses] enabled = false) : Codex ne pourra pas s'y connecter.");
          const ctx = contexts.value[form.model];
          let toml = `model = ${q(model)}\n`
            + (ctx ? `# Contexte du modèle (catalogue du proxy) : Codex ne le connaît pas.\n`
              + `model_context_window = ${ctx}\n` : "")
            + `model_provider = "llm-proxy"\n\n`
            + `[model_providers.llm-proxy]\nname = "llm-proxy"\nbase_url = ${q(origin + "/v1")}\n`
            + `wire_api = "responses"\n` + (auth.value ? `env_key = ${q(env)}\n` : "");
          if (mcp) {
            toml += `\n[mcp_servers.llmproxy]\nurl = ${q(origin + "/mcp")}\n`
              + (auth.value ? `bearer_token_env_var = ${q(env)}\n` : "")
              + `# Exécutés sur le proxy, pas sur le poste ; \`codex exec\` ne peut rien\n`
              + `# demander et refuserait chaque appel.\ndefault_tools_approval_mode = "approve"\n`
              + `# Transcription, images : jusqu'à 7 min côté proxy.\ntool_timeout_sec = 600\n`;
          }
          files.push({ name: "~/.codex/config.toml", download: "config.toml", lang: "toml",
            note: "À fusionner si le fichier existe déjà.", content: toml });
          if (auth.value) shell("~/.bashrc (ou ~/.zshrc)", exportLine + "\n", "La clé, par l'environnement.");
          steps.push("Poser config.toml" + (auth.value ? ", exporter la clé, rouvrir le terminal" : "")
            + ", lancer `codex`.");
          if (auth.value) steps.push("Codex interactif passe par un démon (`codex app-server`) qui garde "
            + "l'environnement de son lancement : après un changement de clé, le relancer — "
            + "`pkill -f 'codex app-server'`, puis `codex`. Sinon : 401 malgré la bonne clé.");
          if (!mcp) steps.push("La recherche web de Codex passe déjà par le proxy, sans MCP.");
          break;
        }
        case "pi": {
          // Le délai des outils longs du proxy (jusqu'à 7 min) : pi le compte
          // en secondes, omp en millisecondes (30 s par défaut).
          const server = (extra) => JSON.stringify({ mcpServers: { llmproxy: {
            url: origin + "/mcp",
            headers: { Authorization: "Bearer ${" + env + "}" }, ...extra,
          } } }, null, 2) + "\n";
          if (mcp) {
            files.push({ name: "~/.pi/agent/mcp.json", download: "mcp.json", lang: "json",
              note: "pi exige \"exposure\": \"direct\" pour déclarer les outils au modèle ; timeout en secondes.",
              content: server({ exposure: "direct", timeout: 600 }) });
            files.push({ name: "~/.omp/agent/mcp.json", download: "mcp.json", lang: "json",
              note: "omp : timeout en millisecondes. Désactiver son web_search "
                + "intégré pour que la recherche soit celle du proxy.", content: server({ timeout: 600000 }) });
          }
          shell("~/.bashrc (ou ~/.zshrc)",
            `export LLM_PROXY_URL=${origin}\n${exportLine}\n`
            + (env !== "LLM_PROXY_API_KEY" ? `export LLM_PROXY_API_KEY="$${env}"\n` : ""),
            "Lus par l'extension llm-proxy.ts (dépôt llmsetup) et par l'en-tête du mcp.json ; "
            + "pi refuse un en-tête dont la variable n'est pas définie.");
          shell("Lancer", `pi --model albert/${model}\n`,
            "Avec l'extension llm-proxy.ts, les modèles se nomment albert/<backend>/<modèle>.");
          steps.push("Copier llm-proxy.ts (dépôt llmsetup, tools/) dans ~/.pi/agent/extensions/ "
            + "et ~/.omp/agent/extensions/.");
          if (mcp) steps.push("Les outils apparaissent comme mcp__llmproxy__web_search (pi) "
            + "ou mcp__llmproxy_web_search (omp).");
          break;
        }
        case "python": {
          files.push({ name: "exemple.py", lang: "python", note: "pip install openai",
            content: `import os\n\nfrom openai import OpenAI\n\nclient = OpenAI(\n`
              + `    base_url=${q(origin + "/v1")},\n    api_key=os.environ.get(${q(env)}, "unused"),\n)\n`
              + `r = client.chat.completions.create(\n    model=${q(model)},\n`
              + `    messages=[{"role": "user", "content": "Bonjour"}],\n`
              + (form.mcp ? `    # Outils exécutés par le proxy : déclarés par leur type.\n`
                + `    tools=[{"type": "web_search"}],\n` : "")
              + `)\nprint(r.choices[0].message.content)\n` });
          if (auth.value) shell("Environnement", exportLine + "\n", "La clé, par l'environnement.");
          break;
        }
        case "curl": {
          shell("Requête", (auth.value ? exportLine + "\n" : "")
            + `curl -s ${origin}/v1/chat/completions \\\n  -H "Content-Type: application/json"`
            + (auth.value ? ` \\\n  -H "Authorization: Bearer $${env}"` : "")
            + ` \\\n  -d '${JSON.stringify({ model, messages: [{ role: "user", content: "Bonjour" }],
              ...(form.mcp ? { tools: [{ type: "web_search" }] } : {}) })}'\n`,
            form.mcp ? "Avec {\"type\": \"web_search\"} : la recherche est exécutée par le proxy." : "");
          break;
        }
        case "openwebui": {
          files.push({ name: "Réglages › Connexions › API OpenAI", lang: "text",
            note: "Dans l'interface d'administration d'Open WebUI.",
            content: `URL : ${origin}/v1\nClé : ${auth.value ? "<votre clé du proxy>" : "unused"}\n` });
          files.push({ name: "Ou par l'environnement du conteneur", download: "openwebui.env", lang: "sh",
            content: `OPENAI_API_BASE_URL=${origin}/v1\nOPENAI_API_KEY=${auth.value ? "<votre clé du proxy>" : "unused"}\n` });
          steps.push("Open WebUI ne déclare jamais d'outils : pour qu'il ait la recherche web, "
            + "les nommer dans [chat].always du proxy (onglet Proxy ci-contre).");
          break;
        }
        default: {
          files.push({ name: "Paramètres du client", lang: "text",
            content: `Base URL : ${origin}/v1\nClé       : ${auth.value ? "$" + env : "unused (le proxy est ouvert)"}\n`
              + `Modèle    : ${model}\n` });
          steps.push("Tout client OpenAI-compatible : base URL, clé, nom de modèle préfixé "
            + "par son backend. Le streaming passe intact.");
        }
      }
      // Vérifier : le catalogue avec la clé, et les outils du serveur MCP.
      const authH = auth.value ? ` \\\n  -H "Authorization: Bearer $${env}"` : "";
      let check = `curl -s ${origin}/v1/models${authH} | jq '.data[].id'\n`;
      if (mcp) {
        check += `\ncurl -s ${origin}/mcp \\\n  -H "Content-Type: application/json" \\\n`
          + `  -H "Accept: application/json, text/event-stream"${authH} \\\n`
          + `  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq '.result.tools[].name'\n`;
      }
      shell("Vérifier", check, mcp ? "Le catalogue des modèles, puis les outils que le serveur "
        + "MCP offre — avec la même clé." : "Le catalogue des modèles, avec la même clé.");
      return { files, steps, warn };
    });


    return { account, clients, form, client, models, clientOut, auth, mcpOn,
             copied, copy, download };
  },
  template: `
    <div class="config-grid">
      <form class="panel config-form" @submit.prevent>
        <div class="panel-head"><h2>Client</h2></div>
        <div class="config-fields">
          <label class="field"><span>Client</span>
            <select v-model="form.client">
              <option v-for="c in clients" :key="c.id" :value="c.id">{{ c.label }}</option>
            </select></label>
          <label class="field"><span>Modèle</span>
            <select v-if="models.length" v-model="form.model">
              <option v-for="m in models" :key="m" :value="m">{{ m }}</option></select>
            <input v-else v-model="form.model" placeholder="backend/modèle" spellcheck="false"></label>
          <label class="field" v-if="form.client === 'claude'"><span>Modèle des tâches de fond</span>
            <select v-if="models.length" v-model="form.small">
              <option value="">— aucun —</option>
              <option v-for="m in models" :key="m" :value="m">{{ m }}</option></select>
            <input v-else v-model="form.small" spellcheck="false"></label>
          <label class="check" v-if="client.mcp || ['python', 'curl'].includes(form.client)">
            <input type="checkbox" v-model="form.mcp">
            <template v-if="client.mcp">les outils du proxy par son serveur MCP</template>
            <template v-else>déclarer la recherche web du proxy</template></label>
          <label class="field"><span>Variable de la clé</span>
            <input v-model="form.env" spellcheck="false"></label>
          <p class="note" v-if="!auth">Le proxy est ouvert : la clé est libre, la variable vaut
            <code>unused</code>.</p>
          <p class="note" v-else-if="account">La clé : créez-en une par client dans l'onglet
            <button type="button" class="link" @click="$emit('goto', 'keys')">Clés</button>.</p>
        </div>
      </form>
      <div class="config-out">
        <p class="chat-warn panel" v-for="w in clientOut.warn" :key="w">{{ w }}</p>
        <section class="panel config-file" v-for="f in clientOut.files" :key="f.name">
          <header class="panel-head">
            <h3>{{ f.name }}</h3>
            <span class="config-actions">
              <button type="button" class="btn ghost" @click="copy(f)">{{ copied === f.name ? "Copié" : "Copier" }}</button>
              <button type="button" class="btn ghost" @click="download(f)">Télécharger</button>
            </span>
          </header>
          <p class="note" v-if="f.note">{{ f.note }}</p>
          <pre>{{ f.content }}</pre>
        </section>
        <section class="panel config-steps" v-if="clientOut.steps.length">
          <div class="panel-head"><h2>Étapes</h2></div>
          <ol><li v-for="s in clientOut.steps" :key="s">{{ s }}</li></ol>
        </section>
      </div>
    </div>

  `,
};
