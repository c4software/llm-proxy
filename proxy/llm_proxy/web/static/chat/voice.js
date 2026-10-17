/*
  La VOIX du chat, par les modèles du proxy :

    dicter   le micro (getUserMedia), enregistré puis converti en WAV
             16 kHz mono DANS LA PAGE (les serveurs de transcription
             lisent le WAV ; le navigateur, lui, enregistre du WebM ou du
             MP4), envoyé à /ui/audio/transcriptions ; le texte rejoint la
             zone de saisie
    écouter  une réponse, sans son Markdown ni ses blocs de code, lue par
             /ui/audio/speech avec la voix choisie (/ui/audio/voices)

  Les modèles : ceux du catalogue complet (/ui/models?all=1) de type
  automatic-speech-recognition et text-to-speech ; le choix (modèle,
  voix) reste dans ce navigateur.
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

// Les modèles de voix du catalogue : {asr: [ids], tts: [ids]}.
export async function voiceModels() {
  const r = await call("GET", "/ui/models?all=1");
  const of = (type) => (r.data || []).filter((m) => m.type === type).map((m) => m.id).sort();
  return { asr: of("automatic-speech-recognition"), tts: of("text-to-speech") };
}

export async function voicesOf(model) {
  const r = await call("GET", "/ui/audio/voices?model=" + encodeURIComponent(model));
  return (r.data || []).map((v) => v.id);
}

// ── dicter ──────────────────────────────────────────────────────────────
// start() → un enregistreur ; stop() → le WAV (Blob).
export async function startRecording() {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const rec = new MediaRecorder(stream);
  const chunks = [];
  rec.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
  rec.start();
  return {
    stop: () => new Promise((resolve, reject) => {
      rec.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop());
        try { resolve(await toWav(new Blob(chunks, { type: rec.mimeType }))); }
        catch (e) { reject(e); }
      };
      rec.stop();
    }),
    cancel: () => { rec.onstop = null; rec.stop(); stream.getTracks().forEach((t) => t.stop()); },
  };
}

// N'importe quel audio que le navigateur sait décoder → WAV 16 kHz mono.
async function toWav(blob) {
  const RATE = 16000;
  const ctx = new AudioContext();
  const decoded = await ctx.decodeAudioData(await blob.arrayBuffer());
  ctx.close();
  const frames = Math.ceil(decoded.duration * RATE);
  const off = new OfflineAudioContext(1, Math.max(frames, 1), RATE);
  const src = off.createBufferSource();
  src.buffer = decoded;
  src.connect(off.destination);
  src.start();
  const pcm = (await off.startRendering()).getChannelData(0);
  const buf = new ArrayBuffer(44 + pcm.length * 2);
  const v = new DataView(buf);
  const str = (o, s) => [...s].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)));
  str(0, "RIFF"); v.setUint32(4, 36 + pcm.length * 2, true); str(8, "WAVE");
  str(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, RATE, true); v.setUint32(28, RATE * 2, true); v.setUint16(32, 2, true);
  v.setUint16(34, 16, true); str(36, "data"); v.setUint32(40, pcm.length * 2, true);
  for (let i = 0; i < pcm.length; i++) {
    const x = Math.max(-1, Math.min(1, pcm[i]));
    v.setInt16(44 + i * 2, x < 0 ? x * 0x8000 : x * 0x7fff, true);
  }
  return new Blob([buf], { type: "audio/wav" });
}

export async function transcribe(wav, model, incognito = false) {
  const form = new FormData();
  form.append("model", model);
  form.append("file", wav, "dictee.wav");
  const r = await fetch("/ui/audio/transcriptions", {
    method: "POST", body: form,
    headers: { accept: "application/json", ...(incognito ? { "x-incognito": "1" } : {}) },
  });
  let j = null;
  try { j = await r.json(); } catch { /* corps illisible */ }
  if (!r.ok) throw new Error((j && j.error && j.error.message) || `HTTP ${r.status}`);
  return String((j && j.text) || "").trim();
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
