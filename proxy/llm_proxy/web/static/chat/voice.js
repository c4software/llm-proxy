// @ts-check
/*
  La VOIX du chat, par le NAVIGATEUR (speechSynthesis) : ÉCOUTER une
  réponse, sans son Markdown ni ses blocs de code — tout de suite, sans
  passer par un modèle. Sert aussi l'outil `speak` (tools/device/speak.js).

  Le texte est lu PHRASE par phrase (une file d'énonciations) : une
  longue énonciation est coupée au bout de quelques secondes par
  certaines voix (celles de Google, sous Chrome). Une lecture à la fois :
  en lancer une autre arrête la précédente.

  Le choix (voix, débit) reste dans ce navigateur.
*/
const KEY = "llm-proxy-chat-voice";
const MAX_SPEECH = 20_000;      // caractères lus au plus
const CHUNK = 220;              // caractères d'une énonciation, au plus

const synth = typeof window !== "undefined" ? window.speechSynthesis : undefined;
export const supported = !!synth;

// La lecture en cours : son identifiant (un message, une carte), ou «».
export const state = Vue.reactive({ id: "" });
// Les voix du navigateur : {uri, name, lang, local}, celles de la langue
// de la page d'abord. Elles arrivent parfois après le chargement.
export const voices = Vue.ref([]);

function loadVoices() {
  if (!synth) return;
  const lang = (navigator.language || "fr").slice(0, 2);
  voices.value = synth.getVoices()
    .map((v) => ({ uri: v.voiceURI, name: v.name, lang: v.lang, local: v.localService }))
    .sort((a, b) => Number(b.lang.startsWith(lang)) - Number(a.lang.startsWith(lang))
      || Number(b.local) - Number(a.local) || a.name.localeCompare(b.name));
}
if (synth) {
  loadVoices();
  synth.addEventListener("voiceschanged", loadVoices);
}

/** @returns {{voice: string, rate: number}} */
export function loadPrefs() {
  try {
    const p = JSON.parse(localStorage.getItem(KEY) || "{}") || {};
    return { voice: typeof p.voice === "string" ? p.voice : "", rate: Number(p.rate) || 1 };
  } catch { return { voice: "", rate: 1 }; }
}
/** @param {{voice: string, rate: number}} p */
export function savePrefs(p) {
  try { localStorage.setItem(KEY, JSON.stringify(p)); } catch { /* sans stockage */ }
}

/** Le texte d'une réponse, tel qu'on le lit : sans code, liens ni balises.
 * @param {string} markdown */
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

/** Le texte en énonciations : par phrases, regroupées jusqu'à CHUNK.
 * @param {string} text */
function chunks(text) {
  const out = [];
  let cur = "";
  for (const s of text.match(/[^.!?;:…]+[.!?;:…]*\s*/g) || [text]) {
    // Une « phrase » trop longue : coupée aux virgules, puis aux espaces.
    const parts = s.length <= CHUNK ? [s] : s.match(new RegExp(`.{1,${CHUNK}}(\\s|$)`, "g")) || [s];
    for (const p of parts) {
      if (cur && (cur + p).length > CHUNK) { out.push(cur.trim()); cur = ""; }
      cur += p;
    }
  }
  if (cur.trim()) out.push(cur.trim());
  return out;
}

/** La voix choisie (son URI), sinon celle de la langue `lang`, sinon celle
 * du navigateur. @param {string} uri @param {string} [lang] */
function pickVoice(uri, lang) {
  const all = synth ? synth.getVoices() : [];
  const chosen = all.find((v) => v.voiceURI === uri);
  if (chosen && (!lang || chosen.lang.slice(0, 2) === lang.slice(0, 2))) return chosen;
  if (lang) return all.find((v) => v.lang === lang) || all.find((v) => v.lang.slice(0, 2) === lang.slice(0, 2)) || null;
  return null;
}

/** Lit `text` ; `id` désigne cette lecture (state.id), arrêtée par stop()
 * ou par une autre lecture.
 * @param {string} id @param {string} text
 * @param {{voice?: string, rate?: number, lang?: string}} [opts] */
export function speak(id, text, opts = {}) {
  if (!synth) throw new Error("ce navigateur n'a pas de synthèse vocale");
  const voice = pickVoice(opts.voice || "", opts.lang);
  // Toutes les énonciations prêtes AVANT de toucher à la lecture en cours :
  // une erreur ici laisse l'état tel qu'il était.
  const said = chunks(text).map((part) => {
    const u = new SpeechSynthesisUtterance(part);
    if (voice) { u.voice = voice; u.lang = voice.lang; } else if (opts.lang) u.lang = opts.lang;
    u.rate = opts.rate || 1;
    u.onerror = () => { if (state.id === id) state.id = ""; };
    return u;
  });
  if (!said.length) return;
  // La dernière énonciation finie : la lecture est finie.
  said[said.length - 1].onend = () => { if (state.id === id) state.id = ""; };
  synth.cancel();
  state.id = id;
  for (const u of said) synth.speak(u);
}

export function stop() {
  state.id = "";
  if (synth) synth.cancel();
}
