<script>
// @ts-check
/*
  Configuration → Clients : on choisit le client (Claude Code, Codex CLI
  ou application, pi / omp, OpenCode, SDK OpenAI, curl, Open WebUI, tout client OpenAI, le
  panneau d'agents d'Omarchy), le modèle du
  catalogue (/ui/models), et s'il prend aussi les outils du proxy par son
  serveur MCP (/mcp). Pour Codex, les modèles de son catalogue se cochent
  (les décochés restent retenus par le navigateur, un nouveau modèle du
  proxy arrive coché). La page produit les fichiers à poser, avec leur
  chemin, et les étapes. La CLÉ n'y figure jamais : elle vit dans un
  fichier à part (KEYFILE, mode 600, posé une fois), que le profil du shell
  exporte dans une variable (LLM_PROXY_API_KEY par défaut).
*/
import * as Vue from "vue";
import * as VueUse from "@vueuse/core";
import { useCopy, download } from "../lib/files.js";
import { TZ } from "../lib/format.js";
import { q, envName, KEYFILE, KEY_SETUP, useAccount, useCatalog } from "./shared.js";
import { catalogState } from "../lib/catalog.js";
import piExtension from "./pi-llm-proxy.ts?raw";
import opencodeExtension from "./opencode-llm-proxy.ts?raw";

const { reactive, computed, watch, ref } = Vue;
const origin = window.location.origin;
// Le panneau d'agents d'Omarchy lit un enregistrement par fichier de ce
// dossier : un service systemd de l'utilisateur y dépose /v1/usage/agent.
const OMARCHY_UNIT = "llm-proxy-usage";
const OMARCHY_DIR = "~/.local/state/omarchy/agents/usage";

/** L'ExecStart du service : /v1/usage/agent dans le dossier d'Omarchy. */
function omarchyRun(auth) {
  // Dans une unité systemd, « % » ouvre un spécificateur et « $ » une de
  // ses variables : doublés. Le fuseau va tel quel, son « + » en %%2B.
  const url = `${origin}/v1/usage/agent?tz=${TZ.replace(/\+/g, "%%2B")}`;
  const dir = OMARCHY_DIR.replace("~", () => "$$HOME");
  // La clé passe à curl par son entrée (-H @-), jamais en argument : elle
  // ne se lit pas dans la liste des processus.
  const get = auth
    ? `printf "Authorization: Bearer %%s" "$$(cat ${KEYFILE.replace("~", () => "$$HOME")})" | curl -fsS -H @- "${url}"`
    : `curl -fsS "${url}"`;
  return `d="${dir}"; mkdir -p "$$d" && ${get} -o "$$d/.llm-proxy.tmp" `
    + `&& mv "$$d/.llm-proxy.tmp" "$$d/llm-proxy.json"`;
}

