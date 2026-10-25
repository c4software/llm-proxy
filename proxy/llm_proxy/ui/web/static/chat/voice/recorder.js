// @ts-check
/*
  L'ENREGISTREMENT au micro d'une voix à cloner (MediaRecorder) : brut,
  sans contrôle automatique du gain ni annulation d'écho (le clonage
  copierait leurs artefacts). Le niveau d'entrée, pour le vu-mètre, et
  l'arrêt de lui-même au bout de MAX secondes.

    const rec = useRecorder();
    await rec.start((blob) => …);   // WebM ou Ogg : audio.js le lit
    rec.state.level, rec.state.seconds ; rec.stop() (ou MAX atteint)
*/
const MAX = 30;

export const supported = typeof navigator !== "undefined" && !!navigator.mediaDevices
  && !!navigator.mediaDevices.getUserMedia && typeof MediaRecorder !== "undefined";

export function useRecorder() {
  const state = Vue.reactive({ recording: false, seconds: 0, level: 0 });
  /** @type {MediaRecorder | null} */
  let rec = null;
  /** @type {MediaStream | null} */
  let stream = null;
  /** @type {AudioContext | null} */
  let ctx = null;
  let frame = 0, began = 0;
  /** @type {Blob[]} */
  let parts = [];
  /** @type {((b: Blob) => void) | null} */
  let done = null;

  function release() {
    cancelAnimationFrame(frame);
    if (stream) for (const t of stream.getTracks()) t.stop();
    if (ctx) ctx.close().catch(() => {});
    stream = null; ctx = null;
    state.recording = false;
    state.level = 0;
  }

  /** @param {(b: Blob) => void} onBlob l'enregistrement fini */
  async function start(onBlob) {
    done = onBlob;
    stream = await navigator.mediaDevices.getUserMedia({ audio: {
      echoCancellation: false, autoGainControl: false, noiseSuppression: true, channelCount: 1 } });
    parts = [];
    rec = new MediaRecorder(stream);
    rec.ondataavailable = (e) => { if (e.data && e.data.size) parts.push(e.data); };
    rec.onstop = () => {
      const blob = new Blob(parts, { type: rec ? rec.mimeType : "audio/webm" });
      release();
      if (done) done(blob);
      done = null;
    };
    // Le vu-mètre : la crête de chaque image.
    ctx = new AudioContext();
    const an = ctx.createAnalyser();
    an.fftSize = 1024;
    ctx.createMediaStreamSource(stream).connect(an);
    const data = new Float32Array(an.fftSize);
    began = performance.now();
    const tick = () => {
      an.getFloatTimeDomainData(data);
      let peak = 0;
      for (const v of data) peak = Math.max(peak, Math.abs(v));
      state.level = peak;
      state.seconds = (performance.now() - began) / 1000;
      if (state.seconds >= MAX) { stop(); return; }
      frame = requestAnimationFrame(tick);
    };
    rec.start();
    state.recording = true;
    state.seconds = 0;
    tick();
  }

  function stop() {
    if (rec && rec.state !== "inactive") rec.stop();
    else release();
  }

  /** Abandonné : le micro rendu, rien de gardé. */
  function cancel() {
    done = null;
    if (rec && rec.state !== "inactive") { rec.onstop = null; rec.stop(); }
    release();
  }

  return { state, start, stop, cancel, MAX };
}
