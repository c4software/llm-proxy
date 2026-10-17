/*
  Les modèles d'IMAGES du chat : le prompt avec le contexte de la
  conversation, et une requête de génération (/ui/images/generations) ou
  de retouche (/ui/images/edits), rendue en pièce jointe.
*/
// Le type d'une image rendue en base64, lu dans ses premiers octets.
export function b64Type(b64) {
  return b64.startsWith("/9j/") ? "image/jpeg" : b64.startsWith("R0lG") ? "image/gif"
    : b64.startsWith("UklGR") ? "image/webp" : "image/png";
}

// Le prompt d'une image, avec le CONTEXTE de la conversation : les
// demandes précédentes de la même suite d'images (chaque message
// auquel un modèle d'images a répondu, en remontant jusqu'à une
// réponse de conversation), puis la dernière. « Plus sombre » suit
// donc « un phare au coucher du soleil ». Une retouche n'en a pas
// besoin : l'image porte le contexte.
export const MAX_PROMPT = 4000;
export function imagePrompt(conv, edit) {
  const msgs = conv.messages;
  const parts = [];
  for (let i = msgs.length - 1; i >= 0; i--) {
    const m = msgs[i];
    if (m.role === "assistant" && !m.image) break;
    if (m.role === "user" && m.content.trim()) parts.unshift(m.content.trim());
    if (edit) break;
  }
  const text = parts.join("\n");
  return text.length > MAX_PROMPT ? text.slice(-MAX_PROMPT) : text;
}

// Une requête d'image : génération, ou retouche d'`image` (data URL).
export async function oneImage(conv, body, signal, onUnauthorized) {
  const r = await fetch(body.image ? "/ui/images/edits" : "/ui/images/generations", {
    method: "POST", signal,
    headers: { "content-type": "application/json", accept: "application/json",
               ...(conv.incognito ? { "x-incognito": "1" } : {}) },
    body: JSON.stringify(body),
  });
  if (r.status === 401) { onUnauthorized(); throw new Error("Session expirée : reconnectez-vous."); }
  let j = null;
  try { j = await r.json(); } catch { /* corps illisible */ }
  if (!r.ok) throw new Error((j && j.error && (j.error.message || j.error)) || `HTTP ${r.status}`);
  const item = ((j && j.data) || [])[0] || {};
  const tag = Date.now().toString(36);
  if (item.b64_json) {
    const type = b64Type(item.b64_json);
    return { kind: "image", type, size: Math.round(item.b64_json.length * 3 / 4),
             name: `image-${tag}.${type.split("/")[1].replace("jpeg", "jpg")}`,
             data: `data:${type};base64,${item.b64_json}`, revised: item.revised_prompt || "" };
  }
  if (/^https?:\/\//.test(item.url || "")) {
    return { kind: "image", type: "", size: 0, name: `image-${tag}`, data: item.url,
             revised: item.revised_prompt || "" };
  }
  throw new Error("Le modèle n'a pas rendu d'image lisible.");
}
