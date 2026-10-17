/*
  Le FORMULAIRE de Configuration → Proxy, sans Vue : son état neuf
  (newForm), son préremplissage depuis la configuration en vigueur
  (prefillForm, sur les `values` de GET /ui/config — ce que le fichier dit
  vraiment, pas une estimation), et le fichier PARTIEL qu'il produit
  (buildProxy) : le proxy l'applique sur la configuration en vigueur
  (POST /ui/config/merge), la page ne montre que le fichier complet.
  buildProxy rend aussi le .env des secrets référencés et les
  avertissements.
*/
import { q, list, envName, csv } from "../shared.js";

export const TOOL_LIST = [
  { id: "web_search", label: "Recherche web (SearXNG)" },
  { id: "web_fetch", label: "Lecture de pages" },
  { id: "web_grep", label: "Recherche dans une page" },
  { id: "code_execution", label: "Exécution de code" },
  { id: "image_generation", label: "Génération d'images", model: true },
  { id: "ocr", label: "OCR", model: true },
  { id: "transcribe", label: "Transcription", model: true },
  { id: "memory", label: "Mémoire persistante" },
];

// L'état d'un formulaire neuf (avant préremplissage).
export function newForm() {
  return {
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
  };
}

// ${VAR} : le nom de la variable ; toute autre valeur : "".
const VAR_REF = /^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$/;
export const varOf = (v) => (typeof v === "string" && (VAR_REF.exec(v) || [])[1]) || "";

export function prefillForm(px, c) {
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
}

export function buildProxy(px) {
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
}
