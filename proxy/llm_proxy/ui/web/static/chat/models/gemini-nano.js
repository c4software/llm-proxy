// @ts-check
/*
  GEMINI NANO, le modèle que Chrome embarque (l'API Prompt,
  `LanguageModel`) : sa description pour la liste (browser.js) et sa
  façon de répondre, sur l'appareil.

  Un seul outil, la QUESTION À CHOIX (ask_user) : les outils natifs de
  l'API Prompt ne répondent pas (Chrome 155), il répond donc en JSON
  imposé (responseConstraint) — un texte, et, s'il lui manque une
  précision, des questions au format d'ask_user, que la page pose comme
  pour tout autre modèle. Son premier usage télécharge le modèle
  (plusieurs Go), que Chrome garde ensuite pour toutes les pages ; le
  charger prend une vingtaine de secondes à la première réponse.
*/
import { partialString as partial } from "../../lib/partialjson.js";
import { prompt } from "../../lib/prompts.js";

const OPTIONS = {
  expectedInputs: [{ type: "text", languages: ["fr", "en"] }, { type: "image" }],
  expectedOutputs: [{ type: "text", languages: ["fr"] }],
};

const api = () => (typeof self !== "undefined" && /** @type {any} */ (self).LanguageModel) || null;

async function blob(url) {
  return (await fetch(url)).blob();
}

const SCHEMA = { type: "object", required: ["action", "text"], properties: {
  action: { enum: ["answer", "ask"] },
  text: { type: "string" },
  questions: { type: "array", maxItems: 3, items: {
    type: "object", required: ["question", "header", "options"], properties: {
      question: { type: "string" },
      header: { type: "string" },
      options: { type: "array", minItems: 2, maxItems: 5, items: {
        type: "object", required: ["label"], properties: {
          label: { type: "string" }, description: { type: "string" } } } },
    } } },
} };

// Le protocole de réponse (JSON, answer ou ask), ses bornes lues dans
// SCHEMA.
const QUESTIONS = SCHEMA.properties.questions;
const CHOICES = QUESTIONS.items.properties.options;
const protocol = () => prompt("gemini_nano.protocol", { max_questions: QUESTIONS.maxItems,
  min_options: CHOICES.minItems, max_options: CHOICES.maxItems });

// Le contenu d'un message de l'API → celui de l'API Prompt.
async function content(c) {
  if (!Array.isArray(c)) return String(c || "");
  const out = [];
  for (const part of c) {
    if (part.type === "text") out.push({ type: "text", value: part.text });
    else if (part.type === "image_url" && part.image_url) {
      out.push({ type: "image", value: await blob(part.image_url.url) });
    }
  }
  return out;
}

const textOf = (c) => (Array.isArray(c) ? c.map((p) => p.text || "").join("") : String(c || ""));

// Les messages de l'API → ceux de l'API Prompt : une question posée
// (appel d'ask_user) reste dans le texte de la réponse, la réponse de
// l'utilisateur (résultat de l'outil) devient son message. Les appels
// d'autres outils (d'un autre modèle) ne sont pas repris ; deux messages
// de suite du même rôle sont joints.
function plain(messages) {
  const out = [];
  const push = (role, c) => {
    const prev = out[out.length - 1];
    if (prev && prev.role === role && typeof prev.content === "string" && typeof c === "string") {
      prev.content += "\n\n" + c;
    } else out.push({ role, content: c });
  };
  for (const m of messages) {
    if (m.role === "system" || m.role === "user") push(m.role, m.content);
    else if (m.role === "assistant") {
      let t = textOf(m.content);
      for (const c of m.tool_calls || []) {
        if (c.function && c.function.name === "ask_user") {
          let qs = [];
          try { qs = JSON.parse(c.function.arguments).questions || []; } catch { /* illisible */ }
          t += (t ? "\n\n" : "") + qs.map((q) => prompt("gemini_nano.question", { question: q.question })).join("\n");
        }
      }
      if (t.trim()) push("assistant", t);
    } else if (m.role === "tool" && (m.content || "").trim()) {
      push("user", prompt("gemini_nano.answers", { answers: m.content }));
    }
  }
  return out;
}

async function nanoAnswer(messages, live, signal, onStatus = (_text) => {}) {
  const LM = api();
  if (!LM) throw new Error("Ce navigateur n'a pas de modèle intégré.");
  // Le dernier message de l'utilisateur est la demande ; ce qui précède,
  // le contexte de la session.
  const usable = plain(messages);
  const last = usable.pop();
  if (!last || last.role !== "user") throw new Error("Rien à demander.");
  const initialPrompts = [];
  if (usable[0] && usable[0].role === "system") {
    initialPrompts.push({ role: "system", content: textOf(usable.shift().content) + "\n\n" + protocol() });
  } else initialPrompts.push({ role: "system", content: protocol() });
  for (const m of usable) {
    initialPrompts.push({ role: m.role, content: m.role === "user" ? await content(m.content) : m.content });
  }
  onStatus("Chargement de Gemini Nano…");
  const session = await LM.create({
    ...OPTIONS, initialPrompts, signal,
    monitor(m) {
      m.addEventListener("downloadprogress", (e) => {
        onStatus(e.loaded < 1 ? `Téléchargement de Gemini Nano : ${Math.round(e.loaded * 100)} %` : "");
      });
    },
  });
  onStatus("");
  const base = live.content;
  let raw = "";
  try {
    const input = await content(last.content);
    const stream = session.promptStreaming(
      Array.isArray(input) ? [{ role: "user", content: input }] : input,
      { signal, responseConstraint: SCHEMA });
    for await (const chunk of stream) {
      raw += chunk;
      live.content = base + partial(raw, "text");
    }
  } finally {
    session.destroy();
  }
  let got = null;
  try { got = JSON.parse(raw); } catch { return null; }
  live.content = base + String(got.text || "");
  const questions = Array.isArray(got.questions) ? got.questions : [];
  return got.action === "ask" && questions.length ? { questions } : null;
}

export const GEMINI_NANO = {
  id: "chrome/gemini-nano", name: "Gemini Nano", group: "Dans le navigateur",
  vision: true, byDefault: false,
  caps: { hostedTools: false, pageTools: ["ask_user"], reasoning: false, folder: false },
  available: async () => (api() ? api().availability(OPTIONS) : "unavailable"),
  answer: nanoAnswer,
};
