/*
  Le FLUX d'une réponse du chat (SSE de /ui/chat/completions) :
  readStream lit les événements `data:` un à un ; take les applique au
  message en cours — texte, raisonnement, appels d'outils hébergés
  (`x_hosted_tool`, propre au chat de la page), sources citées, usage —
  et les appels que la PAGE doit exécuter (`tool_calls` : les outils du
  dossier ouvert, chat/workspace.js), rassemblés morceau par morceau dans
  `hooks.calls` (index → {id, name, arguments}).
*/
export async function readStream(stream, live, hooks = {}) {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let nl;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl).replace(/\r$/, "");
      buf = buf.slice(nl + 1);
      if (!line.startsWith("data:")) continue;   // «: ping» et le reste
      const data = line.slice(5).trim();
      if (!data || data === "[DONE]") continue;
      let doc;
      try { doc = JSON.parse(data); } catch { continue; }
      take(doc, live);
    }
  }
}

export function take(doc, live, hooks = {}) {
  if (doc.error) {
    live.error = typeof doc.error === "string" ? doc.error
      : doc.error.message || "erreur du proxy";
    return;
  }
  if (doc.usage) live.usage = doc.usage;
  for (const choice of doc.choices || []) {
    const d = choice.delta || {};
    if (d.content) live.content += d.content;
    const thought = d.reasoning_content || d.reasoning;
    if (typeof thought === "string") live.reasoning += thought;
    // Un appel pour la page : ses morceaux arrivent au fil du flux.
    if (Array.isArray(d.tool_calls) && hooks.calls) {
      for (const tc of d.tool_calls) {
        const slot = hooks.calls[tc.index || 0] || (hooks.calls[tc.index || 0] =
          { id: "", name: "", arguments: "" });
        if (tc.id) slot.id = tc.id;
        if (tc.function && tc.function.name) slot.name += tc.function.name;
        if (tc.function && tc.function.arguments) slot.arguments += tc.function.arguments;
      }
    }
    if (d.x_hosted_tool) {
      const ev = d.x_hosted_tool;
      const known = live.tools.find((t) => t.id === ev.id);
      if (known) Object.assign(known, ev);
      else live.tools.push({ ...ev });
      if (ev.name === "memory" && ev.status !== "running" && hooks.onMemory) hooks.onMemory();
    }
    for (const a of d.annotations || []) {
      const c = a.url_citation;
      if (c && !live.sources.some((s) => s.url === c.url)) {
        live.sources.push({ url: c.url, title: c.title || c.url });
      }
    }
  }
}
