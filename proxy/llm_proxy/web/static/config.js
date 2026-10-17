/*
  L'onglet « Configuration » : des GÉNÉRATEURS de configuration, tout dans
  le navigateur — rien n'est écrit sur le proxy. Un composant à part,
  enregistré par dashboard.js sous <config-panel>.

  CLIENTS. On choisit le client (Claude Code, Codex, pi / omp, SDK OpenAI,
  curl, Open WebUI, tout client OpenAI), le modèle du catalogue
  (/ui/models), et s'il prend aussi les outils du proxy par son serveur
  MCP (/mcp). La page produit les fichiers à poser, avec leur chemin, et
  les étapes. La CLÉ n'y figure jamais : elle vit dans un fichier à part
  (~/.config/llm-proxy.key, mode 600, posé une fois), que le profil du
  shell exporte dans une variable (LLM_PROXY_API_KEY par défaut).

  PROXY. Un formulaire guidé pour data/config.toml : accès, backends,
  surfaces, outils, chat. Prérempli d'après la configuration en vigueur
  (/ui/config) ; ce qu'il change s'applique sur elle (/ui/config/merge),
  et la page ne montre qu'UN fichier, complet.
  Les secrets sont des «${VAR}» dans le TOML ; leurs valeurs vont dans un
  .env à part, que la page produit aussi (clés du proxy tirées au hasard
  dans le navigateur, à la demande). Le fichier montre aussi les défauts
  (« # défaut »), masque les secrets écrits en clair, se modifie et se
  vérifie dans la page — jamais écrit sur le proxy (config_view.py).

  CLÉS et COMPTES, avec les comptes OIDC seulement ([oidc]). Chacun crée
  ses clés (des uuid4, /ui/keys) pour ses clients — montrées UNE fois, à
  la création — et les révoque. Un admin voit aussi celles de tous et les
  comptes (/ui/users) : accepter un compte en attente, bloquer, promouvoir.
  Le générateur « Proxy » est alors réservé aux admins.
*/
// Où vit la clé du proxy sur le poste, et comment l'y poser sans
// qu'elle passe par l'historique du shell.
const KEYFILE = "~/.config/llm-proxy.key";
const KEY_SETUP = `(umask 077; read -rsp "Clé du proxy : " k && echo && printf '%s' "$k" > ${KEYFILE})\n`;

