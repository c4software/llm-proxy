// @ts-check
// Composable voix et transcription du chat : lecture à voix haute (Écouter),
// modèles de synthèse vocale (voix prédéfinies ou clonées, studio), modèles
// ASR et enregistrement au micro.
import { ref, reactive, computed, watch, onUnmounted } from "vue";
import * as voice from "./voice/index.js";
import * as speech from "./voice/speech.js";
import * as asr from "./voice/transcribe.js";
import { useRecorder } from "./voice/recorder.js";
import { presetVoices, language as voiceLanguage } from "./voice/proxy.js";
import { clones as voiceClones, load as loadClones } from "./voice/clones.js";

export function useChatVoice({ current, pending, status, voiceMode, voiceOf, asrMode, uid, startRun, done }) {
  const voiceName = voice.label;
  const voiceSupported = voice.supported;
  const playing = computed(() => ({
    id: voice.state.id.startsWith("msg:") ? voice.state.id.slice(4) : "",
    loading: voice.state.loading,
  }));

  function listen(m) {
    if (playing.value.id === m.id) { voice.stop(); return; }
    voice.speak("msg:" + m.id, voice.speakable(m.final || m.content),
                { incognito: !!(current.value && current.value.incognito) })
      .catch((e) => { status.value = "Écoute : " + e.message; });
  }
  onUnmounted(() => { if (voice.state.id.startsWith("msg:")) voice.stop(); });

  const presets = reactive({});
  watch(() => current.value && voiceMode.value === "custom" && current.value.model, async (m) => {
    if (!m || presets[m]) return;
    try { presets[m] = await presetVoices(m); } catch (e) { status.value = "Voix : " + e.message; }
  }, { immediate: true });
  watch(voiceMode, (r) => { if (r === "clone") loadClones(); }, { immediate: true });

  const voiceChoices = computed(() => {
    const c = current.value;
    if (!c) return [];
    if (voiceMode.value === "custom") return (presets[c.model] || []).map((v) => ({ id: v, name: v }));
    if (voiceMode.value === "clone") return voiceClones.value.map((v) => ({ id: v.id, name: v.name }));
    return [];
  });
  watch(voiceChoices, (list) => {
    const c = current.value;
    const key = voiceMode.value === "clone" ? "voiceClone" : "voiceName";
    if (c && list.length && !list.some((v) => v.id === c[key])) c[key] = list[0].id;
  });

  const studioOpen = ref(false);
  const newVoice = () => { studioOpen.value = true; };
  function voiceSaved(c) {
    studioOpen.value = false;
    if (current.value) current.value.voiceClone = c.id;
  }
  const voiceLang = computed(() => voice.prefs.language);

  const justSpoken = ref("");
  function onlyOne(e) {
    justSpoken.value = "";
    for (const a of document.querySelectorAll("audio.gen-audio")) {
      if (a !== e.target && a instanceof HTMLAudioElement) a.pause();
    }
    voice.stop();
  }

  async function speakReply(conv) {
    const r = voiceOf(conv.model);
    const msg = { id: uid(), role: "assistant", content: "", reasoning: "", tools: [], sources: [],
                  attachments: [], voice: r, voiceName: "", model: conv.model, usage: null, error: "",
                  started: Date.now(), duration: 0 };
    const ask = [...conv.messages].reverse().find((m) => m.role === "user");
    conv.messages.push(msg);
    conv.updated = Date.now();
    const live = conv.messages[conv.messages.length - 1];
    const ctrl = startRun();
    try {
      if (!ask || !ask.content.trim()) throw new Error("Écrire le texte à lire.");
      const clone = voiceClones.value.find((v) => v.id === conv.voiceClone);
      live.voiceName = r === "custom" ? conv.voiceName || "" : r === "clone" ? (clone ? clone.name : "") : "décrite";
      const got = await speech.speakTurn({
        text: ask.content, model: conv.model, role: r, voice: conv.voiceName, clone: conv.voiceClone,
        instructions: conv.system, language: voice.prefs.language || voiceLanguage(),
        incognito: !!conv.incognito, signal: ctrl.signal,
        onPart: (k, n) => { live.progress = n > 1 ? `passage ${k + 1} / ${n}` : ""; } });
      live.attachments.push(got);
      if (current.value && current.value.id === conv.id && document.visibilityState === "visible") justSpoken.value = live.id;
    } catch (e) {
      live.error = e.name === "AbortError" ? "Arrêté." : e.message;
    } finally {
      delete live.progress;
      done(conv, live, ctrl);
    }
  }

  async function transcribeReply(conv) {
    const msg = { id: uid(), role: "assistant", content: "", reasoning: "", tools: [], sources: [],
                  attachments: [], asr: true, model: conv.model, usage: null, error: "",
                  started: Date.now(), duration: 0 };
    const ask = [...conv.messages].reverse().find((m) => m.role === "user");
    conv.messages.push(msg);
    conv.updated = Date.now();
    const live = conv.messages[conv.messages.length - 1];
    const ctrl = startRun();
    try {
      const sources = ((ask && ask.attachments) || []).filter((a) => a.kind === "audio" && a.data);
      const blobs = [];
      for (const a of sources) blobs.push(await (await fetch(a.data, { signal: ctrl.signal })).blob());
      const got = await asr.transcribeTurn({
        audio: blobs, model: conv.model, prompt: ask ? ask.content : "",
        language: asr.languageCode(voice.prefs.language), incognito: !!conv.incognito, signal: ctrl.signal,
        onPart: (k, n) => { live.progress = n > 1 ? `passage ${k + 1} / ${n}` : ""; } });
      live.audioDuration = got.duration;
      live.content = got.text || "(rien d'entendu)";
    } catch (e) {
      live.error = e.name === "AbortError" ? "Arrêté." : e.message;
    } finally {
      delete live.progress;
      done(conv, live, ctrl);
    }
  }

  const rec = useRecorder({ max: 10 * 60 });
  async function toggleRecord() {
    if (rec.state.recording) { rec.stop(); return; }
    try {
      await rec.start((blob) => {
        const ext = /ogg/.test(blob.type) ? "ogg" : /mp4/.test(blob.type) ? "m4a" : "webm";
        const name = "micro-" + new Date().toLocaleTimeString("fr-FR").replace(/:/g, "-") + "." + ext;
        const r = new FileReader();
        r.onload = () => { pending.value.push({ kind: "audio", name, type: blob.type, size: blob.size, data: String(r.result) }); };
        r.readAsDataURL(blob);
      });
      status.value = "";
    } catch (e) { status.value = "Micro : " + (e.name === "NotAllowedError" ? "accès refusé" : e.message); }
  }
  watch(asrMode, (on) => { if (!on && rec.state.recording) rec.cancel(); });

  return {
    voiceName, voiceSupported, playing, listen, voiceChoices, studioOpen,
    newVoice, voiceSaved, voiceLang, justSpoken, onlyOne, speakReply,
    transcribeReply, rec, toggleRecord,
  };
}
