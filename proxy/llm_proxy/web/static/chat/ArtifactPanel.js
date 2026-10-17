/*
  Le panneau des ARTIFACTS, à droite du fil (comme celui de la mémoire) :
  ce que le modèle a montré par show_artifact dans la conversation — un
  artifact à la fois, ses versions, rendu dans un cadre ISOLÉ (sandbox
  sans allow-same-origin : il ne voit ni la page, ni sa session ; sa
  politique de contenu est dans outputs.artifactDoc). Le source se copie
  ou se télécharge ; « Large » élargit le panneau.
*/
import { artifactDoc, artifactFile } from "./outputs.js";
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
    const doc = computed(() => (version.value ? artifactDoc(version.value) : ""));
    const wide = ref(false);
    const showSource = ref(false);
    const file = computed(() => (art.value && version.value ? artifactFile(art.value, version.value) : null));
    return { art, version, versionIndex, doc, wide, showSource, file, copied, copy, download,
             pick: (id) => emit("select", id), close: () => emit("close") };
  },
  template: `
  <section class="artifact-side" :class="{ wide }" v-if="art">
    <header class="artifact-head">
      <select v-if="artifacts.length > 1" :value="art.id" @change="pick($event.target.value)"
              aria-label="Artifact affiché">
        <option v-for="a in artifacts" :key="a.id" :value="a.id">{{ a.title }}</option></select>
      <h3 v-else :title="art.title">{{ art.title }}</h3>
      <select v-if="art.versions.length > 1" v-model.number="versionIndex" aria-label="Version">
        <option :value="-1">v{{ art.versions.length }} (dernière)</option>
        <option v-for="(v, k) in art.versions.slice(0, -1)" :key="k" :value="k">v{{ k + 1 }}</option></select>
      <span class="tag">{{ version.type }}</span>
      <span class="artifact-actions">
        <button type="button" class="btn ghost" :aria-pressed="String(showSource)"
                @click="showSource = !showSource">{{ showSource ? "Aperçu" : "Source" }}</button>
        <button type="button" class="btn ghost" @click="copy(file)">{{ copied === file.name ? "Copié" : "Copier" }}</button>
        <button type="button" class="btn ghost" @click="download(file)">Télécharger</button>
        <button type="button" class="btn ghost" :aria-pressed="String(wide)" @click="wide = !wide"
                title="Élargir le panneau">Large</button>
        <button type="button" class="icon" title="Fermer" @click="close">×</button>
      </span>
    </header>
    <pre class="artifact-source" v-if="showSource">{{ version.content }}</pre>
    <iframe v-else class="artifact-frame" :srcdoc="doc" sandbox="allow-scripts"
            referrerpolicy="no-referrer" :title="art.title"></iframe>
  </section>`,
};
