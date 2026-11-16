/*
  Les fournisseurs connus : la page d'installation (SetupPanel.js) et
  Configuration → Proxy, section Backends. Un choix remplit le nom
  (préfixe des modèles), l'URL, la variable de la clé et le protocole
  (`api`, OpenAI par défaut) ;
  `model` : un modèle de départ, pour le modèle par défaut de l'installation.
*/
export const PROVIDERS = [
  { id: "nvidia", label: "NVIDIA", note: "gratuit, environ 40 requêtes par minute",
    url: "https://integrate.api.nvidia.com", keyVar: "NVIDIA_API_KEY",
    model: "nvidia/nvidia/nemotron-3.5-lightning-30b-a3b" },
  { id: "amd", label: "AMD Token Lab", note: "gratuit, 30 jours, 20 requêtes par minute",
    url: "https://api.tokenlab.amd.com", keyVar: "AMD_API_KEY",
    model: "amd/Gemma-4-31B-IT" },
  { id: "groq", label: "Groq", note: "gratuit, 30 requêtes par minute",
    url: "https://api.groq.com/openai", keyVar: "GROQ_API_KEY",
    model: "groq/openai/gpt-oss-120b" },
  { id: "mistral", label: "Mistral", note: "plan Experiment gratuit",
    url: "https://api.mistral.ai", keyVar: "MISTRAL_API_KEY",
    model: "mistral/mistral-small-latest" },
  { id: "openrouter", label: "OpenRouter", note: "modèles :free, 50 requêtes par jour",
    url: "https://openrouter.ai/api", keyVar: "OPENROUTER_API_KEY",
    model: "openrouter/nvidia/nemotron-3.5-lightning:free" },
  { id: "go", label: "OpenCode Go", note: "abonnement",
    url: "https://opencode.ai/inference/go/openai", keyVar: "OPENCODE_GO_API_KEY",
    model: "go/glm-5.3-flash" },
  { id: "claude", label: "Anthropic (Claude)", note: "payant, crédit d'API",
    url: "https://api.anthropic.com", keyVar: "ANTHROPIC_API_KEY", api: "anthropic",
    model: "claude/claude-opus-5-5", prices: () => CLAUDE_PRICES },
  // Son défaut tool_choice « none » casse les outils : « auto » injecté.
  { id: "albert", label: "Albert (API de l'État)", note: "quotas",
    url: "https://albert.api.etalab.gouv.fr", keyVar: "ALBERT_API_KEY",
    quotas: true, toolChoice: "auto" },
];

// Les tarifs publics d'Anthropic en novembre 2026, $ par million de tokens
// ([backends.<nom>.prices], upstream/pricing.py) ; cache_write : l'écriture
// à 5 minutes (le cache automatique du proxy). Le premier motif qui couvre
// un modèle gagne : les plus précis d'abord.
const CLAUDE_PRICES = {
  "claude-fable-5-2": { input: 8, output: 40, cache_read: 0.2, cache_write: 10 },
  "claude-mythos-5-2": { input: 8, output: 40, cache_read: 0.2, cache_write: 10 },
  "claude-fable-5-1": { input: 10, output: 50, cache_read: 0.25, cache_write: 12.5 },
  "claude-mythos-5-1": { input: 10, output: 50, cache_read: 0.25, cache_write: 12.5 },
  "claude-fable-5": { input: 10, output: 50, cache_read: 1, cache_write: 12.5 },
  "claude-mythos-5": { input: 10, output: 50, cache_read: 1, cache_write: 12.5 },
  "claude-opus-5-5": { input: 4, output: 20, cache_read: 0.2, cache_write: 5 },
  "claude-opus-*": { input: 5, output: 25, cache_read: 0.5, cache_write: 6.25 },
  "claude-sonnet-5-5": { input: 2, output: 10, cache_read: 0.1, cache_write: 2.5 },
  "claude-sonnet-5": { input: 2, output: 10, cache_read: 0.2, cache_write: 2.5 },
  "claude-sonnet-4-6": { input: 3, output: 15, cache_read: 0.3, cache_write: 3.75 },
  "claude-haiku-5-5": { input: 0.1, output: 0.5, cache_read: 0.01, cache_write: 0.125,
    long: { above: 100000, input: 0.5, output: 2.5, cache_read: 0.05, cache_write: 0.625 } },
  "claude-haiku-4-5*": { input: 1, output: 5, cache_read: 0.1, cache_write: 1.25 },
};

// Une valeur TOML en ligne : un tarif en décimal (4.0, 0.125), `above` en
// entier (un nombre de tokens), une table en ligne ({ input = 4.0, … }).
const tomlValue = (v, key = "") => (v && typeof v === "object"
  ? "{ " + Object.entries(v).map(([k, x]) => `${k} = ${tomlValue(x, k)}`).join(", ") + " }"
  : key === "above" ? String(Math.round(v))
  : Number.isInteger(Number(v)) ? Number(v).toFixed(1) : String(Number(v)));

/** Les lignes de [backends.<nom>.prices] (vide sans tarif). */
export function pricesToml(name, prices) {
  const rows = Object.entries(prices || {});
  if (!rows.length) return [];
  return [`[backends.${name}.prices]`,
          ...rows.map(([pattern, rate]) => `${JSON.stringify(pattern)} = ${tomlValue(rate)}`)];
}

// Une ligne du formulaire Backends, préremplie pour ce fournisseur.
export const providerBackend = (p) => ({
  name: p.id, url: p.url, key: true, keyVar: p.keyVar, quotas: !!p.quotas,
  api: p.api || "openai", images: p.api === "anthropic", prices: p.prices ? p.prices() : null,
  toolChoice: p.toolChoice || "", maxTokens: "", fairShare: true,
});

// Les variables que le docker-compose.yml du dépôt transmet déjà au
// conteneur ; toute autre ${VAR} du TOML doit l'être par l'override.
const BASE_ENV = new Set(["ALBERT_API_KEY", "EXECUTOR_TOKEN", "PROXY_API_KEY",
                          "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET"]);
export const IMAGE = "ghcr.io/c4software/llm-proxy:latest";

// Le docker-compose.override.yml du service albert-proxy : l'image publiée,
// et chaque ${VAR} du fichier complet (commentaires exclus) que le compose
// de base ne transmet pas.
export function buildOverride(toml) {
  const vars = new Set();
  for (const line of String(toml || "").split("\n")) {
    if (/^\s*#/.test(line)) continue;
    for (const m of line.matchAll(/\$\{([A-Za-z_][A-Za-z0-9_]*)\}/g)) vars.add(m[1]);
  }
  const extra = [...vars].filter((v) => !BASE_ENV.has(v)).sort();
  const L = ["# docker-compose.override.yml, à côté du docker-compose.yml",
             "services:", "  albert-proxy:",
             "    # L'image publiée à chaque version, plutôt que la construire sur place.",
             `    image: ${IMAGE}`];
  if (extra.length) {
    L.push("    # Les variables du .env que le TOML référence : sans elles ici, le",
           "    # conteneur ne les voit pas et la clé part vide.",
           "    environment:", ...extra.map((v) => `      ${v}: "\${${v}}"`));
  }
  return L.join("\n") + "\n";
}
