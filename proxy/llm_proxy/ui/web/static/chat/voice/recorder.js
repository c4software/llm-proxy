// @ts-check
/*
  L'ENREGISTREMENT au micro d'une voix à cloner, avec VueUse : le flux du
  micro (useUserMedia), le vu-mètre et le chrono (useRafFn) ; l'audio,
  par MediaRecorder. Brut : sans contrôle automatique du gain ni
  annulation d'écho (le clonage copierait leurs artefacts). S'arrête de
  lui-même au bout de MAX secondes. À créer dans un setup() : le micro est
  rendu quand le composant disparaît.

    const rec = useRecorder();
    await rec.start((blob) => …);   // WebM ou Ogg : audio.js le lit
    rec.state.level, rec.state.seconds ; rec.stop() (ou MAX atteint)
*/
const MAX = 30;

export function useRecorder() {
  const state = Vue.reactive({ recording: false, seconds: 0, level: 0 });
  const supported = VueUse.useSupported(() => typeof MediaRecorder !== "undefined"
    && !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia));
  const mic = VueUse.useUserMedia({ constraints: { audio: {
    echoCancellation: false, autoGainControl: false, noiseSuppression: true, channelCount: 1 }, video: false } });
  /** @type {MediaRecorder | null} */
  let rec = null;
  /** @type {AudioContext | null} */
  let ctx = null;
  /** @type {AnalyserNode | null} */
  let analyser = null;
  const data = new Float32Array(1024);
  let began = 0;
  /** @type {((b: Blob) => void) | null} */
  let done = null;

  // À chaque image : la crête du signal, le temps écoulé.
  const meter = VueUse.useRafFn(() => {
    if (analyser) {
      analyser.getFloatTimeDomainData(data);
      let peak = 0;
      for (const v of data) peak = Math.max(peak, Math.abs(v));
      state.level = peak;
    }
    state.seconds = (performance.now() - began) / 1000;
    if (state.seconds >= MAX) stop();
  }, { immediate: false });

  function release() {
    meter.pause();
    mic.stop();
    if (ctx) ctx.close().catch(() => {});
    ctx = null; analyser = null; rec = null;
    state.recording = false;
    state.level = 0;
  }

  /** @param {(b: Blob) => void} onBlob l'enregistrement fini */
  async function start(onBlob) {
    done = onBlob;
    const stream = await mic.start();
    if (!stream) throw new Error("micro indisponible");
    const parts = [];
    const r = rec = new MediaRecorder(stream);
    r.ondataavailable = (e) => { if (e.data && e.data.size) parts.push(e.data); };
    r.onstop = () => {
      const blob = new Blob(parts, { type: r.mimeType || "audio/webm" });
      release();
      if (done) done(blob);
      done = null;
    };
    ctx = new AudioContext();
    analyser = ctx.createAnalyser();
    analyser.fftSize = data.length;
    ctx.createMediaStreamSource(stream).connect(analyser);
    began = performance.now();
    r.start();
    state.recording = true;
    state.seconds = 0;
    meter.resume();
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

  VueUse.tryOnScopeDispose(cancel);
  return { state, supported, start, stop, cancel, MAX };
}
