/*
  Le panneau des ARTIFACTS, à droite du fil (comme celui de la mémoire) :
  ce que le modèle a montré par show_artifact dans la conversation — un
  artifact à la fois, ses versions, rendu dans un cadre ISOLÉ (sandbox
  sans allow-same-origin : il ne voit ni la page, ni sa session ; sa
  politique de contenu est dans outputs.artifactDoc). Le source se copie
  ou se télécharge ; « Large » élargit le panneau. « Réseau » l'ouvre à
  internet, pour CET artifact (`art.network`, gardé avec lui), après un
  avertissement : un artifact écrit par le modèle peut alors envoyer ce
  qu'il contient à n'importe quel site.
*/
import { artifactDoc, artifactFile } from "./artifacts.js";
import { useCopy, download } from "../lib/files.js";

const { ref, computed, watch } = Vue;

export const ArtifactPanel = {
  props: {
    artifacts: { type: Array, default: () => [] },
    selected: { type: String, default: "" },
  },
  emits: ["close", "select"],
  setup(props, { emit }) {
    const { copied, copy } = useCopy();
    const art = computed(() => props.artifacts.find((a) => a.id === props.selected)
      || props.artifacts[props.artifacts.length - 1] || null);
    const versionIndex = ref(-1);              // -1 : la dernière
    watch(() => [art.value && art.value.id, art.value && art.value.versions.length],
          () => { versionIndex.value = -1; });
    const version = computed(() => {
      if (!art.value) return null;
      const v = art.value.versions;
      return v[versionIndex.value < 0 ? v.length - 1 : versionIndex.value];
    });
    const doc = computed(() => (version.value
      ? artifactDoc(version.value, { network: !!art.value.network }) : ""));
    // Le réseau : coupé d'un clic, ouvert après l'avertissement.
    const asking = ref(false);
    watch(() => art.value && art.value.id, () => { asking.value = false; });
    function toggleNetwork() {
      if (art.value.network) { art.value.network = false; return; }
      asking.value = !asking.value;
    }
    function allowNetwork() {
      art.value.network = true;
      asking.value = false;
    }
    const wide = ref(false);
    const showSource = ref(false);
    const file = computed(() => (art.value && version.value ? artifactFile(art.value, version.value) : null));
    return { art, version, versionIndex, doc, wide, showSource, file, copied, copy, download,
             asking, toggleNetwork, allowNetwork,
             pick: (id) => emit("select", id), close: () => emit("close") };
  },
  template: `
  <section class="artifact-side" :class="{ wide }" v-if="art">
    <header class="artifact-head">
      <select v-if="artifacts.length > 1" class="art-pick" :value="art.id" @change="pick($event.target.value)"
              aria-label="Artifact affiché" :title="art.title">
        <option v-for="a in artifacts" :key="a.id" :value="a.id">{{ a.title }}</option></select>
      <h3 v-else :title="art.title">{{ art.title }}</h3>
      <select v-if="art.versions.length > 1" v-model.number="versionIndex" aria-label="Version">
        <option :value="-1">v{{ art.versions.length }}</option>
        <option v-for="(v, k) in art.versions.slice(0, -1)" :key="k" :value="k">v{{ k + 1 }}</option></select>
      <span class="tag" :title="'Type : ' + version.type">{{ version.type }}</span>
      <span class="artifact-actions">
        <button type="button" class="btn ghost art-btn" :aria-pressed="String(showSource)"
                @click="showSource = !showSource" :title="showSource ? 'Aperçu' : 'Source'"
                :aria-label="showSource ? 'Aperçu' : 'Source'">
          <svg v-if="showSource" class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>
          <svg v-else class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M8 8l-4 4 4 4M16 8l4 4-4 4"/></svg></button>
        <button type="button" class="btn ghost art-btn" @click="copy(file)"
                :title="copied === file.name ? 'Copié' : 'Copier le source'" aria-label="Copier le source">
          <svg v-if="copied === file.name" class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12l5 5 9-10"/></svg>
          <svg v-else class="ico" viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h8"/></svg></button>
        <button type="button" class="btn ghost art-btn" @click="download(file)"
                :title="'Télécharger ' + file.name" aria-label="Télécharger">
          <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg></button>
        <button type="button" class="btn ghost art-btn net" :aria-pressed="String(!!art.network)" @click="toggleNetwork"
                :title="art.network ? 'Réseau ouvert à cet artifact : couper' : 'Réseau bloqué : l’ouvrir à cet artifact (cartes, API…)'"
                aria-label="Réseau">
          <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c3 3.5 3 14.5 0 18M12 3c-3 3.5-3 14.5 0 18"/></svg></button>
        <button type="button" class="btn ghost art-btn" :aria-pressed="String(wide)" @click="wide = !wide"
                :title="wide ? 'Réduire le panneau' : 'Élargir le panneau'" aria-label="Large">
          <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg></button>
        <button type="button" class="btn ghost art-btn" title="Fermer" aria-label="Fermer" @click="close">
          <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
      </span>
    </header>
    <div class="artifact-warn" v-if="asking" role="alertdialog" aria-label="Ouvrir le réseau">
      <p><b>Ouvrir le réseau à cet artifact ?</b> Il pourra charger des cartes, des images,
        appeler des API — et envoyer ce qu'il contient à n'importe quel site. Il a été écrit
        par le modèle : à n'ouvrir que s'il vient d'une conversation de confiance. Il reste
        isolé de la page et de votre session.</p>
      <div class="card-actions">
        <button type="button" class="btn" @click="allowNetwork">Autoriser</button>
        <button type="button" class="btn ghost" @click="asking = false">Annuler</button>
      </div>
    </div>
    <pre class="artifact-source" v-if="showSource">{{ version.content }}</pre>
    <iframe v-else class="artifact-frame" :srcdoc="doc" sandbox="allow-scripts"
            referrerpolicy="no-referrer" :title="art.title"></iframe>
  </section>`,
};
