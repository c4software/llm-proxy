// @ts-check
/*
  Le moteur NAVIGATEUR de la voix (speechSynthesis) : tout de suite, sans
  passer par un modèle.

  Le texte est lu PHRASE par phrase (une file d'énonciations) : une
  longue énonciation est coupée au bout de quelques secondes par
  certaines voix (celles de Google, sous Chrome).
*/
import { state } from "./state.js";
import { sentences } from "./text.js";

const CHUNK = 220;              // caractères d'une énonciation, au plus

const synth = typeof window !== "undefined" ? window.speechSynthesis : undefined;
export const supported = !!synth;

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

/** La voix choisie (son URI), sinon celle de la langue `lang`, sinon celle
 * du navigateur. @param {string} uri @param {string} [lang] */
function pickVoice(uri, lang) {
  const all = synth ? synth.getVoices() : [];
  const chosen = all.find((v) => v.voiceURI === uri);
  if (chosen && (!lang || chosen.lang.slice(0, 2) === lang.slice(0, 2))) return chosen;
  if (lang) return all.find((v) => v.lang === lang) || all.find((v) => v.lang.slice(0, 2) === lang.slice(0, 2)) || null;
  return null;
}

/** Lit `text` sous l'identifiant `id` (state.id).
 * @param {string} id @param {string} text
 * @param {{voice?: string, rate?: number, lang?: string}} [opts] */
export function speak(id, text, opts = {}) {
  if (!synth) throw new Error("ce navigateur n'a pas de synthèse vocale");
  const voice = pickVoice(opts.voice || "", opts.lang);
  // Toutes les énonciations prêtes AVANT de toucher à la lecture en cours :
  // une erreur ici laisse l'état tel qu'il était.
  const said = sentences(text, CHUNK).map((part) => {
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
  if (synth) synth.cancel();
}

/** Le nom de la voix choisie. @param {string} uri */
export function voiceName(uri) {
  const v = voices.value.find((x) => x.uri === uri);
  return v ? v.name : "voix du navigateur";
}
