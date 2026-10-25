// @ts-check
/*
  Le moteur PROXY de la voix : la synthèse d'un modèle du catalogue
  (POST /ui/audio/speech, compté à la clé connectée ; docs/voix.md), et
  la transcription (POST /ui/audio/transcriptions, transcribe.js).
  Les modèles de synthèse (`text-to-speech` au catalogue), rangés par
  rôle d'après leur nom (Qwen3-TTS) :

    custom    voix toutes faites (GET /ui/audio/voices), ton réglable
    clone     lit avec la voix d'un enregistrement (clones.js)
    design    invente une voix d'après une description : sert à FABRIQUER
              une référence (VoiceStudio.js), pas à lire — il en
              inventerait une nouvelle à chaque passage

  La lecture se fait par passages (text.js) : le premier court, pour
  commencer vite ; le suivant se synthétise pendant que l'autre est joué
  (la génération va 2 à 3 fois plus vite que la lecture). Un backend qui
  bascule de modèle répond `backend_offline` le temps du chargement :
  réessayé quelques fois.
*/
import { catalogState } from "../../lib/catalog.js";
import { state } from "./state.js";
import { sentences } from "./text.js";
import * as clones from "./clones.js";
import { base64 as audioBase64 } from "./audio.js";

const RETRIES = 5;
const RETRY_MS = 3000;
const FIRST = 160, NEXT = 600;     // caractères d'un passage

// Les langues des modèles Qwen3-TTS, par code BCP 47.
const LANGS = { zh: "Chinese", en: "English", ja: "Japanese", ko: "Korean", de: "German",
                fr: "French", ru: "Russian", pt: "Portuguese", es: "Spanish", it: "Italian" };
export const LANGUAGES = Object.entries(LANGS).map(([code, name]) => ({ code, name }));
/** Le nom de langue du modèle pour un code BCP 47 ; «» : il devinera.
 * @param {string} [code] */
export function language(code) {
  return LANGS[String(code || navigator.language || "").slice(0, 2).toLowerCase()] || "";
}

/** Le rôle d'un modèle de synthèse, d'après son nom : « custom »,
 * « clone » ou « design ». @param {string} id */
export function role(id) {
  const n = id.toLowerCase();
  if (/design/.test(n)) return "design";
  if (/(^|[-_/.])base($|[-_.])|clone/.test(n)) return "clone";
  return "custom";
}

/** Les modèles de synthèse du catalogue, par rôle. */
export const models = Vue.computed(() => {
  const out = { custom: [], clone: [], design: [] };
  for (const m of catalogState.data) if (m.type === "text-to-speech") out[role(m.id)].push(m.id);
  return out;
});
export const available = Vue.computed(() => models.value.custom.length + models.value.clone.length > 0);

// Les voix toutes faites, par modèle (lues une fois).
const presets = Vue.reactive({});
/** @param {string} model @returns {Promise<string[]>} */
export async function presetVoices(model) {
  if (!presets[model]) {
    const r = await withRetry(() => fetch("/ui/audio/voices?model=" + encodeURIComponent(model),
                                          { headers: { accept: "application/json" } }));
    const json = await r.json();
    presets[model] = (json.data || []).map((v) => v.id || v.name).filter(Boolean);
  }
  return presets[model];
}

/** Une réponse lue comme une erreur : le message du proxy (ou du backend).
 * @param {Response} r */
async function failure(r) {
  let json = null;
  try { json = await r.json(); } catch { /* corps vide */ }
  const e = json && json.error;
  const err = new Error((e && (e.message || e)) || (json && json.detail) || `HTTP ${r.status}`);
  /** @type {any} */ (err).type = e && e.type;
  return err;
}

/** @param {() => Promise<Response>} send @param {AbortSignal} [signal] */
async function withRetry(send, signal) {
  for (let n = 0; ; n++) {
    const r = await send();
    if (r.ok) return r;
    const err = await failure(r);
    // Le backend charge un autre modèle : il revient dans quelques secondes.
    if (r.status !== 503 || /** @type {any} */ (err).type !== "backend_offline" || n >= RETRIES) throw err;
    await new Promise((res, rej) => {
      const t = setTimeout(res, RETRY_MS);
      if (signal) signal.addEventListener("abort", () => { clearTimeout(t); rej(new DOMException("arrêté", "AbortError")); });
    });
  }
}

/** Une synthèse : le corps de /v1/audio/speech → l'audio (WAV).
 * @param {object} body @param {{incognito?: boolean, signal?: AbortSignal}} [opts]
 * @returns {Promise<Blob>} */
