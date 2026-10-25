// @ts-check
/*
  Les MODÈLES DE TRANSCRIPTION dans la conversation
  (`automatic-speech-recognition` au catalogue), comme ceux de voix
  (speech.js) : choisis dans la liste des modèles, ils ÉCOUTENT l'audio du
  message — joint, ou enregistré au micro dans la barre de saisie — et la
  réponse est son texte. Le texte du message, s'il y en a, guide le
  modèle (`prompt` : noms propres, vocabulaire).

  Tout est préparé dans la page : l'audio décodé (ce que le navigateur
  sait lire : WAV, MP3, Ogg, WebM du micro, M4A…), mono 16 kHz, en WAV
  (certains modèles, Qwen3-ASR, ne lisent que lui). Un long audio part par
  PASSAGES d'une minute au plus, coupés dans le moment le plus calme de
  leurs dernières secondes (pas au milieu d'un mot), transcrits l'un après
  l'autre et mis bout à bout.
*/
import * as audio from "./audio.js";
import { transcribe, LANGUAGES } from "./proxy.js";

const RATE = 16000;
const PART = 60;             // secondes d'un passage, au plus
const LOOK = 10;             // secondes, à la fin d'un passage, où chercher où couper
const WIN = 0.05;            // secondes d'une fenêtre de mesure
export const MAX_SECONDS = 30 * 60;

/** Où couper `s` avant `end` : la fenêtre la plus calme des LOOK
 * dernières secondes. @param {Float32Array} s @param {number} from @param {number} end */
function cut(s, from, end) {
  const win = Math.round(RATE * WIN);
  let best = end, quiet = Infinity;
  for (let i = Math.max(from + win, end - LOOK * RATE); i + win <= end; i += win) {
    let sum = 0;
    for (let k = i; k < i + win; k++) sum += s[k] * s[k];
    if (sum < quiet) { quiet = sum; best = i + Math.round(win / 2); }
  }
  return best;
}

/** Les passages d'un audio, en WAV. @param {Float32Array} s @returns {Blob[]} */
export function parts(s) {
  const out = [];
  for (let at = 0; at < s.length;) {
    const end = s.length - at <= PART * RATE ? s.length : cut(s, at, at + PART * RATE);
    out.push(audio.wav(s.subarray(at, end), RATE));
    at = end;
  }
  return out;
}

/** Le code ISO 639-1 d'un nom de langue des réglages de la voix
 * (« French » → « fr ») ; «» : le modèle la devine. @param {string} [name] */
export const languageCode = (name) => (LANGUAGES.find((l) => l.name === name) || { code: "" }).code;

/** L'audio d'un message → son texte.
 * @param {{audio: Blob[], model: string, prompt?: string, language?: string,
 *   incognito?: boolean, signal?: AbortSignal, onPart?: (done: number, total: number) => void}} o
 * @returns {Promise<{text: string, duration: number}>} */
export async function transcribeTurn(o) {
  if (!o.audio.length) throw new Error("Joindre un audio, ou l'enregistrer (micro de la barre).");
  const samples = [];
  for (const blob of o.audio) samples.push(await audio.decode(blob, RATE));
  const duration = samples.reduce((n, s) => n + s.length, 0) / RATE;
  if (duration > MAX_SECONDS) throw new Error(`Audio de plus de ${MAX_SECONDS / 60} minutes.`);
  const files = samples.map((s) => parts(s));
  const total = files.reduce((n, f) => n + f.length, 0);
  const body = { model: o.model, ...(o.language ? { language: o.language } : {}),
                 ...(o.prompt && o.prompt.trim() ? { prompt: o.prompt.trim() } : {}) };
  // Un paragraphe par audio joint.
  const texts = [];
  let done = 0;
  for (const f of files) {
    const got = [];
    for (const wav of f) {
      if (o.onPart) o.onPart(done++, total);
      got.push(await transcribe(wav, body, { incognito: o.incognito, signal: o.signal }));
    }
    texts.push(got.filter(Boolean).join(" "));
  }
  return { text: texts.filter(Boolean).join("\n\n"), duration };
}
