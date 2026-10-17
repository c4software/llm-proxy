/*
  Un modèle DU NAVIGATEUR : Gemini Nano, que Chrome embarque (l'API
  Prompt, `LanguageModel`). Proposé dans la liste quand le navigateur l'a
  (ou peut le télécharger) ; il répond sur l'appareil, sans passer par le
  proxy — donc sans les outils du proxy ni ceux de la page.

    localModels()                  [] ou [{id, local, vision}]
    isLocal(id)
    runLocal(messages, live, signal, onStatus)
                                   les messages de l'API (images en data
                                   URL) → la réponse, écrite à mesure
                                   dans live.content

  Le premier usage télécharge le modèle (plusieurs Go) : Chrome le
  garde ensuite pour toutes les pages.
*/
export const LOCAL_ID = "chrome/gemini-nano";
export const isLocal = (id) => id === LOCAL_ID;

const OPTIONS = {
  expectedInputs: [{ type: "text", languages: ["fr", "en"] }, { type: "image" }],
  expectedOutputs: [{ type: "text", languages: ["fr"] }],
};

const api = () => (typeof self !== "undefined" && self.LanguageModel) || null;

export async function localModels() {
  const LM = api();
  if (!LM) return [];
  try {
    const got = await LM.availability(OPTIONS);
    if (got === "unavailable") return [];
    return [{ id: LOCAL_ID, local: true, vision: true, availability: got }];
  } catch {
    return [];
  }
}

async function blob(url) {
  return (await fetch(url)).blob();
}

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

export async function runLocal(messages, live, signal, onStatus = () => {}) {
  const LM = api();
  if (!LM) throw new Error("Ce navigateur n'a pas de modèle intégré.");
  // Le dernier message de l'utilisateur est la question ; ce qui précède,
  // le contexte de la session. Les appels d'outils d'un autre modèle ne
  // sont pas repris (le texte de ses réponses, si).
  const usable = messages.filter((m) => ["system", "user", "assistant"].includes(m.role)
    && (Array.isArray(m.content) ? m.content.length : m.content));
  const last = usable.pop();
  if (!last || last.role !== "user") throw new Error("Rien à demander.");
  const initialPrompts = [];
  for (const m of usable) {
    initialPrompts.push({ role: m.role, content: m.role === "user" ? await content(m.content)
      : (Array.isArray(m.content) ? m.content.map((p) => p.text || "").join("") : m.content) });
  }
  const session = await LM.create({
    ...OPTIONS, initialPrompts, signal,
    monitor(m) {
      m.addEventListener("downloadprogress", (e) => {
        onStatus(e.loaded < 1 ? `Téléchargement de Gemini Nano : ${Math.round(e.loaded * 100)} %` : "");
      });
    },
  });
  onStatus("");
  try {
    const input = await content(last.content);
    const stream = session.promptStreaming(
      Array.isArray(input) ? [{ role: "user", content: input }] : input, { signal });
    for await (const chunk of stream) live.content += chunk;
  } finally {
    session.destroy();
  }
}
