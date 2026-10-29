// @ts-check
/*
  Les RÉGLAGES de la voix, dans ceux du chat : le moteur (navigateur ou
  modèle du proxy), la voix, le ton et la langue (proxy), le débit ; les
  voix clonées du compte (écouter la référence, la télécharger
  avec sa transcription, la supprimer ; en importer une téléchargée :
  voicefile.js) et le studio qui en crée une (VoiceStudio.js).
*/
import * as browser from "./browser.js";
import * as proxy from "./proxy.js";
import * as clones from "./clones.js";
import { prefs, presetKey, cloneKey, cloneModel } from "./index.js";
import { VoiceStudio } from "./VoiceStudio.js";
import * as voicefile from "./voicefile.js";
import * as audio from "./audio.js";
import { download } from "../../lib/files.js";

const { ref, reactive, computed, watch } = Vue;

export const VoiceSettings = {
  components: { VoiceStudio },
  props: { incognito: { type: Boolean, default: false } },
  setup() {
    const models = proxy.models;
    const proxyOk = proxy.available;
    // Les voix toutes faites de chaque modèle, lues quand le moteur du
    // proxy est choisi.
    const presets = reactive({});
    const presetError = ref("");
    async function loadPresets() {
      presetError.value = "";
      for (const m of models.value.custom) {
        if (presets[m]) continue;
        try { presets[m] = await proxy.presetVoices(m); } catch (e) {
          presetError.value = `Voix de ${m} : ${e.message}`;
        }
      }
      // Rien de choisi encore : la première voix venue.
      if (!prefs.proxyVoice) {
        const m = models.value.custom.find((x) => (presets[x] || []).length);
        if (m) prefs.proxyVoice = presetKey(m, presets[m][0]);
        else if (cloneModel.value && clones.clones.value.length) prefs.proxyVoice = cloneKey(clones.clones.value[0].id);
      }
    }
    // Les voix clonées du compte : leur liste (Télécharger, Copier
    // la transcription…) se montre quel que soit le moteur.
    clones.load();
    watch(() => [prefs.engine, models.value.custom.join()], () => {
      if (prefs.engine === "proxy") { clones.load().then(loadPresets); }
    }, { immediate: true });

    const presetGroups = computed(() => models.value.custom.map((m) => ({
      model: m, voices: (presets[m] || []).map((name) => ({ key: presetKey(m, name), name })) })));
    const isPreset = computed(() => prefs.proxyVoice.startsWith("preset:"));
    const defaultLang = computed(() => proxy.language() || "le modèle devine");

    // Les voix clonées : écouter une référence, en supprimer une (confirmée).
    const studioOpen = ref(false);
    // La référence écoutée (lue au proxy) : un <audio> caché, son adresse
    // par useObjectUrl.
    const listening = reactive({ id: "" });
    /** @type {any} */
    const refBlob = ref(null);
    const refUrl = VueUse.useObjectUrl(refBlob);
    const player = ref(null);
    const stopRef = () => { listening.id = ""; refBlob.value = null; };
    async function playRef(c) {
      if (listening.id === c.id) { stopRef(); return; }
      listening.id = c.id;
      try {
        const blob = await clones.wav(c.id);
        if (listening.id !== c.id) return;
        refBlob.value = blob;
        Vue.nextTick(() => player.value && player.value.play().catch(stopRef));
      } catch (e) {
        stopRef();
        importError.value = e.message || String(e);
      }
    }
    // Télécharger : le WAV avec son nom et sa transcription (voicefile.js).
    const saveRef = async (c) => download({ name: voicefile.fileName(c.name),
      content: await voicefile.pack(await clones.wav(c.id), { name: c.name, text: c.text }) }, "audio/wav");
    // Importer une voix téléchargée : gardée telle quelle si elle porte sa
    // transcription ; sinon, au studio (Fichier) de la compléter.
    const importError = ref("");
    const importer = VueUse.useFileDialog({ accept: "audio/wav,.wav", multiple: false });
    importer.onChange(async (files) => {
      const f = files && files[0];
      if (!f) return;
      importError.value = "";
      try {
        const tags = await voicefile.unpack(f);
        if (!tags.text) throw new Error("ce fichier n'a pas de transcription : Nouvelle voix clonée → Fichier");
        const got = await audio.normalize(f);
        const c = await clones.add({ name: tags.name || f.name.replace(/\.wav$/i, ""), text: tags.text,
                                     wav: got.wav, duration: got.duration, source: "importée" });
        use(c);
      } catch (e) {
        importError.value = "Import : " + (e.message || e);
      }
    });
    const importVoice = () => { clones.load(); importer.open({ reset: true }); };
    // La transcription d'une voix : le texte que demande sa référence
    // (reference_text), à reprendre ailleurs avec le WAV téléchargé.
    const clipboard = VueUse.useClipboard({ legacy: true });
    const copiedId = VueUse.refAutoReset("", 1400);
    function copyText(c) {
      clipboard.copy(c.text);
      copiedId.value = c.id;
    }
    const confirming = ref("");
    async function remove(c) {
      if (confirming.value !== c.id) { confirming.value = c.id; return; }
      confirming.value = "";
      if (listening.id === c.id) stopRef();
      await clones.remove(c.id);
      if (prefs.proxyVoice === cloneKey(c.id)) prefs.proxyVoice = "";
    }
    function use(c) {
      prefs.engine = "proxy";
      prefs.proxyVoice = cloneKey(c.id);
    }
    function saved(c) {
      studioOpen.value = false;
      use(c);
    }
    // Le studio ouvert : à l'écran.
    const root = ref(null);
    watch(studioOpen, (open) => {
      if (open) Vue.nextTick(() => { const el = root.value && root.value.querySelector(".voice-studio");
                                     if (el) el.scrollIntoView({ block: "nearest", behavior: "smooth" }); });
    }, { immediate: true });

    return { prefs, models, proxyOk, presetGroups, presetError, isPreset, defaultLang, cloneModel,
             clones: clones.clones, browserOk: browser.supported, browserVoices: browser.voices,
             languages: proxy.LANGUAGES, studioOpen, listening, playRef, saveRef, confirming, remove,
             use, saved, cloneKey, copyText, copiedId, root, importVoice, importError, refUrl, player, stopRef };
  },
  template: `
  <div class="field wide voice-settings" v-if="browserOk || proxyOk" ref="root">
    <span>Voix</span>
    <div class="voice-pick">
      <label v-if="proxyOk">moteur
        <select v-model="prefs.engine">
          <option value="browser" :disabled="!browserOk">navigateur</option>
          <option value="proxy">modèle du proxy</option></select></label>
      <label v-if="prefs.engine !== 'proxy' || !proxyOk">voix
        <select v-model="prefs.voice"><option value="">voix du navigateur (défaut)</option>
          <option v-for="v in browserVoices" :key="v.uri" :value="v.uri">{{ v.name }} — {{ v.lang }}{{ v.local ? "" : " (en ligne)" }}</option></select></label>
      <template v-else>
        <label>voix
          <select v-model="prefs.proxyVoice">
            <option value="" disabled>choisir une voix</option>
            <optgroup v-for="g in presetGroups" :key="g.model" :label="'Toutes faites — ' + g.model">
              <option v-for="v in g.voices" :key="v.key" :value="v.key">{{ v.name }}</option></optgroup>
            <optgroup v-if="cloneModel && clones.length" label="Clonées">
              <option v-for="c in clones" :key="c.id" :value="cloneKey(c.id)">{{ c.name }}</option></optgroup>
          </select></label>
        <label v-if="isPreset">ton
          <input type="text" v-model.lazy="prefs.tone" placeholder="Voix posée de conteur"></label>
        <label>langue
          <select v-model="prefs.language"><option value="">auto ({{ defaultLang }})</option>
            <option v-for="l in languages" :key="l.code" :value="l.name">{{ l.name }}</option></select></label>
        <label v-if="models.clone.length > 1">clonage
          <select v-model="prefs.cloneModel"><option value="">{{ models.clone[0] }}</option>
            <option v-for="m in models.clone.slice(1)" :key="m" :value="m">{{ m }}</option></select></label>
      </template>
      <label>débit
        <select v-model.number="prefs.rate">
          <option v-for="r in [0.8, 1, 1.2, 1.5, 2]" :key="r" :value="r">× {{ r }}</option></select></label>
    </div>
    <p class="msg-error" v-if="prefs.engine === 'proxy' && presetError">{{ presetError }}</p>
    <audio ref="player" hidden :src="refUrl || undefined" @ended="stopRef"></audio>

    <div class="voice-clones" v-if="proxyOk && cloneModel">
      <ul v-if="clones.length">
        <li v-for="c in clones" :key="c.id" :class="{ on: prefs.engine === 'proxy' && prefs.proxyVoice === cloneKey(c.id) }">
          <b>{{ c.name }}</b> <span class="dim">{{ c.duration.toFixed(1) }} s · {{ c.source }}</span>
          <button type="button" class="link" @click="use(c)">Utiliser</button>
          <button type="button" class="link" @click="playRef(c)">{{ listening.id === c.id ? "Arrêter" : "Référence" }}</button>
          <button type="button" class="link" @click="saveRef(c)"
                  title="Le WAV, avec son nom et sa transcription : à réimporter, ou pour curl">Télécharger</button>
          <button type="button" class="link" @click="copyText(c)"
                  title="Le texte de la référence (reference_text), à reprendre avec le WAV">{{
            copiedId === c.id ? "Copiée" : "Copier la transcription" }}</button>
          <button type="button" class="link danger" @click="remove(c)" @blur="confirming = ''">{{
            confirming === c.id ? "Confirmer la suppression" : "Supprimer" }}</button>
        </li>
      </ul>
      <div class="studio-bar" v-if="!studioOpen">
        <button type="button" class="btn ghost" @click="studioOpen = true">Nouvelle voix clonée…</button>
        <button type="button" class="btn ghost" @click="importVoice"
                title="Une voix téléchargée d'ici (son WAV porte nom et transcription)">Importer une voix…</button>
      </div>
      <voice-studio v-else :clone-model="cloneModel" :language="prefs.language" :incognito="incognito"
                    @saved="saved" @close="studioOpen = false"></voice-studio>
      <p class="msg-error" v-if="importError">{{ importError }}</p>
    </div>
  </div>`,
};
