// @ts-check
/*
  Le STUDIO d'une voix clonée (docs/voix.md, « Cloner une voix ») : sa
  référence, de trois sources au choix —

    micro          lire le texte proposé (une quinzaine de secondes, pièce
                   calme), recorder.js
    fichier        un enregistrement qu'on a déjà, et sa transcription
    description    une voix INVENTÉE par le modèle de conception, d'après
                   sa description (genre, âge, timbre, accent, style), qui
                   lit le texte ; relancer jusqu'à ce qu'elle plaise

  — normalisée dans la page (audio.js), écoutée, ESSAYÉE sur une autre
  phrase par le modèle de clonage, puis gardée dans ce navigateur
  (clones.js). La transcription doit être exacte, mot pour mot.
*/
import * as audio from "./audio.js";
import * as clones from "./clones.js";
import * as proxy from "./proxy.js";
import { useRecorder, supported as canRecord } from "./recorder.js";

const { ref, reactive, computed, watch, onUnmounted } = Vue;

const SAMPLE = "Il était une fois, au bord d'une forêt immense, un vieux meunier qui vivait seul "
  + "avec son chat. Chaque matin, il ouvrait les volets de son moulin et regardait le soleil se "
  + "lever sur les champs de blé.";
const DESCRIPTION = "Voix masculine française d'une cinquantaine d'années, grave, chaude et "
  + "veloutée, légèrement feutrée. Narrateur professionnel de livre audio : diction parfaite, "
  + "accent français standard, débit posé, intonation naturelle et expressive sans excès.";
const TRIAL = "Le meunier soupira longuement. Il savait que le chat avait raison.";
const NAMES = { micro: "Ma voix", fichier: "Voix importée", description: "Voix conçue" };

