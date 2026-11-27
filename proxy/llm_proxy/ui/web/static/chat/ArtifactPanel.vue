<script>
// @ts-check
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
  Largeur, Large, Plein écran, Réseau et son avertissement : SidePanel.vue,
  commun avec le terminal.
  Un PROJET (plusieurs fichiers) : son arborescence à gauche, le fichier
  choisi à droite — sa source colorée, ou, pour un HTML, son aperçu (le
  CSS et le JS du projet mis en ligne) ; seul l'aperçu de son entrée
  (entryOf) s'inscrit au pont. Copier : le fichier affiché ; Télécharger :
  le projet en .zip.
*/
import * as Vue from "vue";
import { artifactDoc, artifactFile, debugOn, entryOf, fileDoc, fileTree, loadHighlighter, projectDoc } from "./artifacts.js";
import SidePanel from "./SidePanel.vue";
import { useCopy, download } from "../lib/files.js";
import * as bridge from "./artifact-bridge.js";

const { ref, shallowRef, computed, watch, onUnmounted } = Vue;

const ArtifactPanel = {
  components: { SidePanel },
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
    // highlight.js, chargé au premier artifact de code ; le cadre attend
    // (un seul chargement, que le pont ne perde pas) ; s'il manque au
    // serveur, le code brut — et un nouvel essai à la version suivante.
    const hl = shallowRef(null);
    const hlLoading = ref(false);
    watch(version, (v) => {
      if (!v || (v.type !== "code" && v.type !== "project") || hl.value || hlLoading.value) return;
      hlLoading.value = true;
      loadHighlighter().then((m) => { hl.value = m; }, () => {}).finally(() => { hlLoading.value = false; });
    }, { immediate: true });
    // Un PROJET : le fichier choisi (gardé d'une version à l'autre s'il y
    // est encore ; sinon l'entrée, sinon le premier), ses dossiers repliés.
    const showSource = ref(false);
    const isProject = computed(() => !!version.value && version.value.type === "project");
    const path = ref("");
    const collapsed = ref(new Set());
    const entry = computed(() => (isProject.value ? entryOf(version.value) : null));
    watch(version, (v) => {
      if (!v || v.type !== "project" || v.files.some((f) => f.path === path.value)) return;
      path.value = (entryOf(v) || v.files[0]).path;
    }, { immediate: true });
    const current = computed(() => (isProject.value ? version.value.files.find((f) => f.path === path.value) || null : null));
    const isHtml = computed(() => !!current.value && /\.html?$/i.test(current.value.path));
    // L'arborescence (fileTree), masquée sous un dossier replié.
    const tree = computed(() => (isProject.value ? fileTree(version.value.files) : [])
      .filter((r) => ![...collapsed.value].some((d) => r.key.startsWith(d + "/"))));
    function toggleDir(key) {
      const s = new Set(collapsed.value);
      if (s.has(key)) s.delete(key); else s.add(key);
      collapsed.value = s;
    }
    const opts = () => ({ network: !!art.value.network, debug: debugOn(art.value), hl: hl.value });
    const doc = computed(() => {
      const v = version.value;
      if (!v || ((v.type === "code" || v.type === "project") && hlLoading.value)) return "";
      if (v.type !== "project") return artifactDoc(v, opts());
      if (!current.value) return "";
      return isHtml.value && !showSource.value ? projectDoc(v, current.value.path, opts()) : fileDoc(current.value, hl.value);
    });
    // Le cadre affiché, inscrit au pont (artifact-bridge.js) à chaque
    // chargement : le modèle teste la version que l'utilisateur voit.
    const frame = ref(null);
    const shownVersion = computed(() => {
      if (!art.value) return 0;
      return versionIndex.value < 0 ? art.value.versions.length : versionIndex.value + 1;
    });
    // Seulement si le débogage est actif : sinon le document n'a pas de pont.
    // Un projet : seulement l'aperçu de son entrée.
    function onFrameLoad() {
      if (!frame.value || !art.value || !debugOn(art.value)) return;
      if (isProject.value && !(entry.value && path.value === entry.value.path && !showSource.value)) return;
      bridge.register(art.value.id, shownVersion.value, frame.value);
    }
    // Le pont attend ce projet (inspect_artifact, run_in_artifact) : son
    // entrée, en aperçu.
    watch(() => [bridge.wanted.value, art.value && art.value.id], ([want, id]) => {
      if (!want || want.id !== id) return;
      bridge.wanted.value = null;
      if (isProject.value && entry.value) { path.value = entry.value.path; showSource.value = false; }
    }, { immediate: true });
    // Un autre document (version, réseau, débogage) : l'ancien n'est plus
    // à interroger, le nouveau s'inscrira à son chargement.
    watch(doc, () => { if (frame.value) bridge.unregister(frame.value); });
    onUnmounted(() => { if (frame.value) bridge.unregister(frame.value); });
    const inspecting = computed(() => !!art.value && bridge.busy.value === art.value.id);
    // Copier : le texte affiché (pour un projet, le fichier choisi) ;
    // Télécharger : l'artifact (un projet : son .zip).
    // Le .zip d'un projet n'est construit qu'au téléchargement.
    const fileName = computed(() => (!art.value ? "" : isProject.value ? `${art.value.id}.zip`
      : artifactFile(art.value, version.value).name));
    const copyable = computed(() => (current.value ? { name: current.value.path, content: current.value.content }
      : version.value ? artifactFile(art.value, version.value) : null));
    const save = () => download(artifactFile(art.value, version.value), isProject.value ? "application/zip" : undefined);
    return { art, version, versionIndex, doc, frame, debugOn, onFrameLoad, inspecting, showSource, fileName, copyable, save,
             copied, copy, isProject, path, current, isHtml, tree, collapsed, toggleDir,
             pick: (id) => emit("select", id), close: () => emit("close") };
  },
};