export async function synthesize(body, opts = {}) {
  const r = await withRetry(() => fetch("/ui/audio/speech", {
    method: "POST", signal: opts.signal,
    headers: { "content-type": "application/json", accept: "audio/*",
               ...(opts.incognito ? { "x-incognito": "1" } : {}) },
    body: JSON.stringify({ response_format: "wav", ...body }),
  }), opts.signal);
  return r.blob();
}

/** Une transcription : un audio (WAV) → son texte.
 * @param {Blob} wav @param {{model: string, language?: string, prompt?: string}} body
 * @param {{incognito?: boolean, signal?: AbortSignal}} [opts] @returns {Promise<string>} */
export async function transcribe(wav, body, opts = {}) {
  const file = "data:audio/wav;base64," + await audioBase64(wav);
  const r = await withRetry(() => fetch("/ui/audio/transcriptions", {
    method: "POST", signal: opts.signal,
    headers: { "content-type": "application/json", accept: "application/json",
               ...(opts.incognito ? { "x-incognito": "1" } : {}) },
    body: JSON.stringify({ ...body, file }),
  }), opts.signal);
  const json = await r.json();
  return String((json && json.text) || "").trim();
}

/** @typedef {{kind: "preset", model: string, name: string} | {kind: "clone", model: string, id: string}} Voice */

/** Le corps d'une requête pour `input`, selon la voix.
 * @param {Voice} voice @param {string} input
 * @param {{tone?: string, language?: string}} opts */
async function bodyFor(voice, input, opts) {
  const lang = opts.language ? { language: opts.language } : {};
  if (voice.kind === "preset") {
    return { model: voice.model, voice: voice.name, input, ...lang,
             ...(opts.tone ? { instructions: opts.tone } : {}) };
  }
  const ref = await clones.reference(voice.id);
  if (!ref) throw new Error("voix clonée introuvable dans ce navigateur");
  return { model: voice.model, input, reference_audio: ref.audio, reference_text: ref.text, ...lang };
}

// La lecture en cours : son numéro, son arrêt, l'audio joué.
let seq = 0;
/** @type {AbortController | null} */
let controller = null;
/** @type {HTMLAudioElement | null} */
let audio = null;
/** @type {(() => void) | null} */
let ended = null;

/** Joue un audio jusqu'au bout (ou jusqu'à stop()).
 * @param {Blob} blob @param {number} rate */
function play(blob, rate) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(blob);
    const a = new Audio(url);
    a.playbackRate = rate || 1;
    a.preservesPitch = true;
    audio = a;
    const done = () => { URL.revokeObjectURL(url); if (audio === a) audio = null; ended = null; resolve(undefined); };
    ended = done;
    a.onended = done;
    a.onerror = () => { URL.revokeObjectURL(url); ended = null; reject(new Error("audio illisible")); };
    a.play().catch((e) => { URL.revokeObjectURL(url); ended = null; reject(e); });
  });
}

/** Lit `text` sous l'identifiant `id` (state.id), passage par passage.
 * @param {string} id @param {string} text
 * @param {{voice: Voice, tone?: string, language?: string, rate?: number, incognito?: boolean}} opts */
export async function speak(id, text, opts) {
  const parts = sentences(text, NEXT, FIRST);
  if (!parts.length) return;
  stop();
  const mine = ++seq;
  const ctl = controller = new AbortController();
  Object.assign(state, { id, loading: true, error: "", errorId: "" });
  const make = (part) => {
    const p = bodyFor(opts.voice, part, opts)
      .then((body) => synthesize(body, { incognito: opts.incognito, signal: ctl.signal }));
    p.catch(() => { /* lue à son tour */ });
    return p;
  };
  try {
    let next = make(parts[0]);
    for (let i = 0; i < parts.length; i++) {
      const blob = await next;
      if (mine !== seq) return;
      if (i + 1 < parts.length) next = make(parts[i + 1]);
      state.loading = false;
      await play(blob, opts.rate || 1);
      if (mine !== seq) return;
    }
  } catch (e) {
    if (mine !== seq || (e && e.name === "AbortError")) return;
    Object.assign(state, { error: e.message || String(e), errorId: id });
    throw e;
  } finally {
    if (mine === seq) Object.assign(state, { id: "", loading: false });
  }
}

export function stop() {
  seq++;
  if (controller) controller.abort();
  controller = null;
  if (audio) audio.pause();
  if (ended) ended();
  state.loading = false;
}
