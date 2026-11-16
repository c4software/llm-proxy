import type { ExtensionAPI, ProviderModelConfig } from "@earendil-works/pi-coding-agent";

// Adresse et clé du proxy lues dans l'environnement (LLM_PROXY_URL,
// LLM_PROXY_API_KEY ; LLM_PROXY_KEY accepté aussi, comme dans gufo-media.ts et
// le mcp.json voisin) : rien en dur dans le dépôt. Défauts : le proxy du
// réseau local, ouvert.
const ENDPOINT = (process.env.LLM_PROXY_URL ?? "http://llmproxy").replace(/\/+$/, "");
const API_KEY = process.env.LLM_PROXY_API_KEY ?? process.env.LLM_PROXY_KEY ?? "unused";

// Albert n'expose pas de cap de génération : on plafonne nous-mêmes.
const DEFAULT_MAX_TOKENS = 16384;
const DEFAULT_CONTEXT = 128000;

interface AlbertModel {
  id: string;
  type?: string;
  aliases?: string[];
  max_context_length?: number | null;
  owned_by?: string;
  costs?: { prompt_tokens?: number; completion_tokens?: number };
}

interface ModelInfo {
  id: string;
  contextWindow: number;
  maxTokens: number;
  costs: { input: number; output: number };
}

/** Number(undefined) donne NaN, pas undefined : ?? ne rattrape rien. */
function num(value: unknown, fallback: number): number {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? n : fallback;
}

const THINKING: Partial<ProviderModelConfig> = {
  reasoning: true,
  thinkingLevelMap: { off: "off", minimal: null, low: "low", medium: "medium", high: "high", xhigh: "xhigh", max: null },
  compat: {
    thinkingFormat: "chat-template",
    chatTemplateKwargs: {
      enable_thinking: { $var: "thinking.enabled" },
      reasoning_effort: { $var: "thinking.effort", omitWhenOff: true },
    },
  },
};

export default async function (pi: ExtensionAPI) {
  let models: ModelInfo[] = [];

  try {
    const res = await fetch(`${ENDPOINT}/v1/models`, {
      headers: { Authorization: `Bearer ${API_KEY}` },
      signal: AbortSignal.timeout(10000),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const payload = (await res.json()) as { data: AlbertModel[] };

    models = payload.data
      // seuls les text-generation acceptent /v1/chat/completions
      //.filter((m) => m.type === "text-generation")
      .map((m) => {
        const contextWindow = num(m.max_context_length, DEFAULT_CONTEXT);
        return {
          id: m.id,
          contextWindow,
          maxTokens: Math.min(DEFAULT_MAX_TOKENS, contextWindow),
          costs: {
            input: num(m.costs?.prompt_tokens, 0),
            output: num(m.costs?.completion_tokens, 0),
          },
        };
      })
      .sort((a, b) => a.id.localeCompare(b.id));
  } catch (err) {
    // Albert injoignable ou clé expirée : on n'enregistre rien plutôt que
    // de bloquer le démarrage
    console.error(`[albert] découverte impossible : ${err}`);
    return;
  }

  if (models.length === 0) {
    console.error("[albert] aucun modèle text-generation exposé");
    return;
  }

  pi.registerProvider("albert", {
    name: "Albert API (DINUM)",
    baseUrl: `${ENDPOINT}/v1`,
    apiKey: API_KEY,
    api: "openai-completions",
    models: models.map((m) => ({
      id: m.id,
      name: m.id,
      input: ["text"],
      // Albert facture en unités de budget par million de tokens.
      cost: {
        input: m.costs.input,
        output: m.costs.output,
        cacheRead: 0,
        cacheWrite: 0,
      },
      contextWindow: m.contextWindow,
      maxTokens: m.maxTokens,
      ...THINKING,
    })),
  });
}
