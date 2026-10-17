/*
  La VOIX du chat, par les modèles du proxy : ÉCOUTER une réponse, sans
  son Markdown ni ses blocs de code, lue par /ui/audio/speech avec la
  voix choisie (/ui/audio/voices).

  Les modèles : ceux du catalogue complet (/ui/models?all=1) de type
  text-to-speech ; le choix (modèle, voix) reste dans ce navigateur.
*/
import { call } from "../lib/http.js";

const KEY = "llm-proxy-chat-voice";
const MAX_SPEECH = 3000;        // caractères lus au plus

export function loadPrefs() {
  try { return JSON.parse(localStorage.getItem(KEY) || "{}") || {}; } catch { return {}; }
}
export function savePrefs(p) {
  try { localStorage.setItem(KEY, JSON.stringify(p)); } catch { /* sans stockage */ }
}

// Les modèles de synthèse du catalogue : {tts: [ids]}.
export async function voiceModels() {
  const r = await call("GET", "/ui/models?all=1");
  return { tts: (r.data || []).filter((m) => m.type === "text-to-speech").map((m) => m.id).sort() };
}

export async function voicesOf(model) {
  const r = await call("GET", "/ui/audio/voices?model=" + encodeURIComponent(model));
  return (r.data || []).map((v) => v.id);
}

// ── écouter ─────────────────────────────────────────────────────────────
// Le texte d'une réponse, tel qu'on le lit : sans code, liens ni balises.
export function speakable(markdown) {
  const t = String(markdown || "")
    .replace(/```[\s\S]*?(```|$)/g, " (code) ")
    .replace(/`([^`]*)`/g, "$1")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/^#{1,6}\s+/gm, "").replace(/^\s*[-*+]\s+/gm, "").replace(/[*_~>|]/g, "")
    .replace(/\s+/g, " ").trim();
  return t.length > MAX_SPEECH ? t.slice(0, MAX_SPEECH) + "…" : t;
}

export async function speech(text, model, voice, incognito = false) {
  const r = await fetch("/ui/audio/speech", {
    method: "POST",
    headers: { "content-type": "application/json", accept: "audio/*, application/json",
               ...(incognito ? { "x-incognito": "1" } : {}) },
    body: JSON.stringify({ model, input: text, ...(voice ? { voice } : {}), response_format: "wav" }),
  });
  if (!r.ok) {
    let j = null;
    try { j = await r.json(); } catch { /* corps illisible */ }
    throw new Error((j && j.error && j.error.message) || `HTTP ${r.status}`);
  }
  return URL.createObjectURL(await r.blob());
}
