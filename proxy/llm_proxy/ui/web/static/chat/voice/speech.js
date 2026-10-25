// @ts-check
/*
  Les MODÈLES DE VOIX dans la conversation, comme ceux d'images
  (images.js) : choisis dans la liste des modèles, ils LISENT le message
  envoyé ; la réponse est l'audio, joint au message (kind « audio »,
  gardé avec la conversation comme une image, /ui/assets).

  Selon le rôle du modèle (proxy.js) :
    custom    une voix toute faite (`voice`), le prompt système comme ton
    clone     une voix clonée de ce navigateur (clones.js)
    design    le prompt système DÉCRIT la voix ; une seule requête (le
              modèle inventerait une voix nouvelle à chaque passage)

  Un long texte part par passages (text.js), synthétisés l'un après
  l'autre puis mis bout à bout en un seul WAV (audio.js).
*/
import * as audio from "./audio.js";
import * as clones from "./clones.js";
import { synthesize, language } from "./proxy.js";
import { sentences } from "./text.js";

const PART = 600;            // caractères d'un passage
export const MAX_TEXT = 6000;

export const ROLE_LABELS = { custom: "voix toutes faites", clone: "voix clonée", design: "voix décrite" };

/** Ce qui manque pour lire, ou «».
 * @param {{role: string, voice?: string, clone?: string, instructions?: string}} o */
export function missing(o) {
  if (o.role === "custom" && !o.voice) return "Choisir une voix.";
  if (o.role === "clone" && !o.clone) return "Choisir une voix clonée (ou en créer une : Réglages → Voix).";
  if (o.role === "design" && !(o.instructions || "").trim()) {
    return "Décrire la voix dans le prompt système (Réglages) : genre, âge, timbre, accent, style.";
  }
  return "";
}

/** Un message lu → l'audio joint à la réponse.
 * @param {{text: string, model: string, role: string, voice?: string, clone?: string,
 *   instructions?: string, language?: string, incognito?: boolean, signal?: AbortSignal,
 *   onPart?: (done: number, total: number) => void}} o
 * @returns {Promise<{kind: "audio", type: string, name: string, size: number, data: string, duration: number}>} */
export async function speakTurn(o) {
  const lack = missing(o);
  if (lack) throw new Error(lack);
  const text = o.text.trim().slice(0, MAX_TEXT);
  const lang = o.language || language();
  const base = { model: o.model, ...(lang ? { language: lang } : {}) };
  if (o.role === "custom") {
    Object.assign(base, { voice: o.voice, ...(o.instructions && o.instructions.trim() ? { instructions: o.instructions.trim() } : {}) });
  } else if (o.role === "clone") {
    const ref = await clones.reference(/** @type {string} */ (o.clone));
    if (!ref) throw new Error("Cette voix clonée n'est pas dans ce navigateur.");
    Object.assign(base, { reference_audio: ref.audio, reference_text: ref.text });
  } else {
    Object.assign(base, { instructions: /** @type {string} */ (o.instructions).trim() });
  }
  const parts = o.role === "design" ? [text] : sentences(text, PART);
  const samples = [];
  for (let i = 0; i < parts.length; i++) {
    if (o.onPart) o.onPart(i, parts.length);
    const blob = await synthesize({ ...base, input: parts[i] }, { incognito: o.incognito, signal: o.signal });
    samples.push(await audio.decode(blob));
  }
  const all = new Float32Array(samples.reduce((n, s) => n + s.length, 0));
  let at = 0;
  for (const s of samples) { all.set(s, at); at += s.length; }
  const wav = audio.wav(all);
  return { kind: "audio", type: "audio/wav", name: `voix-${Date.now().toString(36)}.wav`, size: wav.size,
           data: "data:audio/wav;base64," + await audio.base64(wav), duration: all.length / audio.RATE };
}
