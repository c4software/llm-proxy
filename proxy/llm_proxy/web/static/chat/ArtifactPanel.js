/*
  Le panneau des ARTIFACTS, à droite du fil (comme celui de la mémoire) :
  ce que le modèle a montré par show_artifact dans la conversation — un
  artifact à la fois, ses versions, rendu dans un cadre ISOLÉ (sandbox
  sans allow-same-origin : il ne voit ni la page, ni sa session ; sa
  politique de contenu est dans artifacts.js). Le source se copie
  ou se télécharge ; « Large » élargit le panneau. « Réseau » l'ouvre à
  internet, pour CET artifact (`art.network`, gardé avec lui), après un
  avertissement : un artifact écrit par le modèle peut alors envoyer ce
  qu'il contient à n'importe quel site. « Débogage » (`art.debug`, actif
  par défaut) permet au modèle de tester cet artifact (artifact-bridge.js) ;
  coupé, le modèle peut proposer de le rétablir (request_artifact_debug).
*/
import { artifactDoc, artifactFile, debugOn } from "./artifacts.js";
import { useCopy, download } from "../lib/files.js";
import * as bridge from "./artifact-bridge.js";

const { ref, computed, watch, onUnmounted } = Vue;

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
      ? artifactDoc(version.value, { network: !!art.value.network, debug: debugOn(art.value) }) : ""));
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
    // Le cadre affiché, inscrit au pont (artifact-bridge.js) à chaque
    // chargement : le modèle teste la version que l'utilisateur voit.
    const frame = ref(null);
    const shownVersion = computed(() => {
      if (!art.value) return 0;
      return versionIndex.value < 0 ? art.value.versions.length : versionIndex.value + 1;
    });
    // Seulement si le débogage est actif : sinon le document n'a pas de pont.
    function onFrameLoad() {
      if (frame.value && art.value && debugOn(art.value)) bridge.register(art.value.id, shownVersion.value, frame.value);
    }
    // Un autre document (version, réseau, débogage) : l'ancien n'est plus
    // à interroger, le nouveau s'inscrira à son chargement.
    watch(doc, () => { if (frame.value) bridge.unregister(frame.value); });
    onUnmounted(() => { if (frame.value) bridge.unregister(frame.value); });
    const inspecting = computed(() => !!art.value && bridge.busy.value === art.value.id);
    const wide = ref(false);
    // La LARGEUR du panneau, tirée par sa poignée (bord gauche) : gardée
    // dans ce navigateur ; 0 = celle par défaut. « Large » la remplace tant
    // qu'il est actif ; un double-clic sur la poignée revient au défaut.
    const MIN = 320;
    const width = VueUse.useLocalStorage("llm-proxy-artifact-width", 0);
    const dragging = ref(false);
    function clampWidth(w) {
      const host = panel.value && panel.value.parentElement;
      const max = host ? host.getBoundingClientRect().width - 320 : window.innerWidth - 320;
      return Math.round(Math.max(MIN, Math.min(w, Math.max(MIN, max))));
    }
    function startResize(e) {
      if (e.button !== 0 || !panel.value) return;
      e.preventDefault();
      const handle = e.currentTarget;
      handle.setPointerCapture(e.pointerId);
      const right = panel.value.getBoundingClientRect().right;
      dragging.value = true;
      wide.value = false;
      const move = (ev) => { width.value = clampWidth(right - ev.clientX); };
      const stop = () => {
        dragging.value = false;
        handle.removeEventListener("pointermove", move);
        handle.removeEventListener("pointerup", stop);
        handle.removeEventListener("pointercancel", stop);
      };
      handle.addEventListener("pointermove", move);
      handle.addEventListener("pointerup", stop);
      handle.addEventListener("pointercancel", stop);
    }
    function keyResize(e) {
      const step = e.shiftKey ? 96 : 32;
      const now = width.value || (panel.value ? panel.value.getBoundingClientRect().width : 480);
      if (e.key === "ArrowLeft") { e.preventDefault(); wide.value = false; width.value = clampWidth(now + step); }
      else if (e.key === "ArrowRight") { e.preventDefault(); wide.value = false; width.value = clampWidth(now - step); }
      else if (e.key === "Home") { e.preventDefault(); width.value = 0; }
    }
    const sized = computed(() => !!width.value && !wide.value && !full.value);
    const panelStyle = computed(() => (sized.value ? { width: width.value + "px" } : {}));
    // Plein écran : le panneau entier, son en-tête compris (de quoi en sortir).
    const panel = ref(null);
    const { isSupported: canFull, isFullscreen: full, toggle: toggleFull } = VueUse.useFullscreen(panel);
    const showSource = ref(false);
    const file = computed(() => (art.value && version.value ? artifactFile(art.value, version.value) : null));
    return { art, version, versionIndex, doc, frame, debugOn, onFrameLoad, inspecting, wide, width, dragging, sized, panelStyle,
             startResize, keyResize, resetWidth: () => { width.value = 0; }, panel, canFull, full, toggleFull, showSource, file, copied, copy, download,
             asking, toggleNetwork, allowNetwork,
             pick: (id) => emit("select", id), close: () => emit("close") };
  },
  template: `
  <section class="artifact-side" :class="{ wide, sized, dragging }" v-if="art" ref="panel" :style="panelStyle">
    <div class="artifact-resize" role="separator" aria-orientation="vertical" tabindex="0"
         aria-label="Largeur du panneau" title="Tirer pour redimensionner ; double-clic : largeur par défaut"
         :aria-valuenow="width || undefined" @pointerdown="startResize" @dblclick="resetWidth" @keydown="keyResize"></div>
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
        <button type="button" class="btn ghost art-btn net dbg" :aria-pressed="String(debugOn(art))" @click="art.debug = !debugOn(art)"
                :title="debugOn(art) ? 'Débogage actif : le modèle peut lire la console de cet artifact, y exécuter du code, le capturer — couper (recharge l’artifact)' : 'Débogage coupé : le permettre au modèle pour cet artifact (console, code, capture)'"
                aria-label="Débogage">
          <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M8 6a4 4 0 0 1 8 0M7 9h10v5a5 5 0 0 1-10 0V9zM12 9v10M4 12h3M17 12h3M5 18l2.5-1.5M19 18l-2.5-1.5M5 7l2.5 2M19 7l-2.5 2"/></svg></button>
        <button type="button" class="btn ghost art-btn" :aria-pressed="String(wide)" @click="wide = !wide"
                :title="wide ? 'Réduire le panneau' : 'Élargir le panneau'" aria-label="Large">
          <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12h18M7 8l-4 4 4 4M17 8l4 4-4 4"/></svg></button>
        <button type="button" class="btn ghost art-btn" v-if="canFull" :aria-pressed="String(full)" @click="toggleFull()"
                :title="full ? 'Quitter le plein écran (Échap)' : 'Plein écran'" aria-label="Plein écran">
          <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path v-if="!full" d="M3 3h6M3 3v6M21 3h-6M21 3v6M3 21h6M3 21v-6M21 21h-6M21 21v-6"/><path v-else d="M9 3v6H3M15 3v6h6M9 21v-6H3M15 21v-6h6"/></svg></button>
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
    <p class="artifact-inspect" v-if="inspecting" role="status">Le modèle teste cet artifact…</p>
    <pre class="artifact-source" v-if="showSource">{{ version.content }}</pre>
    <iframe v-else class="artifact-frame" ref="frame" :srcdoc="doc" sandbox="allow-scripts"
            referrerpolicy="no-referrer" :title="art.title" @load="onFrameLoad"></iframe>
  </section>`,
};
