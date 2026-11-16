// Plugin opencode (v2) : le proxy llm-proxy, comme llm-proxy.ts + mcp.json le
// font pour pi et omp.
//   * un provider `llmproxy` (API OpenAI, http://llmproxy/v1) dont les modèles
//     sont lus sur GET /v1/models du proxy au démarrage, puis toutes les
//     5 minutes — taille de contexte comprise (max_context_length) ;
//   * le serveur MCP du proxy (POST /mcp) : recherche web, lecture de pages et
//     de PDF, recherche dans une page, mémoire, OCR, transcription, exécution
//     de code, images.
// Adresse et clé lues dans l'environnement (LLM_PROXY_URL, LLM_PROXY_API_KEY ;
// LLM_PROXY_KEY accepté aussi) : rien en dur. Proxy injoignable : aucun modèle
// enregistré, la découverte n'attend pas plus de DELAI_DECOUVERTE.
// Posé dans ~/.config/opencode/plugins/ (chargé d'office par opencode 2.x).
import { Model, Plugin, Provider } from "@opencode/plugin"

const ENDPOINT = (process.env.LLM_PROXY_URL ?? "http://llmproxy").replace(/\/+$/, "")
const API_KEY = process.env.LLM_PROXY_API_KEY ?? process.env.LLM_PROXY_KEY ?? "unused"
const PROVIDER = "llmproxy"
const DEFAULT_CONTEXT = 128_000
const MAX_OUTPUT = 16_384
// Le proxy interroge tous ses backends pour /v1/models : un backend éteint
// lui coûte son délai méta (5 s) avant qu'il réponde.
const DELAI_DECOUVERTE = 15_000
const RAFRAICHISSEMENT = 5 * 60_000
// Types du catalogue des modèles de conversation (les autres : embeddings,
// reranking, voix, images…) ; un modèle d'OCR, « image vers texte », n'en
// est pas un.
const CHAT = new Set(["text-generation", "image-text-to-text"])
const HORS_CHAT = /ocr/i

interface ProxyModel {
  id: string
  type?: string
  max_context_length?: number | null
}

function num(value: unknown, fallback: number): number {
  const n = Number(value)
  return Number.isFinite(n) && n > 0 ? n : fallback
}

async function discover(): Promise<Model.Info[]> {
  const res = await fetch(`${ENDPOINT}/v1/models`, {
    headers: { Authorization: `Bearer ${API_KEY}` },
    signal: AbortSignal.timeout(DELAI_DECOUVERTE),
  })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  const data = ((await res.json()) as { data?: ProxyModel[] }).data ?? []
  return data
    .filter((m) => m && typeof m.id === "string" && (!m.type || CHAT.has(m.type)) && !HORS_CHAT.test(m.id))
    .sort((a, b) => a.id.localeCompare(b.id))
    .map((m) => {
      const context = num(m.max_context_length, DEFAULT_CONTEXT)
      const base = Model.Info.default(PROVIDER, m.id)
      return {
        ...base,
        name: m.id,
        capabilities: {
          tools: true,
          input: m.type === "image-text-to-text" ? ["text", "image"] : ["text"],
          output: ["text"],
        },
        limit: { context, output: Math.min(MAX_OUTPUT, context) },
      }
    })
}

export default Plugin.define({
  id: "llm-proxy",
  async setup(ctx) {
    let models: Model.Info[] = []
    try {
      models = await discover()
    } catch (err) {
      console.error(`[llm-proxy] découverte impossible (${ENDPOINT}/v1/models) : ${err}`)
    }

    // Relu à chaque ctx.provider.reload() : `models` est la liste du moment.
    await ctx.provider.transform((editor) => {
      if (models.length === 0) return
      editor.add({
        info: {
          ...Provider.Info.empty(PROVIDER),
          name: "llm-proxy",
          activation: "enabled",
          package: "@opencode/ai/providers/openai-compatible",
          settings: { baseURL: `${ENDPOINT}/v1`, apiKey: API_KEY },
          headers: { Authorization: `Bearer ${API_KEY}` },
        },
        models,
      })
    })

    // Délais en millisecondes : les outils longs du proxy (transcription,
    // génération d'image) vont jusqu'à 7 min.
    await ctx.mcp.transform((editor) => {
      editor.set("llmproxy", {
        type: "remote",
        url: `${ENDPOINT}/mcp`,
        headers: { Authorization: `Bearer ${API_KEY}` },
        oauth: false,
        timeout: { startup: 10_000, catalog: 30_000, execution: 600_000 },
      } as never)
    })

    // Les outils web intégrés d'opencode (`websearch`, `webfetch`) sont
    // retirés : à côté de ceux du proxy, le modèle les préférait, et ils
    // lisent les pages depuis le poste, entières — ceux du proxy bornent ce
    // qu'ils rendent (web_fetch, web_grep).
    await ctx.tool.transform((editor) => {
      for (const id of ["websearch", "webfetch"]) {
        try {
          editor.remove(id)
        } catch {
          // absent de cette version : rien à retirer
        }
      }
    })

    const timer = setInterval(() => {
      discover()
        .then(async (fresh) => {
          if (fresh.length === 0) return
          models = fresh
          await ctx.provider.reload()
        })
        .catch(() => {})
    }, RAFRAICHISSEMENT)
    return () => clearInterval(timer)
  },
})