export default ArtifactPanel;
</script>

<template>
  <side-panel v-if="art" storage="llm-proxy-artifact-width" :network="!!art.network" :subject="art.id"
              :network-titles="['Réseau ouvert à cet artifact : couper', 'Réseau bloqué : l’ouvrir à cet artifact (cartes, API…)']"
              @network="art.network = $event" @close="close">
    <template #head>
      <select v-if="artifacts.length > 1" class="art-pick" :value="art.id" @change="pick($event.target.value)"
              aria-label="Artifact affiché" :title="art.title">
        <option v-for="a in artifacts" :key="a.id" :value="a.id">{{ a.title }}</option></select>
      <h3 v-else :title="art.title">{{ art.title }}</h3>
      <select v-if="art.versions.length > 1" v-model.number="versionIndex" aria-label="Version">
        <option :value="-1">v{{ art.versions.length }}</option>
        <option v-for="(v, k) in art.versions.slice(0, -1)" :key="k" :value="k">v{{ k + 1 }}</option></select>
      <span class="tag" :title="'Type : ' + version.type">{{ version.type }}</span>
    </template>
    <template #actions>
      <button type="button" class="btn ghost art-btn" v-if="!isProject || isHtml" :aria-pressed="String(showSource)"
              @click="showSource = !showSource" :title="showSource ? 'Aperçu' : 'Source'"
              :aria-label="showSource ? 'Aperçu' : 'Source'">
        <svg v-if="showSource" class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>
        <svg v-else class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M8 8l-4 4 4 4M16 8l4 4-4 4"/></svg></button>
      <button type="button" class="btn ghost art-btn" @click="copy(copyable)"
              :title="copied === copyable.name ? 'Copié' : 'Copier ' + (isProject ? copyable.name : 'le source')" aria-label="Copier le source">
        <svg v-if="copied === copyable.name" class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12l5 5 9-10"/></svg>
        <svg v-else class="ico" viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h8"/></svg></button>
      <button type="button" class="btn ghost art-btn" @click="save"
              :title="'Télécharger ' + fileName" aria-label="Télécharger">
        <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg></button>
    </template>
    <template #actions-end>
      <button type="button" class="btn ghost art-btn net dbg" :aria-pressed="String(debugOn(art))" @click="art.debug = !debugOn(art)"
              :title="debugOn(art) ? 'Débogage actif : le modèle peut lire la console de cet artifact, y exécuter du code, le capturer — couper (recharge l’artifact)' : 'Débogage coupé : le permettre au modèle pour cet artifact (console, code, capture)'"
              aria-label="Débogage">
        <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M8 6a4 4 0 0 1 8 0M7 9h10v5a5 5 0 0 1-10 0V9zM12 9v10M4 12h3M17 12h3M5 18l2.5-1.5M19 18l-2.5-1.5M5 7l2.5 2M19 7l-2.5 2"/></svg></button>
    </template>
    <template #network-warning>
      <b>Ouvrir le réseau à cet artifact ?</b> Il pourra charger des cartes, des images,
      appeler des API — et envoyer ce qu'il contient à n'importe quel site. Il a été écrit
      par le modèle : à n'ouvrir que s'il vient d'une conversation de confiance. Il reste
      isolé de la page et de votre session.
    </template>
    <p class="artifact-inspect" v-if="inspecting" role="status">Le modèle teste cet artifact…</p>
    <div class="art-project" v-if="isProject"><div class="art-project-in">
      <nav class="art-tree" aria-label="Fichiers du projet">
        <button type="button" v-for="r in tree" :key="r.key" class="art-node" :class="{ dir: r.dir }"
                :style="{ paddingLeft: (8 + r.depth * 14) + 'px' }" :aria-current="!r.dir && r.key === path ? 'true' : null"
                :aria-expanded="r.dir ? String(!collapsed.has(r.key)) : null" :title="r.key"
                @click="r.dir ? toggleDir(r.key) : (path = r.key)">{{ r.dir ? (collapsed.has(r.key) ? '▸ ' : '▾ ') : '' }}{{ r.name }}</button>
      </nav>
      <iframe class="artifact-frame" ref="frame" :srcdoc="doc" sandbox="allow-scripts"
              referrerpolicy="no-referrer" :title="current ? current.path : art.title" @load="onFrameLoad"></iframe>
    </div></div>
    <pre class="artifact-source" v-else-if="showSource">{{ version.content }}</pre>
    <iframe v-else class="artifact-frame" ref="frame" :srcdoc="doc" sandbox="allow-scripts"
            referrerpolicy="no-referrer" :title="art.title" @load="onFrameLoad"></iframe>
  </side-panel>
</template>