const ClientsPart = {
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
      { id: "codexapp", label: "Codex (application de bureau)", mcp: true },
      { id: "pi", label: "pi / omp", mcp: true },
      // Le plugin déclare lui-même le serveur MCP du proxy : pas de case.
      { id: "opencode", label: "OpenCode", mcp: true, mcpFixed: true },
      { id: "python", label: "SDK OpenAI (Python)" },
      { id: "curl", label: "curl" },
      { id: "openwebui", label: "Open WebUI" },
      { id: "generic", label: "Autre client OpenAI" },
      // Un service, pas un client de modèles : ni modèle ni variable de clé.
      { id: "omarchy", label: "Panneau d'agents d'Omarchy", service: true },
    ];
    const form = reactive({
      client: "claude", model: "", small: "", mcp: true, env: "LLM_PROXY_API_KEY", catalog: true,
    });
    watch(models, (ms) => {
      if (!form.model && ms.length) form.model = ms[0];
      if (!form.small && ms.length) form.small = ms[ms.length > 1 ? 1 : 0];
    });
    const client = computed(() => clients.find((c) => c.id === form.client));

    // ── extensions des postes ───────────────────────────────────────────
    // pi, omp et OpenCode lisent le catalogue du proxy par une extension
    // TypeScript (copies de celles du dépôt dotarchy), dont le texte est
    // dans le build de la page (import `?raw`, en tête de module).
    const EXTENSIONS = { "pi-llm-proxy.ts": piExtension, "opencode-llm-proxy.ts": opencodeExtension };
    // Replié : une centaine de lignes, qu'on copie ou télécharge.
    const extFile = (path, name, note) => ({ name, download: "llm-proxy.ts", lang: "typescript",
                                             collapse: true, note, content: EXTENSIONS[path] });

    // ── catalogue de Codex ──────────────────────────────────────────────
    // Sans lui, Codex ne connaît pas les modèles du proxy : ni /model, ni
    // contexte, ni niveaux de raisonnement. Le fichier remplace le
    // catalogue intégré ; chaque modèle doit porter les instructions de
    // base (~20 Ko), sinon Codex refuse le fichier.
    const CATALOG_FILE = "llm-proxy-models.json";
    // Les instructions de base de Codex CLI 0.167.2 (Apache-2.0, extraites
    // de son catalogue intégré), lues une fois, quand Codex est choisi.
    const baseInstructions = ref("");
    const baseError = ref("");
    let baseRead = null;
    watch(() => form.client, (c) => {
      if (baseRead || !["codex", "codexapp"].includes(c)) return;
      baseRead = fetch(new URL("./codex-base-instructions.txt", import.meta.url))
        .then((r) => (r.ok ? r.text() : Promise.reject(new Error("HTTP " + r.status))))
        .then((t) => { baseInstructions.value = t; },
              (e) => { baseRead = null; baseError.value = "Instructions de base de Codex illisibles : " + e.message; });
    }, { immediate: true });
    // Les modèles de conversation, et ceux que l'on retire du catalogue :
    // les décochés, gardés par le navigateur (un modèle nouveau arrive
    // coché). Le modèle de départ y reste toujours.
    const chatIds = computed(() => catalogState.data.filter((m) => m.chat).map((m) => m.id).sort());
    const codexSkip = VueUse.useLocalStorage("llm-proxy-codex-skip", []);
    const skipped = () => (Array.isArray(codexSkip.value) ? codexSkip.value : []);
    const codexPicked = computed({
      get: () => chatIds.value.filter((id) => id === form.model || !skipped().includes(id)),
      set: (ids) => {
        // Ceux d'aujourd'hui décochés (jamais le modèle de départ, dont la
        // case est figée) ; ceux qui ne sont plus au catalogue, gardés.
        codexSkip.value = [...skipped().filter((id) => !chatIds.value.includes(id)),
                           ...chatIds.value.filter((id) => id !== form.model && !ids.includes(id))];
      },
    });
    const codexPick = (all) => { codexPicked.value = all ? chatIds.value : []; };
    const codexCatalog = (current) => {
      const chat = catalogState.data.filter((m) => m.chat);
      const ids = [...codexPicked.value];
      if (current && !ids.includes(current)) ids.unshift(current);
      const by = Object.fromEntries(chat.map((m) => [m.id, m]));
      return { models: ids.map((id, i) => {
        const m = by[id] || {};
        return {
          slug: id, display_name: id, description: "Via llm-proxy",
          // Ceux que déclare son backend ([backends.<nom>].model_efforts),
          // sinon low, medium, high. Sans niveau par défaut : rien n'est
          // envoyé tant qu'on n'en choisit pas un (/model), le backend
          // garde le sien.
          supported_reasoning_levels: (m.efforts || ["low", "medium", "high"])
            .map((e) => ({ effort: e, description: e })),
          shell_type: "unified_exec", visibility: "list", supported_in_api: true,
          priority: i + 1, support_verbosity: false,
          truncation_policy: { mode: "bytes", limit: 10000 },
          experimental_supported_tools: [],
          base_instructions: baseInstructions.value,
          ...(m.context ? { context_window: m.context, max_context_window: m.context } : {}),
          input_modalities: m.vision ? ["text", "image"] : ["text"],
        };
      }) };
    };

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
      const mcp = client.value.mcp && (client.value.mcpFixed || form.mcp);
      if (mcp && !mcpOn.value) {
        warn.push("Le serveur MCP du proxy est désactivé ([mcp_server] enabled = false) : "
          + "les outils ne seront pas joignables.");
      }
      const shell = (name, content, note) => files.push({ name, content, note, lang: "sh" });
      // pi, omp et OpenCode sous Windows : pas de profil du shell, l'adresse
      // et la clé en variables de l'utilisateur, lues par l'extension (et
      // par l'en-tête du mcp.json de pi / omp).
      const winProxyVars = (forWhat) => {
        const set = (name, value) => `[Environment]::SetEnvironmentVariable(${q(name)}, ${value}, "User")\n`;
        files.push({ name: "Windows : les variables de l'utilisateur (PowerShell)", lang: "powershell",
          download: "llm-proxy-env.ps1",
          note: "L'adresse du proxy et la clé, " + forWhat + " Saisie masquée, hors de l'historique. "
            + "Lues par les terminaux ouverts ensuite.",
          content: set("LLM_PROXY_URL", q(origin))
            + (auth.value
              ? `$s = Read-Host "Clé du proxy" -AsSecureString\n`
                + `$k = [Net.NetworkCredential]::new("", $s).Password\n`
                + set("LLM_PROXY_API_KEY", "$k") + (env !== "LLM_PROXY_API_KEY" ? set(env, "$k") : "")
              : set("LLM_PROXY_API_KEY", q("unused")) + (env !== "LLM_PROXY_API_KEY" ? set(env, q("unused")) : "")) });
      };
      if (auth.value && form.client !== "openwebui") {
        shell(["codex", "codexapp", "pi", "opencode"].includes(form.client) ? "Linux, macOS : la clé, une fois" : "La clé, une fois", KEY_SETUP,
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
        case "codex":
        case "codexapp": {
          const app = form.client === "codexapp";
          if (!responsesOn.value) warn.push("La surface Responses est désactivée sur ce proxy "
            + "([responses] enabled = false) : Codex ne pourra pas s'y connecter.");
          const ctx = contexts.value[form.model];
          const cat = form.catalog;
          let toml = `model = ${q(model)}\n`
            + (cat ? `# Les modèles du proxy pour /model, avec leur contexte (relatif à ~/.codex).\n`
              + `model_catalog_json = ${q(CATALOG_FILE)}\n`
              : ctx ? `# Contexte du modèle (catalogue du proxy) : Codex ne le connaît pas.\n`
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
          files.push({ name: "~/.codex/config.toml (Windows : %USERPROFILE%\\.codex\\config.toml)",
            download: "config.toml", lang: "toml",
            note: "À fusionner si le fichier existe déjà : les lignes avant le premier [en-tête] "
              + "tout en haut du fichier — collées sous une section existante (Codex pour Windows ajoute "
              + "[windows]), elles en feraient partie et seraient ignorées (codex doctor : "
              + "« windows.model is ignored »). Sous Windows : nommé config.toml (pas "
              + "config.toml.txt), en UTF-8." + (app ? " Le même fichier que Codex CLI : "
              + "l'application ne lit pas les profils (--profile), d'où model et model_provider en tête." : ""),
            content: toml });
          if (cat) {
            files.push({ name: `~/.codex/${CATALOG_FILE}`, download: CATALOG_FILE, lang: "json", hide: true,
              note: `Les ${codexCatalog(form.model).models.length} modèles de conversation cochés, `
                + "complet : à télécharger dans ~/.codex (Windows : %USERPROFILE%\\.codex). "
                + "Re-télécharger quand les modèles du proxy changent.",
              content: baseInstructions.value
                ? JSON.stringify(codexCatalog(form.model), null, 1) + "\n" : "" });
            if (!baseInstructions.value) warn.push(baseError.value
              || "Instructions de base de Codex en cours de lecture…");
          }
          // Windows : une variable d'utilisateur, lue par les terminaux et
          // les applications ouverts ensuite (le profil du shell n'existe pas).
          const winKey = () => files.push({ name: "Windows : la variable de l'utilisateur (PowerShell)",
            lang: "powershell", download: "llm-proxy-key.ps1",
            note: "Saisie masquée, hors de l'historique. Variable d'environnement de l'utilisateur, "
              + "lue par les terminaux et applications ouverts ensuite.",
            content: `$s = Read-Host "Clé du proxy" -AsSecureString\n`
              + `[Environment]::SetEnvironmentVariable(${q(env)}, `
              + `[Net.NetworkCredential]::new("", $s).Password, "User")\n` });
          if (!app) {
            if (auth.value) {
              shell("~/.bashrc (ou ~/.zshrc)", exportLine + "\n", "La clé, par l'environnement (Linux, macOS).");
              winKey();
            }
            steps.push("Poser config.toml" + (auth.value ? ", poser la clé (profil du shell ou "
              + "variable Windows), ouvrir un nouveau terminal" : "") + ", lancer `codex`.",
              "`codex doctor`, section Configuration : config.toml lu (parse ok) et le modèle du proxy "
              + "avec llm-proxy. Un modèle OpenAI à la place : le fichier n'est pas lu.");
            if (auth.value) steps.push("Codex interactif passe par un démon (`codex app-server`) qui garde "
              + "l'environnement de son lancement : après un changement de clé, le relancer — "
              + "`pkill -f 'codex app-server'` (Windows : `Get-Process codex | Stop-Process`), "
              + "puis `codex`. Sinon : 401 malgré la bonne clé.");
          } else {
            // Une application lancée du Dock ou du menu Démarrer ne lit pas
            // le profil du shell : la variable doit vivre dans la session.
            if (auth.value) {
              shell("macOS : la variable pour les applications",
                `launchctl setenv ${env} "$(cat ${KEYFILE})"\n`,
                "Une application ouverte du Dock ne lit pas ~/.zshrc. launchctl ne survit pas "
                + "au redémarrage : ajouter aussi cette ligne à ~/.zshrc la repose à chaque terminal.");
              winKey();
            }
            steps.push("Poser config.toml" + (auth.value ? ", poser la variable (macOS ou Windows)" : "") + ".",
              "Quitter complètement Codex (⌘Q, ou « Quitter » dans la zone de notification sous Windows) "
              + "puis le rouvrir : il ne relit la configuration et l'environnement qu'au lancement.",
              "L'application prend le modèle et le fournisseur par défaut de config.toml. Une connexion "
              + "ChatGPT ne servirait qu'aux tâches dans le cloud, qui ne passent pas par le proxy.");
            if (auth.value) steps.push("Après un changement de clé : reposer la variable, puis quitter et "
              + "rouvrir Codex (il embarque son propre `codex app-server`). Sinon : 401 malgré la bonne clé.");
          }
          if (cat) steps.push("Le catalogue remplace celui de Codex : /model ne propose plus que les "
            + "modèles du proxy. Il n'est lu qu'au lancement.");
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
            "Linux, macOS. Lus par l'extension llm-proxy.ts ci-dessous et par l'en-tête du mcp.json ; "
            + "pi refuse un en-tête dont la variable n'est pas définie.");
          winProxyVars("pour l'extension et le mcp.json.");
          files.push(extFile("pi-llm-proxy.ts",
            "~/.pi/agent/extensions/llm-proxy.ts et ~/.omp/agent/extensions/llm-proxy.ts",
            "L'extension du provider : lit GET /v1/models du proxy au démarrage "
            + "et enregistre ses modèles sous le provider llmproxy. Le même fichier pour pi et omp "
            + "(Windows : %USERPROFILE%\\.pi\\agent\\extensions et %USERPROFILE%\\.omp\\agent\\extensions)."));
          shell("Lancer", `pi --model llmproxy/${model}\n`,
            "Avec l'extension llm-proxy.ts, les modèles se nomment llmproxy/<backend>/<modèle>.");
          steps.push("Poser llm-proxy.ts dans ~/.pi/agent/extensions/ et ~/.omp/agent/extensions/"
            + (mcp ? ", et chaque mcp.json à côté" : "") + ", ajouter les lignes au profil du shell "
            + "(Windows : lancer le bloc PowerShell), rouvrir le terminal.",
            "Rien dans /model : `omp models llmproxy` (ou `pi --list-models llmproxy`) affiche l'erreur "
            + "de l'extension, que l'interface ne montre pas — adresse injoignable (ECONNREFUSED, nom "
            + "d'hôte inconnu) ou clé refusée (HTTP 401). Sans aucune erreur : l'extension n'est pas "
            + "chargée (mauvais dossier).");
          if (mcp) steps.push("Les outils apparaissent comme mcp__llmproxy__web_search (pi) "
            + "ou mcp__llmproxy_web_search (omp).");
          break;
        }
        case "opencode": {
          files.push(extFile("opencode-llm-proxy.ts", "~/.config/opencode/plugins/llm-proxy.ts",
            "Le plugin (OpenCode 2.x) : un provider llmproxy dont les modèles sont lus "
            + "sur GET /v1/models du proxy (au démarrage, puis toutes les 5 min), et le serveur MCP du "
            + "proxy. Il retire les outils websearch et webfetch d'OpenCode : la recherche est celle du proxy."));
          shell("~/.bashrc (ou ~/.zshrc)",
            `export LLM_PROXY_URL=${origin}\n${exportLine}\n`
            + (env !== "LLM_PROXY_API_KEY" ? `export LLM_PROXY_API_KEY="$${env}"\n` : ""),
            "Linux, macOS. Lus par le plugin, pour le provider et pour le serveur MCP.");
          winProxyVars("pour le plugin.");
          shell("Lancer", `opencode --model llmproxy/${model}\n`,
            "Avec le plugin, les modèles se nomment llmproxy/<backend>/<modèle>.");
          steps.push("Poser llm-proxy.ts dans ~/.config/opencode/plugins/ (chargé d'office ; Windows : "
            + "%USERPROFILE%\\.config\\opencode\\plugins), ajouter les lignes au profil du shell "
            + "(Windows : lancer le bloc PowerShell), rouvrir le terminal.",
            "Proxy injoignable au démarrage : aucun modèle llmproxy (le plugin l'écrit sur la console), "
            + "relancer OpenCode une fois le proxy joignable.",
            "Les outils du proxy apparaissent sous le serveur MCP llmproxy.");
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
        case "omarchy": {
          const head = `[Unit]\nDescription=Usage du proxy LLM pour le panneau d'agents d'Omarchy\n\n`;
          files.push({ name: `~/.config/systemd/user/${OMARCHY_UNIT}.service`, lang: "ini",
            download: `${OMARCHY_UNIT}.service`,
            content: head + `[Service]\nType=oneshot\nExecStart=/bin/sh -c '${omarchyRun(auth.value)}'\n` });
          files.push({ name: `~/.config/systemd/user/${OMARCHY_UNIT}.timer`, lang: "ini",
            download: `${OMARCHY_UNIT}.timer`,
            content: head + `[Timer]\nOnBootSec=1min\nOnUnitActiveSec=2min\n\n[Install]\nWantedBy=timers.target\n` });
          shell("Mise en route", `systemctl --user daemon-reload\nsystemctl --user enable --now ${OMARCHY_UNIT}.timer\n`,
            "Toutes les 2 minutes, l'usage et les limites du compte de la clé.");
          steps.push(`Le panneau affiche « LLM proxy » dès le premier dépôt de ${OMARCHY_DIR}/llm-proxy.json.`);
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
      if (form.client === "omarchy") {
        shell("Vérifier", `systemctl --user status ${OMARCHY_UNIT}.service\n`
          + `jq '{ready, limits}' ${OMARCHY_DIR}/llm-proxy.json\n`, "Le dernier dépôt et ce qu'il contient.");
        return { files, steps, warn };
      }
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
             chatIds, codexPicked, codexPick, copied, copy, download };
  },
};

export default ClientsPart;
</script>

<template>
    <div class="config-grid">
      <form class="panel config-form" @submit.prevent>
        <div class="panel-head"><h2>Client</h2></div>
        <div class="config-fields">
          <label class="field"><span>Client</span>
            <select v-model="form.client">
              <option v-for="c in clients" :key="c.id" :value="c.id">{{ c.label }}</option>
            </select></label>
          <label class="field" v-if="!client.service"><span>Modèle</span>
            <select v-if="models.length" v-model="form.model">
              <option v-for="m in models" :key="m" :value="m">{{ m }}</option></select>
            <input v-else v-model="form.model" placeholder="backend/modèle" spellcheck="false"></label>
          <label class="field" v-if="form.client === 'claude'"><span>Modèle des tâches de fond</span>
            <select v-if="models.length" v-model="form.small">
              <option value="">— aucun —</option>
              <option v-for="m in models" :key="m" :value="m">{{ m }}</option></select>
            <input v-else v-model="form.small" spellcheck="false"></label>
          <label class="check" v-if="(client.mcp && !client.mcpFixed) || ['python', 'curl'].includes(form.client)">
            <input type="checkbox" v-model="form.mcp">
            <template v-if="client.mcp">les outils du proxy par son serveur MCP</template>
            <template v-else>déclarer la recherche web du proxy</template></label>
          <label class="check" v-if="['codex', 'codexapp'].includes(form.client)">
            <input type="checkbox" v-model="form.catalog">
            le catalogue des modèles du proxy (/model, contexte)</label>
          <fieldset class="backend-models" v-if="['codex', 'codexapp'].includes(form.client) && form.catalog && chatIds.length">
            <legend>Modèles du catalogue <span class="dim">(le modèle de départ y reste)</span></legend>
            <div class="item-actions">
              <button type="button" class="btn ghost" @click="codexPick(true)">Tout cocher</button>
              <button type="button" class="btn ghost" @click="codexPick(false)">Tout décocher</button>
              <span class="dim">{{ codexPicked.length }} / {{ chatIds.length }}</span></div>
            <div class="model-picks">
              <label class="check" v-for="m in chatIds" :key="m">
                <input type="checkbox" :value="m" v-model="codexPicked" :disabled="m === form.model">
                <code>{{ m }}</code></label></div>
          </fieldset>
          <label class="field" v-if="!client.service"><span>Variable de la clé</span>
            <input v-model="form.env" spellcheck="false"></label>
          <p class="note" v-if="!auth && !client.service">Le proxy est ouvert : la clé est libre, la variable vaut
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
              <button type="button" class="btn ghost" v-if="!f.hide" :disabled="!f.content" @click="copy(f)">{{ copied === f.name ? "Copié" : "Copier" }}</button>
              <button type="button" class="btn ghost" :disabled="!f.content" @click="download(f)">Télécharger</button>
            </span>
          </header>
          <p class="note" v-if="f.note">{{ f.note }}</p>
          <details class="config-collapse" v-if="f.collapse && f.content">
            <summary>Afficher le fichier ({{ f.content.split("\n").length - 1 }} lignes)</summary>
            <pre>{{ f.content }}</pre>
          </details>
          <pre v-else-if="!f.hide && !f.collapse">{{ f.content }}</pre>
        </section>
        <section class="panel config-steps" v-if="clientOut.steps.length">
          <div class="panel-head"><h2>Étapes</h2></div>
          <ol><li v-for="s in clientOut.steps" :key="s">{{ s }}</li></ol>
        </section>
      </div>
    </div>
</template>