window.ConfigPanel = {
  props: {
    session: { type: Object, required: true },
    health: { type: Object, default: null },
  },
  setup(props) {
    const { ref, reactive, computed, watch, onMounted, onBeforeUnmount, nextTick } = Vue;
    const origin = window.location.origin;
    const STORE_PART = "llm-proxy-config-part";

    // Les comptes OIDC : le compte connecté, et s'il est admin.
    const account = computed(() => props.session.account || null);
    const isAdmin = computed(() => !!account.value && account.value.role === "admin");
    const parts = computed(() => [
      { id: "clients", label: "Clients" },
      ...(account.value ? [{ id: "keys", label: "Clés" }] : []),
      // Ce que l'outil `memory` a gardé pour vous (memory.js).
      ...(props.session.memory ? [{ id: "memory", label: "Mémoire" }] : []),
      ...(isAdmin.value ? [{ id: "accounts", label: "Comptes" }] : []),
      ...(!account.value || isAdmin.value ? [{ id: "proxy", label: "Proxy" }] : []),
    ]);
    const stored = ref((() => {
      try { return localStorage.getItem(STORE_PART) || "clients"; }
      catch { return "clients"; }
    })());
    // « settings » : l'ancien onglet Paramètres, fondu dans Proxy.
    const part = computed(() => {
      const want = stored.value === "settings" ? "proxy" : stored.value;
      return parts.value.some((p) => p.id === want) ? want : "clients";
    });
    function setPart(p) {
      stored.value = p;
      try { localStorage.setItem(STORE_PART, p); } catch { /* sans stockage */ }
      if (p === "keys") loadKeys();
      if (p === "accounts") loadUsers();
      if (p === "proxy" && !settings.loaded) loadSettings();
    }

    // ── la configuration complète : UN fichier ───────────────────────────
    // GET /ui/config : tout ce que le proxy lit, valeurs du fichier ou
    // défauts (« # défaut »), secrets masqués, et ces valeurs pour
    // préremplir le formulaire. Le formulaire s'y applique (POST
    // /ui/config/merge) : `generated`, le fichier complet qu'il donne.
    // `text` : ce qu'affiche la page — `generated`, tant qu'on ne l'a pas
    // modifié à la main. Vérifié par le proxy (POST /ui/config/check),
    // copié, téléchargé — jamais écrit sur lui.
    const settings = reactive({ loaded: false, path: "", defaults: 0, values: null,
                                defaultPaths: [], original: "",
                                generated: "", text: "", error: "", check: null, checking: false });
    async function loadSettings() {
      settings.error = "";
      settings.check = null;
      try {
        const r = await call("GET", "/ui/config");
        Object.assign(settings, { loaded: true, path: r.path, defaults: r.defaults,
                                  values: r.values, defaultPaths: r.default_paths || [],
                                  original: r.toml, generated: r.toml, text: r.toml });
        loadAdvanced();
        if (!prefilled.value) prefill();
      } catch (e) { settings.error = e.message; }
    }
    async function checkSettings() {
      settings.checking = true;
      settings.check = null;
      try { settings.check = await call("POST", "/ui/config/check", { toml: settings.text }); }
      catch (e) { settings.error = e.message; }
      finally { settings.checking = false; }
    }
    watch(() => settings.text, () => { settings.check = null; });
    const settingsFile = computed(() => ({ name: "config.toml", download: "config.toml",
                                           content: settings.text }));
    const edited = computed(() => settings.text !== settings.generated);

    // ── clés et comptes (OIDC) ──────────────────────────────────────────
    const keys = ref([]);
    const tomlKeys = ref([]);
    const allKeys = ref(false);         // admin : les clés de tous
    const keyLabel = ref("");
    const fresh = ref(null);            // la clé tout juste créée, en clair
    const keyError = ref("");
    const armed = ref(null);            // révocation : deuxième clic attendu
    const users = ref([]);
    const userError = ref("");
    const ROLES = [
      { id: "pending", label: "en attente" }, { id: "user", label: "utilisateur" },
      { id: "admin", label: "admin" }, { id: "blocked", label: "bloqué" },
    ];
    const roleLabel = (r) => (ROLES.find((x) => x.id === r) || { label: r }).label;
    const pendingCount = computed(() => users.value.filter((u) => u.role === "pending").length);
    function when(t) {
      return t ? new Date(t * 1000).toLocaleString("fr-FR",
        { dateStyle: "short", timeStyle: "short" }) : "—";
    }
    async function call(method, url, body) {
      const r = await fetch(url, {
        method, headers: { accept: "application/json",
          ...(body ? { "content-type": "application/json" } : {}) },
        body: body ? JSON.stringify(body) : undefined,
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error((data.error || {}).message || "HTTP " + r.status);
      return data;
    }
    // Le partage équitable : la part du compte connecté et sa consommation
    // sur l'heure glissante (relues avec les clés).
    const fair = ref(props.session.fair_share || null);
    const num = (n) => Number(n).toLocaleString("fr-FR");
    async function loadKeys() {
      if (!account.value) return;
      call("GET", "/ui/session").then((s) => { fair.value = s.fair_share || null; })
        .catch(() => {});
      try {
        const d = await call("GET", "/ui/keys" + (allKeys.value ? "?all=1" : ""));
        keys.value = d.data || [];
        tomlKeys.value = d.toml || [];
        keyError.value = "";
      } catch (e) { keyError.value = e.message; }
    }
    async function createKey() {
      keyError.value = "";
      try {
        fresh.value = await call("POST", "/ui/keys", { label: keyLabel.value });
        keyLabel.value = "";
        await loadKeys();
      } catch (e) { keyError.value = e.message; }
    }
    async function revokeKey(k) {
      if (armed.value !== k.id) {
        armed.value = k.id;
        setTimeout(() => { if (armed.value === k.id) armed.value = null; }, 3000);
        return;
      }
      armed.value = null;
      try {
        await call("DELETE", "/ui/keys/" + encodeURIComponent(k.id));
        if (fresh.value && fresh.value.id === k.id) fresh.value = null;
        await loadKeys();
      } catch (e) { keyError.value = e.message; }
    }
    async function loadUsers() {
      if (!isAdmin.value) return;
      try {
        users.value = (await call("GET", "/ui/users")).data || [];
        for (const u of users.value) if (u.share) customShare[u.id] = u.share;
        userError.value = "";
      } catch (e) { userError.value = e.message; }
    }
    // La part d'un compte (admin) : défaut (null), illimitée (0), ou un
    // nombre de tokens par heure glissante.
    const shareMode = (u) => (u.share === null || u.share === undefined ? "default"
      : u.share === 0 ? "unlimited" : "custom");
    const customShare = reactive({});
    async function setShare(u, share) {
      try {
        await call("PATCH", "/ui/users/" + u.id, { share });
        userError.value = "";
      } catch (e) { userError.value = e.message; }
      await loadUsers();
    }
    function pickShare(u, mode) {
      if (mode === "default") return setShare(u, null);
      if (mode === "unlimited") return setShare(u, 0);
      customShare[u.id] = u.share || (fair.value && fair.value.default) || 100000;
      return setShare(u, customShare[u.id]);
    }
    function typeShare(u) {
      const n = Math.round(Number(customShare[u.id]));
      if (n > 0) setShare(u, n);
    }
    async function setRole(u, role) {
      if (role === u.role) return;
      try {
        await call("PATCH", "/ui/users/" + u.id, { role });
        userError.value = "";
      } catch (e) { userError.value = e.message; }
      await loadUsers();
    }
    // Une clé du TOML rattachée à un compte (admin) : elle écrit dans sa
    // mémoire et suit son état. "" = détachée.
    async function bindToml(k, value) {
      try {
        await call("PUT", "/ui/keys/toml/" + encodeURIComponent(k.api_key_id),
          { user_id: value === "" ? null : Number(value) });
        keyError.value = "";
      } catch (e) { keyError.value = e.message; }
      await loadKeys();
    }
    watch(allKeys, (on) => { loadKeys(); if (on) loadUsers(); });

    // ── utilitaires ─────────────────────────────────────────────────────
    // Une chaîne TOML « basique » : les échappements de JSON en sont un
    // sous-ensemble valide.
    const q = (s) => JSON.stringify(String(s ?? ""));
    const list = (xs) => "[" + xs.map(q).join(", ") + "]";
    const envName = (s) => String(s || "").toUpperCase().replace(/[^A-Z0-9_]/g, "_")
      .replace(/^(\d)/, "_$1");
    function randomKey() {
      const b = new Uint8Array(24);
      crypto.getRandomValues(b);
      return "sk-" + [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
    }
    const copied = ref("");
    async function copy(file) {
      try {
        await navigator.clipboard.writeText(file.content);
        copied.value = file.name;
        setTimeout(() => { if (copied.value === file.name) copied.value = ""; }, 1400);
      } catch { /* refusé par le navigateur : le texte reste sélectionnable */ }
    }
    function download(file) {
      const blob = new Blob([file.content], { type: "text/plain;charset=utf-8" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = file.download || file.name.split("/").pop();
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    }

    // ── catalogue ───────────────────────────────────────────────────────
    const models = ref([]);
    const contexts = ref({});         // id → fenêtre de contexte, si connue
    async function loadModels() {
      try {
        const r = await fetch("/ui/models", { headers: { accept: "application/json" } });
        if (!r.ok) return;
        const data = (await r.json()).data || [];
        models.value = data.map((m) => m.id).sort();
        contexts.value = Object.fromEntries(data.filter((m) => m.context).map((m) => [m.id, m.context]));
      } catch { /* hors ligne : saisie libre */ }
    }
    // Le catalogue complet (images, voix, OCR compris) pour les outils
    // qui visent un modèle spécialisé : /ui/models n'a que ceux de chat.
    const allModels = ref([]);
    async function loadAllModels() {
      try {
        const r = await fetch("/ui/models?all=1", { headers: { accept: "application/json" } });
        if (r.ok) allModels.value = ((await r.json()).data || []).map((m) => m.id).sort();
      } catch { /* les modèles de chat suffiront */ }
    }
    onMounted(() => {
      loadModels(); loadAllModels();
      if (part.value === "keys") loadKeys();
      if (part.value === "proxy") loadSettings();
      loadUsers();                    // admin : le nombre de comptes en attente
    });

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

    // ══ PROXY (config.toml) ═════════════════════════════════════════════
    const TOOL_LIST = [
      { id: "web_search", label: "Recherche web (SearXNG)" },
      { id: "web_fetch", label: "Lecture de pages" },
      { id: "web_grep", label: "Recherche dans une page" },
      { id: "code_execution", label: "Exécution de code" },
      { id: "image_generation", label: "Génération d'images", model: true },
      { id: "ocr", label: "OCR", model: true },
      { id: "transcribe", label: "Transcription", model: true },
      { id: "memory", label: "Mémoire persistante" },
    ];
    const px = reactive({
      keys: [],                  // [{ name, value }] : name = variable d'env
      backends: [],              // [{ name, url, key, quotas, images, toolChoice, maxTokens, fairShare }]
      anthropic: true, defaultModel: "", responses: true,
      hostedTools: true, always: "", alwaysExcept: "",
      tools: Object.fromEntries(TOOL_LIST.map((t) => [t.id, false])),
      toolModels: { image_generation: "", ocr: "", transcribe: "" },
      searxng: "http://searxng:8080", executor: "http://executor:8080",
      maxCalls: 16, mcpServer: true, uiChat: false, chatModel: "", publicUrl: "",
      retention: 90, logLevel: "INFO",
      // Partage équitable : part par défaut (tokens / heure, 0 = aucune
      // limite) ; routes hors quotas et hors partage ([proxy].exempt_paths).
      fairShare: 0, exempt: "/embeddings, /rerank, /audio/transcriptions, /ocr",
      // Comptes ([oidc]) : client_secret va dans le .env (client_id aussi,
      // sauf s'il est déjà écrit en clair : ce n'est pas un secret).
      oidc: { on: false, issuer: "", redirectUri: "", newAccounts: "pending",
              sessionDays: 7, maxKeys: 50, label: "Se connecter",
              clientId: "", idVar: "OIDC_CLIENT_ID", secretVar: "OIDC_CLIENT_SECRET" },
      executorVar: "",                // la variable du jeton, lue dans le fichier
    });
    const prefilled = ref(false);

    // Préremplissage depuis la configuration en vigueur (GET /ui/config,
    // `values` : le fichier et les défauts, secrets masqués) — ce que le
    // fichier dit vraiment, pas une estimation : le formulaire ne réécrit
    // que ce qu'on y change.
    const VAR_REF = /^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$/;
    const varOf = (v) => (typeof v === "string" && (VAR_REF.exec(v) || [])[1]) || "";
    function prefill() {
      const c = settings.values;
      if (!c) return;
      const g = (path, d) => {
        const v = path.split(".").reduce((n, k) => (n && typeof n === "object" ? n[k] : undefined), c);
        return v === undefined || v === null ? d : v;
      };
      px.logLevel = g("proxy.log_level", "INFO");
      // Une clé écrite en clair (masquée) devient un ${VAR} : sa valeur va au .env.
      px.keys = g("proxy.api_keys", []).map((k, i) => ({
        name: varOf(k) || "PROXY_API_KEY_" + (i + 1), value: "", plain: !varOf(k) }));
      px.exempt = g("proxy.exempt_paths", []).join(", ");
      px.backends = Object.entries(g("backends", {})).map(([name, b]) => ({
        name, url: b.url || "", key: !!b.api_key, keyVar: varOf(b.api_key), quotas: !!b.quotas,
        images: !!b.images, toolChoice: typeof b.force_tool_choice === "string" ? b.force_tool_choice : "",
        maxTokens: b.max_tokens || "", fairShare: b.fair_share !== false,
      }));
      px.fairShare = g("fair_share.tokens_per_hour", 0);
      const o = g("oidc", {});
      Object.assign(px.oidc, { on: !!(o.issuer && o.client_id), issuer: o.issuer || "",
        redirectUri: o.redirect_uri || "", newAccounts: o.new_accounts || "pending",
        sessionDays: o.session_days || 7, maxKeys: o.max_keys_per_account || 50,
        label: o.label || "Se connecter",
        // client_id n'est pas un secret : écrit en clair, il le reste.
        clientId: varOf(o.client_id) ? "" : (o.client_id || ""),
        secretPlain: !!o.client_secret && !varOf(o.client_secret),
        idVar: varOf(o.client_id) || "OIDC_CLIENT_ID", secretVar: varOf(o.client_secret) || "OIDC_CLIENT_SECRET" });
      px.anthropic = !!g("anthropic.enabled", false);
      px.defaultModel = g("anthropic.model_map.default", "");
      px.responses = !!g("responses.enabled", false);
      px.hostedTools = !!g("chat.hosted_tools", false);
      px.always = g("chat.always", []).join(", ");
      px.alwaysExcept = g("chat.always_except", []).join(", ");
      px.maxCalls = g("tools.max_calls", 8);
      for (const t of TOOL_LIST) px.tools[t.id] = !!g(`tools.${t.id}.enabled`, false);
      for (const id of Object.keys(px.toolModels)) px.toolModels[id] = g(`tools.${id}.model`, "");
      px.searxng = g("tools.web_search.searxng_url", px.searxng);
      px.executor = g("tools.code_execution.url", px.executor);
      px.executorVar = varOf(g("tools.code_execution.token", ""));
      px.mcpServer = !!g("mcp_server.enabled", false);
      px.uiChat = !!g("ui.chat", false);
      px.chatModel = g("ui.chat_model", "");
      px.publicUrl = g("files.public_url", "");
      px.retention = g("stats.retention_days", 90);
      prefilled.value = true;
    }
    // Réinitialiser : relire la configuration, puis le formulaire.
    async function resetProxy() {
      prefilled.value = false;
      await loadSettings();
    }

    // Les modèles d'un menu : le catalogue, et la valeur en place si elle
    // n'y est pas (un modèle de configuration hors catalogue ne se perd
    // pas). `all` : tous les modèles du catalogue complet — l'OCR, la
    // transcription ou l'image visent des modèles qui ne sont pas de chat.
    function modelChoices(current, all = false) {
      const list = all && allModels.value.length ? allModels.value : models.value;
      return current && !list.includes(current) ? [current, ...list] : list;
    }

    const addBackend = () => px.backends.push({ name: "", url: "http://", key: false,
      quotas: false, images: false, toolChoice: "", maxTokens: "", fairShare: true });
    const addKey = () => px.keys.push({ name: "PROXY_API_KEY_" + (px.keys.length + 1), value: randomKey() });
    const csv = (s) => String(s || "").split(/[\s,]+/).map((x) => x.trim()).filter(Boolean);

    const proxyOut = computed(() => {
      const warn = [];
      const env = [];
      const L = [];
      const keyVars = px.keys.map((k) => envName(k.name)).filter(Boolean);
      L.push("[proxy]", `log_level = ${q(px.logLevel)}`,
             `api_keys = ${list(keyVars.map((v) => "${" + v + "}"))}`,
             `exempt_paths = ${list(csv(px.exempt))}`, "");
      for (const k of px.keys) {
        const v = envName(k.name);
        if (v) env.push(`${v}=${k.value || ""}`);
      }
      const oidcOn = px.oidc.on && !!px.oidc.issuer.trim();
      for (const k of px.keys) if (k.plain) warn.push(`Une clé du proxy était écrite en clair : `
        + `elle devient \${${envName(k.name)}}, sa valeur est à mettre dans le .env.`);
      if (!keyVars.length && !oidcOn) warn.push("Aucune clé : le proxy est ouvert à quiconque l'atteint"
        + (px.uiChat ? " — chat de /ui compris, qui dépense des tokens." : "."));

      const share = Math.max(0, Math.round(Number(px.fairShare) || 0));
      L.push("[fair_share]", `tokens_per_hour = ${share}`, "");
      if (share && !keyVars.length && !oidcOn) warn.push("Partage équitable sans effet : proxy ouvert, personne n'est compté.");

      if (px.oidc.on) {
        const o = px.oidc;
        L.push("[oidc]", `issuer = ${q(o.issuer.trim().replace(/\/+$/, ""))}`,
               o.clientId ? `client_id = ${q(o.clientId)}` : `client_id = "\${${o.idVar}}"`,
               `client_secret = "\${${o.secretVar}}"`);
        if (o.redirectUri.trim()) L.push(`redirect_uri = ${q(o.redirectUri.trim())}`);
        L.push(`new_accounts = ${q(o.newAccounts)}`,
               `session_days = ${Math.max(1, Number(o.sessionDays) || 7)}`,
               `max_keys_per_account = ${Math.max(1, Number(o.maxKeys) || 50)}`,
               `label = ${q(o.label || "Se connecter")}`, "");
        if (!o.clientId) env.push(`${o.idVar}=`);
        env.push(`${o.secretVar}=`);
        if (!o.issuer.trim()) warn.push("Comptes OIDC : l'émetteur (issuer) est requis.");
        if (o.secretPlain) warn.push(`Comptes OIDC : client_secret était écrit en clair : il devient `
          + `\${${o.secretVar}}, sa valeur est à mettre dans le .env.`);
        if (!o.redirectUri.trim() && !px.publicUrl.trim()) warn.push("Comptes OIDC : sans adresse de retour "
          + "ni adresse publique, elle est déduite de la requête — en http:// derrière un reverse proxy, "
          + "que le fournisseur refuse.");
      }

      L.push("[anthropic]", `enabled = ${px.anthropic}`);
      if (px.anthropic && px.defaultModel) L.push("", "[anthropic.model_map]", `default = ${q(px.defaultModel)}`);
      L.push("", "[responses]", `enabled = ${px.responses}`, "");
      L.push("[chat]", `hosted_tools = ${px.hostedTools}`,
             `always = ${list(csv(px.always))}`, `always_except = ${list(csv(px.alwaysExcept))}`, "");
      L.push("[tools]", `max_calls = ${Math.max(1, Number(px.maxCalls) || 8)}`, "");
      for (const t of TOOL_LIST) {
        L.push(`[tools.${t.id}]`, `enabled = ${!!px.tools[t.id]}`);
        if (t.id === "web_search") L.push(`searxng_url = ${q(px.searxng)}`);
        if (t.id === "code_execution") {
          // Le jeton : sa variable du fichier, sinon EXECUTOR_TOKEN dès que
          // l'outil est actif (inactif et sans jeton : rien à écrire).
          const tv = px.executorVar || (px.tools[t.id] ? "EXECUTOR_TOKEN" : "");
          L.push(`url = ${q(px.executor)}`);
          if (tv) L.push(`token = "\${${tv}}"`);
          if (tv && px.tools[t.id]) env.push(`${tv}=`);
        }
        if (t.model) {
          const m = px.toolModels[t.id];
          if (m) L.push(`model = ${q(m)}`);
          if (px.tools[t.id] && !m) warn.push(`${t.label} : un modèle est requis.`);
        }
        L.push("");
      }
      L.push("[mcp_server]", `enabled = ${px.mcpServer}`, "");
      L.push("[ui]", `chat = ${px.uiChat}`);
      if (px.chatModel) L.push(`chat_model = ${q(px.chatModel)}`);
      L.push("");
      L.push("[files]", `public_url = ${q(px.publicUrl.replace(/\/+$/, ""))}`, "");
      L.push("[stats]", `retention_days = ${Number(px.retention) || 0}`, "");
      const names = new Set();
      for (const b of px.backends) {
        const name = String(b.name || "").trim();
        if (!/^[a-z0-9][a-z0-9_-]*$/i.test(name)) {
          warn.push(`Backend « ${name || "sans nom"} » : nom invalide (lettres, chiffres, - et _).`);
          continue;
        }
        if (names.has(name)) { warn.push(`Backend « ${name} » : nom en double.`); continue; }
        names.add(name);
        L.push(`[backends.${name}]`, `url = ${q(b.url)}`);
        if (b.key) {
          const v = b.keyVar || envName(name) + "_API_KEY";
          L.push(`api_key = "\${${v}}"`);
          env.push(`${v}=`);
        }
        if (b.quotas) L.push("quotas = true");
        if (b.images) L.push("images = true");
        if (b.toolChoice) L.push(`force_tool_choice = ${q(b.toolChoice)}`);
        if (Number(b.maxTokens) > 0) L.push(`max_tokens = ${Number(b.maxTokens)}`);
        if (!b.fairShare) L.push("fair_share = false");
        L.push("");
      }
      if (!names.size) warn.push("Aucun backend : le proxy n'aurait rien à servir.");
      if (px.always && !px.hostedTools) warn.push("[chat].always est sans effet sans hosted_tools.");
      return {
        warn,
        // Le fichier PARTIEL du formulaire : le proxy l'applique sur la
        // configuration en vigueur (POST /ui/config/merge) — la page ne
        // montre que le fichier complet.
        partial: L.join("\n").replace(/\n{3,}/g, "\n\n"),
        files: [
          { name: ".env", download: "llm-proxy.env", lang: "sh",
            note: "Les secrets que le TOML référence par ${VAR} : à côté du docker-compose.yml, "
              + "jamais dans le dépôt. À compléter là où la valeur est vide.",
            content: env.length ? env.join("\n") + "\n" : "# aucun secret référencé\n" },
        ],
      };
    });

    // Le formulaire change → le fichier complet se recalcule (un instant
    // après la dernière frappe). Modifié à la main : il n'est plus écrasé,
    // « Réappliquer le formulaire » le rend au formulaire.
    let mergeTimer = 0;
    async function mergeForm() {
      if (!prefilled.value) return;
      try {
        const r = await call("POST", "/ui/config/merge", { toml: proxyOut.value.partial,
                                                            overrides: overrides.value });
        const follow = !edited.value;
        settings.generated = r.toml;
        settings.defaults = r.defaults;
        if (follow) settings.text = r.toml;
        settings.error = "";
      } catch (e) { settings.error = e.message; }
    }
    // Ce qui diffère de la configuration en vigueur, en lignes du fichier :
    // une valeur changée compte une fois (sa ligne d'avant, sa ligne d'après).
    const changes = computed(() => {
      if (!settings.loaded) return 0;
      const before = settings.original.split("\n"), after = settings.text.split("\n");
      const a = new Set(before), b = new Set(after);
      return Math.max(after.filter((l) => !a.has(l)).length, before.filter((l) => !b.has(l)).length);
    });
    const showWarn = ref(false);

    // ── avancé : tout réglage que le formulaire ne couvre pas ─────────────
    // Généré depuis la configuration en vigueur (fichier et défauts) : une
    // ligne par réglage, éditable selon son type. Ce qui change part au
    // proxy en `overrides` ({path, value}) avec le formulaire.
    const FORM_FLAT = new Set(["proxy.log_level", "proxy.api_keys", "proxy.exempt_paths",
      "fair_share.tokens_per_hour", "anthropic.enabled", "anthropic.model_map.default",
      "responses.enabled", "chat.hosted_tools", "chat.always", "chat.always_except",
      "tools.max_calls", "mcp_server.enabled", "ui.chat", "ui.chat_model",
      "files.public_url", "stats.retention_days"]);
    const BACKEND_FORM_KEYS = ["url", "api_key", "quotas", "images", "force_tool_choice",
                               "max_tokens", "fair_share"];
    const OIDC_FORM_KEYS = ["issuer", "client_id", "client_secret", "redirect_uri",
                            "new_accounts", "session_days", "max_keys_per_account", "label"];
    function formCovers(path) {
      const [top, a, b] = path;
      if (top === "backends") return path.length === 3 && BACKEND_FORM_KEYS.includes(b);
      if (top === "oidc") return path.length === 2 && OIDC_FORM_KEYS.includes(a);
      if (top === "tools" && path.length === 3 && TOOL_LIST.some((t) => t.id === a)) {
        const tool = TOOL_LIST.find((t) => t.id === a);
        return b === "enabled" || (b === "model" && !!tool.model)
          || (a === "web_search" && b === "searxng_url")
          || (a === "code_execution" && (b === "url" || b === "token"));
      }
      return FORM_FLAT.has(path.join("."));
    }
    const kindOf = (v) => typeof v === "boolean" ? "bool" : typeof v === "number" ? "number"
      : typeof v === "string" ? "string"
      : Array.isArray(v) && v.every((x) => typeof x === "string") ? "list" : "json";
    const asText = (v, kind) => kind === "list" ? v.join(", ")
      : kind === "json" ? JSON.stringify(v) : v;
    const advanced = ref([]);        // [{path, group, key, kind, orig, value, isDefault}]
    const advFilter = ref("");
    function loadAdvanced() {
      const rows = [];
      const defaults = new Set(settings.defaultPaths);
      const walk = (node, path) => {
        for (const [k, v] of Object.entries(node)) {
          const p = [...path, k];
          if (v && typeof v === "object" && !Array.isArray(v)) { walk(v, p); continue; }
          if (formCovers(p)) continue;
          const kind = kindOf(v);
          rows.push({ path: p, group: path.join(".") || "(racine)", key: k, kind, orig: v,
                      value: asText(v, kind), isDefault: defaults.has(p.join(".")), error: "" });
        }
      };
      walk(settings.values || {}, []);
      advanced.value = rows;
    }
    // La valeur saisie, typée comme l'originale : {value} ou {error}.
    function parseAdv(row) {
      if (row.kind === "bool") return { value: row.value };
      if (row.kind === "number") {
        const n = Number(row.value);
        return row.value === "" || Number.isNaN(n) ? { error: "nombre attendu" } : { value: n };
      }
      if (row.kind === "list") return { value: csv(row.value) };
      if (row.kind === "json") {
        try { return { value: JSON.parse(row.value) }; } catch { return { error: "JSON invalide" }; }
      }
      return { value: row.value };
    }
    const advError = (row) => parseAdv(row).error || "";
    const backendNames = computed(() => new Set(px.backends.map((b) => String(b.name || "").trim())));
    const overrides = computed(() => advanced.value.flatMap((row) => {
      const { value, error } = parseAdv(row);
      if (error || JSON.stringify(value) === JSON.stringify(row.orig)) return [];
      // Un backend retiré du formulaire ne revient pas par ses réglages avancés.
      if (row.path[0] === "backends" && !backendNames.value.has(row.path[1])) return [];
      return [{ path: row.path, value }];
    }));
    const advGroups = computed(() => {
      const f = advFilter.value.trim().toLowerCase();
      const groups = new Map();
      for (const row of advanced.value) {
        if (f && !row.path.join(".").toLowerCase().includes(f)) continue;
        if (!groups.has(row.group)) groups.set(row.group, []);
        groups.get(row.group).push(row);
      }
      return [...groups].sort(([x], [y]) => x.localeCompare(y))
        .map(([name, rows]) => ({ name, rows }));
    });
    const advChanged = (row) => {
      const { value, error } = parseAdv(row);
      return !error && JSON.stringify(value) !== JSON.stringify(row.orig);
    };
    watch(() => [proxyOut.value.partial, JSON.stringify(overrides.value)], () => {
      clearTimeout(mergeTimer);
      mergeTimer = setTimeout(mergeForm, 300);
    });

    // ── sommaire : les sections de l'onglet, celle à l'écran en évidence ──
    const SECTIONS = [
      { id: "acces", label: "Accès" }, { id: "backends", label: "Backends" },
      { id: "surfaces", label: "Surfaces" }, { id: "outils", label: "Outils" },
      { id: "partage", label: "Partage équitable" }, { id: "comptes", label: "Comptes (OIDC)" },
      { id: "divers", label: "Divers" }, { id: "avance", label: "Avancé" },
      { id: "fichier", label: "Fichier complet", sep: true }, { id: "env", label: ".env" },
    ];
    const activeSection = ref("acces");
    let observer = null;
    function goTo(id) {
      activeSection.value = id;
      const el = document.getElementById("px-" + id);
      if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    function observeSections() {
      if (observer) observer.disconnect();
      if (!("IntersectionObserver" in window)) return;
      const visible = new Map();
      observer = new IntersectionObserver((entries) => {
        for (const e of entries) visible.set(e.target.dataset.section, e.isIntersecting);
        const first = SECTIONS.find((sct) => visible.get(sct.id));
        if (first) activeSection.value = first.id;
      }, { rootMargin: "-80px 0px -55% 0px" });
      document.querySelectorAll("[data-section]").forEach((el) => observer.observe(el));
    }
    watch(part, async (p) => { if (p === "proxy") { await nextTick(); observeSections(); } },
          { immediate: true });
    onBeforeUnmount(() => observer && observer.disconnect());

    return { KEYFILE, settings, loadSettings, checkSettings, settingsFile, edited, resetProxy,
             changes, showWarn, advanced, advFilter, advGroups, advChanged, advError, SECTIONS, activeSection, goTo, part, parts, setPart, account, isAdmin, keys, tomlKeys, allKeys,
             keyLabel, fresh, keyError, armed, createKey, revokeKey, loadKeys,
             users, userError, bindToml, ROLES, fair, num, shareMode,
             customShare, pickShare, typeShare, roleLabel, pendingCount, setRole, when,
             clients, form, client, models, clientOut, auth, mcpOn,
             TOOL_LIST, px, prefill, modelChoices, addBackend, addKey, randomKey, proxyOut,
             copy, download, copied };
  },
  template: `
  <div class="config-view">
    <div class="toolbar">
      <div class="range" role="group" aria-label="Partie">
        <button type="button" v-for="p in parts" :key="p.id" :aria-pressed="String(part === p.id)"
                @click="setPart(p.id)">{{ p.label }}<template
                v-if="p.id === 'accounts' && pendingCount"> ({{ pendingCount }})</template></button>
      </div>
    </div>

    <div class="config-keys" v-if="part === 'memory'">
      <memory-panel expanded></memory-panel>
    </div>

    <div class="config-keys" v-if="part === 'keys'">
      <p class="panel note fair-note" v-if="fair">Partage équitable :
        <template v-if="fair.share">votre part est de <b>{{ num(fair.share) }}</b> tokens
          par heure glissante ; vous en avez utilisé <b>{{ num(fair.used) }}</b>.
          Au-delà, vos requêtes attendent que la part se libère.</template>
        <template v-else>votre part est <b>illimitée</b> ({{ num(fair.used) }} tokens
          sur la dernière heure).</template></p>
      <form class="panel key-new" @submit.prevent="createKey">
        <div class="panel-head"><h2>Nouvelle clé</h2></div>
        <div class="key-new-row">
          <input v-model="keyLabel" maxlength="80" spellcheck="false"
                 placeholder="libellé : le client qui l'utilisera (Claude Code, portable…)">
          <button type="submit" class="btn">Créer</button>
        </div>
        <p class="err" v-if="keyError" role="status">{{ keyError }}</p>
      </form>
      <section class="panel key-fresh" v-if="fresh">
        <div class="panel-head"><h2>Clé créée{{ fresh.label ? " : " + fresh.label : "" }}</h2>
          <button type="button" class="btn ghost" @click="fresh = null">Fermer</button></div>
        <p class="note">Copiez-la maintenant : le proxy n'en garde qu'un condensé, elle ne
          sera plus affichée. Posez-la dans la variable de votre client
          (onglet <button type="button" class="link" @click="setPart('clients')">Clients</button>).</p>
        <div class="key-copy">
          <code>{{ fresh.key }}</code>
          <button type="button" class="btn ghost" @click="copy({ name: fresh.id, content: fresh.key })">{{ copied === fresh.id ? "Copié" : "Copier" }}</button>
        </div>
        <pre>(umask 077; printf '%s' '{{ fresh.key }}' > {{ KEYFILE }})</pre>
        <p class="note">Le profil du shell la lit (onglet Clients) :</p>
        <pre>export {{ form.env || "LLM_PROXY_API_KEY" }}="$(cat {{ KEYFILE }} 2>/dev/null || echo unused)"</pre>
      </section>
      <section class="panel">
        <div class="panel-head"><h2>{{ allKeys ? "Toutes les clés" : "Vos clés" }}</h2>
          <label class="check" v-if="isAdmin"><input type="checkbox" v-model="allKeys"> tous les comptes</label></div>
        <table v-if="keys.length || tomlKeys.length">
          <thead><tr><th>Libellé</th><th v-if="allKeys">Compte</th><th>Clé</th>
            <th>Créée</th><th>Dernier usage</th><th></th></tr></thead>
          <tbody>
            <tr v-for="k in keys" :key="k.id" :class="{ dim: k.revoked }">
              <td>{{ k.label || "—" }}</td>
              <td v-if="allKeys">{{ k.user }}</td>
              <td><code>{{ k.hint }}…</code></td>
              <td>{{ when(k.created) }}</td>
              <td>{{ when(k.last_used) }}</td>
              <td><span v-if="k.revoked" class="tag">révoquée</span>
                <button v-else type="button" class="btn danger" :class="{ armed: armed === k.id }"
                        @click="revokeKey(k)">{{ armed === k.id ? "Révoquer ?" : "Révoquer" }}</button></td>
            </tr>
            <tr v-for="k in tomlKeys" :key="k.api_key_id">
              <td>proxy.api_keys <span class="tag" title="Écrite dans config.toml : se retire là">TOML</span></td>
              <td v-if="allKeys"><select :value="k.user_id ?? ''" @change="bindToml(k, $event.target.value)"
                  aria-label="Compte de cette clé du TOML"
                  title="Rattachée, la clé écrit dans la mémoire du compte et suit son état ; ce qu'elle avait rangé seule ne bouge pas">
                <option value="">— aucun compte —</option>
                <option v-for="u in users" :key="u.id" :value="u.id">{{ u.email || u.name }}</option></select></td>
              <td><code title="condensé (la clé n'est pas connue de la page)">#{{ k.hint }}</code></td>
              <td>—</td><td>—</td><td></td>
            </tr>
          </tbody>
        </table>
        <p class="note" v-else>Aucune clé pour l'instant.</p>
      </section>
    </div>

    <div class="config-keys" v-if="part === 'accounts'">
      <section class="panel">
        <div class="panel-head"><h2>Comptes</h2></div>
        <p class="err" v-if="userError" role="status">{{ userError }}</p>
        <table>
          <thead><tr><th>Compte</th><th>Clés</th><th>Créé</th><th>Connexion</th>
            <th title="Partage équitable : tokens par heure glissante ; au-delà, les requêtes du compte attendent">Part / h</th><th>Rôle</th></tr></thead>
          <tbody>
            <tr v-for="u in users" :key="u.id">
              <td>{{ u.name || u.email }}<span class="dim" v-if="u.name && u.email"> · {{ u.email }}</span>
                <span class="tag" v-if="account && u.id === account.id">vous</span></td>
              <td>{{ u.keys }}</td>
              <td>{{ when(u.created) }}</td>
              <td>{{ when(u.last_login) }}</td>
              <td><div class="share-cell"><select :value="shareMode(u)" @change="pickShare(u, $event.target.value)"
                    :aria-label="'Part de ' + (u.email || u.name)">
                  <option value="default">défaut{{ fair && fair.default ? " (" + num(fair.default) + ")" : " (aucune limite)" }}</option>
                  <option value="unlimited">illimitée</option>
                  <option value="custom">personnalisée</option></select>
                <input v-if="shareMode(u) === 'custom'" type="number" min="1" step="1000"
                       v-model="customShare[u.id]"
                       @change="typeShare(u)" :placeholder="u.share" aria-label="Tokens par heure"></div></td>
              <td><select :value="u.role" @change="setRole(u, $event.target.value)"
                          :aria-label="'Rôle de ' + (u.email || u.name)">
                <option v-for="r in ROLES" :key="r.id" :value="r.id">{{ r.label }}</option></select></td>
            </tr>
          </tbody>
        </table>
      </section>
      <section class="panel backup-panel">
        <div class="panel-head"><h2>Sauvegarde</h2>
          <a class="btn" href="/ui/admin/backup" download>Télécharger une sauvegarde</a></div>
        <p class="note">Une archive de <code>data/</code>, faite à l'instant : les bases
          (statistiques, mémoire, comptes et clés), <code>client.secret</code> et
          <code>config.toml</code>. Elle contient le secret des condensés et la mémoire
          de tous : à garder comme un mot de passe. Le <code>.env</code> n'y est pas.
          Restauration : <code>python -m llm_proxy.backup --restore &lt;archive&gt;</code>,
          proxy arrêté (voir le README).</p>
      </section>
    </div>

    <div class="config-grid" v-if="part === 'clients'">
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
            <button type="button" class="link" @click="setPart('keys')">Clés</button>.</p>
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

    <div class="proxy-layout" v-if="part === 'proxy'">
      <nav class="proxy-nav" aria-label="Sections">
        <template v-for="sct in SECTIONS" :key="sct.id">
          <hr v-if="sct.sep">
          <button type="button" :aria-current="activeSection === sct.id ? 'true' : null"
                  @click="goTo(sct.id)">{{ sct.label }}<span class="count" v-if="sct.id === 'backends'">{{ px.backends.length }}</span><span
                  class="count" v-else-if="sct.id === 'avance' && advanced.length">{{ advanced.length }}</span></button>
        </template>
      </nav>
      <div class="proxy-main">
        <div class="proxy-bar panel">
          <span class="proxy-state">
            <strong v-if="changes">{{ changes }} ligne{{ changes > 1 ? "s" : "" }} modifiée{{ changes > 1 ? "s" : "" }}</strong>
            <span v-else class="dim">Configuration en vigueur, sans modification</span>
            <button type="button" class="link warn-toggle" v-if="proxyOut.warn.length"
                    @click="showWarn = !showWarn">⚠ {{ proxyOut.warn.length }} avertissement{{ proxyOut.warn.length > 1 ? "s" : "" }}</button>
            <span class="dim" v-if="edited">· fichier modifié à la main</span>
          </span>
          <span class="config-actions">
            <button type="button" class="btn ghost" @click="resetProxy" :disabled="!settings.loaded"
                    title="Relit la configuration en vigueur : formulaire et fichier y reviennent">Réinitialiser</button>
            <button type="button" class="btn ghost" @click="checkSettings"
                    :disabled="!settings.loaded || settings.checking">Vérifier</button>
            <button type="button" class="btn ghost" @click="copy(settingsFile)"
                    :disabled="!settings.loaded">{{ copied === settingsFile.name ? "Copié" : "Copier" }}</button>
            <button type="button" class="btn" @click="download(settingsFile)"
                    :disabled="!settings.loaded">Télécharger config.toml</button>
          </span>
          <ul class="proxy-warn" v-if="showWarn && proxyOut.warn.length">
            <li v-for="w in proxyOut.warn" :key="w">{{ w }}</li>
          </ul>
          <p class="err" v-if="settings.error" role="status">{{ settings.error }}</p>
        <div v-if="settings.check" class="settings-check" role="status">
          <p class="err" v-if="!settings.check.ok">TOML invalide : {{ settings.check.error }}</p>
          <p class="ok" v-else-if="!settings.check.unknown.length">TOML valide, toutes les clés sont connues du proxy.</p>
          <p class="err" v-else>TOML valide, mais le proxy ne lit pas ces clés (faute de frappe, réglage retiré) :
            <code v-for="k in settings.check.unknown" :key="k">{{ k }}</code></p>
          <p class="note" v-if="settings.check.masked">{{ settings.check.masked }} valeur(s) masquée(s) à
            remettre avant usage.</p>
        </div>
        </div>
        <p class="note proxy-intro">Tout part de la configuration en vigueur : ce qu'on change ici
          s'applique au fichier complet (section « Fichier complet »). <strong>Rien n'est écrit sur le
          proxy</strong> : télécharger le fichier, remplacer data/config.toml sur l'hôte (l'ancien en
          .bak, valeurs masquées remises), puis redémarrer.</p>
        <section class="panel proxy-section" id="px-acces" data-section="acces">
          <div class="panel-head"><h2>Accès</h2></div>
          <div class="config-fields">
            <p class="note">Une clé par personne : chacune a ses statistiques, sa mémoire, ses conversations.</p>
            <div class="row" v-for="(k, i) in px.keys" :key="i">
              <input v-model="k.name" spellcheck="false" placeholder="VARIABLE" class="mono">
              <input v-model="k.value" spellcheck="false" placeholder="valeur (dans le .env)" class="mono">
              <button type="button" class="icon" title="Nouvelle valeur" @click="k.value = randomKey()">↻</button>
              <button type="button" class="icon danger" title="Retirer" @click="px.keys.splice(i, 1)">×</button>
            </div>
            <button type="button" class="btn ghost" @click="addKey">Ajouter une clé</button>
          </div>
        </section>
        <section class="panel proxy-section" id="px-backends" data-section="backends">
          <div class="panel-head"><h2>Backends</h2></div>
          <div class="config-fields">
            <div class="backend" v-for="(b, i) in px.backends" :key="i">
              <div class="row">
                <input v-model="b.name" placeholder="nom (préfixe)" spellcheck="false" class="mono">
                <input v-model="b.url" placeholder="http://hôte:port" spellcheck="false" class="mono grow">
                <button type="button" class="icon danger" title="Retirer" @click="px.backends.splice(i, 1)">×</button>
              </div>
              <div class="row checks">
                <label class="check"><input type="checkbox" v-model="b.key"> clé d'API</label>
                <label class="check"><input type="checkbox" v-model="b.quotas"> quotas (Albert)</label>
                <label class="check"><input type="checkbox" v-model="b.images"> images</label>
                <span class="check">tool_choice
                  <select v-model="b.toolChoice" aria-label="tool_choice injecté">
                    <option value="">jamais</option><option value="auto">auto</option>
                    <option value="required">required</option></select></span>
                <span class="check">max_tokens <input v-model="b.maxTokens" class="num" inputmode="numeric"
                  aria-label="max_tokens" placeholder="—"></span>
                <label class="check" title="Décoché : ses requêtes ne comptent pas dans la part des clients (fair_share = false)">
                  <input type="checkbox" v-model="b.fairShare"> partage équitable</label>
              </div>
            </div>
            <button type="button" class="btn ghost" @click="addBackend">Ajouter un backend</button>
          </div>
        </section>
        <section class="panel proxy-section" id="px-surfaces" data-section="surfaces">
          <div class="panel-head"><h2>Surfaces</h2></div>
          <div class="config-fields">
            <label class="check"><input type="checkbox" v-model="px.anthropic"> surface Anthropic (Claude Code)</label>
            <label class="field" v-if="px.anthropic"><span>Modèle par défaut de Claude Code</span>
              <select v-model="px.defaultModel" class="mono">
                <option value="">— aucun —</option>
                <option v-for="m in modelChoices(px.defaultModel)" :key="m" :value="m">{{ m }}</option>
              </select></label>
            <label class="check"><input type="checkbox" v-model="px.responses"> surface Responses (Codex)</label>
            <label class="check"><input type="checkbox" v-model="px.mcpServer"> serveur MCP (/mcp)</label>
            <label class="check"><input type="checkbox" v-model="px.uiChat"> chat de /ui</label>
            <label class="field" v-if="px.uiChat"><span>Modèle présélectionné dans le chat</span>
              <select v-model="px.chatModel" class="mono">
                <option value="">— le dernier choisi, sinon le premier —</option>
                <option v-for="m in modelChoices(px.chatModel)" :key="m" :value="m">{{ m }}</option>
              </select></label>
          </div>
        </section>
        <section class="panel proxy-section" id="px-outils" data-section="outils">
          <div class="panel-head"><h2>Outils hébergés</h2></div>
          <div class="config-fields">
            <label class="check"><input type="checkbox" v-model="px.hostedTools"> sur /v1/chat/completions</label>
            <div class="tools-grid">
              <template v-for="t in TOOL_LIST" :key="t.id">
                <label class="check"><input type="checkbox" v-model="px.tools[t.id]"> {{ t.label }}</label>
                <select v-if="t.model && px.tools[t.id]" v-model="px.toolModels[t.id]" class="mono">
                  <option value="">— choisir le modèle —</option>
                  <option v-for="m in modelChoices(px.toolModels[t.id], true)" :key="m" :value="m">{{ m }}</option>
                </select>
                <input v-else-if="t.id === 'web_search' && px.tools[t.id]" v-model="px.searxng"
                       spellcheck="false" class="mono">
                <input v-else-if="t.id === 'code_execution' && px.tools[t.id]" v-model="px.executor"
                       spellcheck="false" class="mono">
                <span v-else></span>
              </template>
            </div>
            <label class="field"><span>Appels d'outils par réponse</span>
              <input v-model.number="px.maxCalls" type="number" min="1" max="64" class="num"></label>
            <label class="field"><span>Présentés d'office ([chat].always)</span>
              <input v-model="px.always" placeholder="web_search, web_fetch" spellcheck="false" class="mono"></label>
          </div>
        </section>
        <section class="panel proxy-section" id="px-partage" data-section="partage">
          <div class="panel-head"><h2>Partage équitable</h2></div>
          <div class="config-fields">
            <p class="note">Une part de tokens par client sur une heure glissante : au-delà, ses requêtes
              attendent, rien n'est refusé. La part de chaque compte se règle dans Comptes.</p>
            <label class="field"><span>Part par défaut (tokens / heure, 0 = aucune limite)</span>
              <input v-model.number="px.fairShare" type="number" min="0" step="10000" class="num"></label>
            <label class="field"><span>Routes exemptées (quotas et partage)</span>
              <input v-model="px.exempt" placeholder="/embeddings, /ocr" spellcheck="false" class="mono"></label>
          </div>
        </section>
        <section class="panel proxy-section" id="px-comptes" data-section="comptes">
          <div class="panel-head"><h2>Comptes (OIDC)</h2></div>
          <div class="config-fields">
            <label class="check"><input type="checkbox" v-model="px.oidc.on"> connexion par un fournisseur OIDC</label>
            <template v-if="px.oidc.on">
              <p class="note">client_secret va dans le .env. Chez le fournisseur : un client
                confidentiel, retour sur &lt;adresse publique&gt;/ui/auth/callback.</p>
              <label class="field"><span>Émetteur (issuer)</span>
                <input v-model="px.oidc.issuer" placeholder="https://auth.example.org/realms/x" spellcheck="false" class="mono"></label>
              <label class="field"><span>Adresse de retour (vide : adresse publique + /ui/auth/callback)</span>
                <input v-model="px.oidc.redirectUri" placeholder="https://llm.example.org/ui/auth/callback" spellcheck="false" class="mono"></label>
              <label class="field"><span>Nouveaux comptes</span>
                <select v-model="px.oidc.newAccounts">
                  <option value="pending">en attente d'un admin</option>
                  <option value="user">actifs d'emblée</option></select></label>
              <label class="field"><span>Durée d'une session (jours)</span>
                <input v-model.number="px.oidc.sessionDays" type="number" min="1" class="num"></label>
              <label class="field"><span>Clés actives par compte</span>
                <input v-model.number="px.oidc.maxKeys" type="number" min="1" class="num"></label>
              <label class="field"><span>Libellé du bouton</span>
                <input v-model="px.oidc.label" spellcheck="false"></label>
            </template>
          </div>
        </section>
        <section class="panel proxy-section" id="px-divers" data-section="divers">
          <div class="panel-head"><h2>Divers</h2></div>
          <div class="config-fields">
            <label class="field"><span>Adresse publique (liens des fichiers)</span>
              <input v-model="px.publicUrl" placeholder="https://llm.example.org" spellcheck="false" class="mono"></label>
            <label class="field"><span>Rétention des statistiques (jours, 0 = illimitée)</span>
              <input v-model.number="px.retention" type="number" min="0" class="num"></label>
          </div>
        </section>
        <section class="panel proxy-section" id="px-avance" data-section="avance">
          <div class="panel-head"><h2>Avancé</h2>
            <input class="adv-filter" v-model="advFilter" type="search" placeholder="Filtrer (ex. timeout, quotas)"
                   aria-label="Filtrer les réglages avancés" spellcheck="false"></div>
          <p class="note adv-intro">Chaque réglage que les sections ci-dessus ne couvrent pas, à sa valeur
            en vigueur (« défaut » : le fichier ne le dit pas, le proxy le prend d'office).</p>
          <div class="adv-group" v-for="g in advGroups" :key="g.name">
            <h3>[{{ g.name }}]</h3>
            <div class="adv-row" v-for="row in g.rows" :key="JSON.stringify(row.path)"
                 :class="{ changed: advChanged(row) }">
              <label class="adv-key mono" :for="'adv-' + row.path.join('.')">{{ row.key }}</label>
              <span class="adv-val">
                <input v-if="row.kind === 'bool'" :id="'adv-' + row.path.join('.')" type="checkbox" v-model="row.value">
                <input v-else :id="'adv-' + row.path.join('.')" v-model="row.value" spellcheck="false"
                       :class="{ mono: row.kind !== 'number', num: row.kind === 'number' }"
                       :inputmode="row.kind === 'number' ? 'decimal' : null">
                <span class="err" v-if="advError(row)">{{ advError(row) }}</span>
              </span>
              <span class="adv-tags">
                <span class="tag" v-if="row.isDefault && !advChanged(row)">défaut</span>
                <span class="tag changed" v-if="advChanged(row)">modifié</span>
              </span>
            </div>
          </div>
          <p class="note adv-intro" v-if="!advGroups.length">{{ advFilter ? "Aucun réglage ne correspond." : "Chargement…" }}</p>
        </section>
        <section class="panel proxy-section config-settings" id="px-fichier" data-section="fichier">
          <div class="panel-head"><h2>Fichier complet</h2>
            <span class="config-actions">
              <button type="button" class="btn ghost" @click="settings.text = settings.generated"
                      :disabled="!edited" title="Rend au fichier les valeurs des sections">Réappliquer les sections</button>
            </span></div>
          <p class="note"><code>{{ settings.path }}</code> avec ce que les sections y changent ; les valeurs
            que le proxy prend d'office sont suivies de « # défaut » ({{ settings.defaults }}). Les
            <code>\${VAR}</code> renvoient au .env ; une valeur secrète écrite en clair est masquée. Modifiable
            directement : les sections ne l'écrasent plus alors.</p>
          <textarea v-model="settings.text" spellcheck="false" aria-label="config.toml"
                    :placeholder="settings.loaded ? '' : 'Chargement…'"></textarea>
        </section>
        <section class="panel proxy-section config-file" id="px-env" data-section="env"
                 v-for="f in proxyOut.files" :key="f.name">
          <header class="panel-head">
            <h2>{{ f.name }}</h2>
            <span class="config-actions">
              <button type="button" class="btn ghost" @click="copy(f)">{{ copied === f.name ? "Copié" : "Copier" }}</button>
              <button type="button" class="btn ghost" @click="download(f)">Télécharger</button>
            </span>
          </header>
          <p class="note" v-if="f.note">{{ f.note }}</p>
          <pre>{{ f.content }}</pre>
        </section>
      </div>
    </div>
  </div>`,
};