export const VoiceStudio = {
  props: {
    cloneModel: { type: String, required: true },
    language: { type: String, default: "" },
    incognito: { type: Boolean, default: false },
  },
  emits: ["saved", "close"],
  setup(props, { emit }) {
    const designModels = computed(() => proxy.models.value.design);
    const source = ref(canRecord ? "micro" : "fichier");
    const form = reactive({ text: SAMPLE, description: DESCRIPTION, designModel: "", name: "", trial: TRIAL });
    const rec = useRecorder();
    const busy = ref("");
    const error = ref("");
    /** @type {any} */
    const result = ref(null);       // {wav, duration, lufs, url, source}
    const trialUrl = ref("");
    const lang = computed(() => props.language || proxy.language());
    // Le texte proposé ne transcrit pas un fichier : vidé pour lui, rendu
    // au micro et à la description s'il n'a pas été écrit.
    watch(source, (to) => {
      if (to === "fichier" && form.text === SAMPLE) form.text = "";
      else if (to !== "fichier" && !form.text.trim()) form.text = SAMPLE;
    });

    function setResult(r) {
      if (result.value) URL.revokeObjectURL(result.value.url);
      result.value = r && { ...r, url: URL.createObjectURL(r.wav) };
      if (trialUrl.value) { URL.revokeObjectURL(trialUrl.value); trialUrl.value = ""; }
    }

    /** @param {Blob} blob @param {string} from */
    async function prepare(blob, from) {
      busy.value = "Normalisation…";
      error.value = "";
      try {
        setResult({ ...(await audio.normalize(blob)), source: from });
        if (!form.name) form.name = NAMES[from];
      } catch (e) {
        error.value = e.message || String(e);
      } finally { busy.value = ""; }
    }

    async function record() {
      if (rec.state.recording) { rec.stop(); return; }
      error.value = "";
      try {
        await rec.start((blob) => { if (blob.size) prepare(blob, "micro"); });
      } catch (e) {
        error.value = "Micro : " + (e.message || e);
      }
    }

    /** @param {Event} e */
    function pickFile(e) {
      const input = /** @type {HTMLInputElement} */ (e.target);
      const f = input.files && input.files[0];
      input.value = "";
      if (f) prepare(f, "fichier");
    }

    async function design() {
      if (!form.text.trim() || !form.description.trim()) { error.value = "Texte et description attendus."; return; }
      busy.value = "Conception de la voix…";
      error.value = "";
      try {
        const blob = await proxy.synthesize({
          model: form.designModel || designModels.value[0], input: form.text.trim(),
          instructions: form.description.trim(), ...(lang.value ? { language: lang.value } : {}),
        }, { incognito: props.incognito });
        await prepare(blob, "description");
      } catch (e) {
        error.value = e.message || String(e);
        busy.value = "";
      }
    }

    async function trial() {
      if (!result.value || !form.text.trim()) return;
      busy.value = "Essai de la voix…";
      error.value = "";
      try {
        const blob = await proxy.synthesize({
          model: props.cloneModel, input: form.trial.trim() || TRIAL,
          reference_audio: await audio.base64(result.value.wav), reference_text: form.text.trim(),
          ...(lang.value ? { language: lang.value } : {}),
        }, { incognito: props.incognito });
        if (trialUrl.value) URL.revokeObjectURL(trialUrl.value);
        trialUrl.value = URL.createObjectURL(blob);
      } catch (e) {
        error.value = e.message || String(e);
      } finally { busy.value = ""; }
    }

    async function save() {
      if (!result.value || !form.text.trim()) return;
      try {
        const clone = await clones.add({
          name: form.name.trim() || NAMES[result.value.source] || "Voix", text: form.text.trim(),
          wav: result.value.wav, duration: result.value.duration, source: result.value.source });
        emit("saved", clone);
      } catch (e) {
        error.value = "Enregistrement dans ce navigateur : " + (e.message || e);
      }
    }

    // Conseils sur la durée (3 à 20 s clonent le mieux).
    const advice = computed(() => {
      const d = result.value && result.value.duration;
      if (!d) return "";
      if (d < 3) return "Courte : 3 à 20 secondes clonent le mieux.";
      if (d > 20) return "Longue : elle alourdit chaque requête sans améliorer la voix (3 à 20 s conseillées).";
      return "";
    });

    onUnmounted(() => {
      rec.cancel();
      setResult(null);
    });

    return { source, form, rec, busy, error, result, trialUrl, advice, designModels, canRecord,
             record, pickFile, design, trial, save, close: () => emit("close") };
  },
  template: `
  <div class="voice-studio">
    <div class="studio-head">
      <b>Nouvelle voix clonée</b>
      <div class="seg" role="tablist">
        <button type="button" v-if="canRecord" :aria-pressed="String(source === 'micro')"
                :disabled="rec.state.recording" @click="source = 'micro'">Micro</button>
        <button type="button" :aria-pressed="String(source === 'fichier')"
                :disabled="rec.state.recording" @click="source = 'fichier'">Fichier</button>
        <button type="button" v-if="designModels.length" :aria-pressed="String(source === 'description')"
                :disabled="rec.state.recording" @click="source = 'description'">Description</button>
      </div>
      <button type="button" class="icon" title="Fermer" aria-label="Fermer" @click="close">✕</button>
    </div>

    <label class="field" v-if="source === 'description'">
      <span>Description de la voix</span>
      <textarea v-model="form.description" rows="3" spellcheck="false"></textarea>
    </label>
    <label class="field">
      <span>{{ source === 'micro' ? "Texte à lire à voix haute" : source === 'fichier'
        ? "Transcription exacte du fichier" : "Texte lu par la voix conçue" }}</span>
      <textarea v-model="form.text" rows="3" :placeholder="source === 'fichier' ? 'Ce qui est dit dans le fichier, mot pour mot.' : ''"></textarea>
    </label>
    <p class="dim" v-if="source === 'micro'">Une quinzaine de secondes, dans une pièce calme, d'une voix
      naturelle. La transcription doit correspondre mot pour mot à ce qui est dit : corriger le texte
      si vous vous en écartez.</p>

    <div class="studio-bar">
      <template v-if="source === 'micro'">
        <button type="button" class="btn" :class="{ danger: rec.state.recording }" :disabled="!!busy" @click="record">
          {{ rec.state.recording ? "Arrêter" : result && result.source === 'micro' ? "Réenregistrer" : "Enregistrer" }}</button>
        <span class="rec-meter" v-if="rec.state.recording" aria-hidden="true">
          <i :style="{ width: Math.min(100, rec.state.level * 140) + '%' }"></i></span>
        <span class="dim" v-if="rec.state.recording">{{ rec.state.seconds.toFixed(1) }} s / {{ rec.MAX }} s</span>
      </template>
      <label class="btn ghost" v-else-if="source === 'fichier'" :class="{ disabled: !!busy }">Choisir un fichier audio…
        <input type="file" accept="audio/*,.wav,.mp3,.ogg,.flac,.m4a,.webm" hidden :disabled="!!busy" @change="pickFile"></label>
      <template v-else>
        <select v-if="designModels.length > 1" v-model="form.designModel" aria-label="Modèle de conception">
          <option value="">{{ designModels[0] }}</option>
          <option v-for="m in designModels.slice(1)" :key="m" :value="m">{{ m }}</option></select>
        <button type="button" class="btn" :disabled="!!busy" @click="design">
          {{ result && result.source === 'description' ? "Relancer" : "Concevoir la voix" }}</button>
      </template>
      <span class="dim" v-if="busy"><span class="spinner" aria-hidden="true"></span> {{ busy }}</span>
    </div>
    <p class="msg-error" v-if="error">{{ error }}</p>

    <div class="studio-result" v-if="result">
      <div class="dim">Référence normalisée : {{ result.duration.toFixed(1) }} s · mono 24 kHz ·
        {{ result.lufs.toFixed(1) }} LUFS<template v-if="advice"> — {{ advice }}</template></div>
      <audio controls :src="result.url"></audio>
      <label class="field">
        <span>Essayer sur une autre phrase</span>
        <div class="studio-bar">
          <input v-model="form.trial" type="text">
          <button type="button" class="btn ghost" :disabled="!!busy || !form.text.trim()" @click="trial">Essayer</button>
        </div>
      </label>
      <audio v-if="trialUrl" controls autoplay :src="trialUrl"></audio>
      <label class="field">
        <span>Nom de la voix</span>
        <div class="studio-bar">
          <input v-model="form.name" type="text" maxlength="60">
          <button type="button" class="btn" :disabled="!!busy || !form.text.trim()" @click="save">Garder cette voix</button>
        </div>
      </label>
    </div>
    <p class="dim small">Gardée dans ce navigateur seulement ; l'enregistrement part avec chaque lecture.
      Ne cloner que des voix qu'on a le droit d'utiliser : une voix conçue, la sienne, ou celle d'une
      personne d'accord.</p>
  </div>`,
};
