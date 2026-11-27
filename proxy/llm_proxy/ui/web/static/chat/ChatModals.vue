<script>
// Modales téléportées du chat : lightbox d'images (navigation, retouche,
// agrandissement), studio de voix clonée et aperçu d'un fichier produit.
import VoiceStudio from "./voice/VoiceStudio.vue";
import { ownImage } from "./history.js";
import { size } from "../lib/format.js";

export default {
  components: { VoiceStudio },
  props: {
    current: { type: Object, default: null },
    zoom: { type: Object, default: null },
    zoomIndex: { type: Number, default: -1 },
    assetsCount: { type: Number, default: 0 },
    streaming: { type: Boolean, default: false },
    upscaleOpt: { type: Object, default: null },
    upscaleFactor: { type: Number, default: 2 },
    upscaleStrength: { type: Number, default: null },
    strengthText: { type: Function, required: true },
    studioOpen: { type: Boolean, default: false },
    voiceLang: { type: String, default: "" },
    filePreview: { type: Object, default: null },
    previewCopied: { type: String, default: "" },
  },
  emits: [
    "close-zoom", "zoom-go", "retouch", "upscale",
    "update:upscaleFactor", "update:upscaleStrength",
    "voice-saved", "close-studio",
    "copy-preview", "download-out", "close-preview",
  ],
  setup() {
    return { ownImage, size };
  },
};
</script>

<template>
  <teleport to="body">
    <div class="lightbox" v-if="zoom" @click.self="$emit('close-zoom')" role="dialog" aria-label="Image agrandie">
      <img :src="zoom.data" :alt="zoom.name" @click="$emit('close-zoom')">
      <div class="lightbox-bar">
        <template v-if="zoomIndex >= 0 && assetsCount > 1">
          <button type="button" class="btn ghost" @click="$emit('zoom-go', -1)" title="Image précédente (←)" aria-label="Image précédente">‹</button>
          <span class="dim">{{ zoomIndex + 1 }} / {{ assetsCount }}</span>
          <button type="button" class="btn ghost" @click="$emit('zoom-go', 1)" title="Image suivante (→)" aria-label="Image suivante">›</button>
        </template>
        <span class="dim">{{ zoom.name }}</span>
        <a class="btn ghost" :href="zoom.data" :download="zoom.name">Télécharger</a>
        <button type="button" class="btn ghost" v-if="ownImage(zoom.data)"
                :disabled="streaming" @click="$emit('retouch', zoom)">Retoucher</button>
        <template v-if="upscaleOpt && ownImage(zoom.data)">
          <select :value="upscaleFactor" :disabled="streaming" aria-label="Facteur d'agrandissement"
                  @change="$emit('update:upscaleFactor', Number($event.target.value))">
            <option v-for="f in upscaleOpt.factors" :key="f" :value="f">× {{ f }}</option></select>
          <select v-if="upscaleOpt.strengths && upscaleOpt.strengths.length > 1" :value="upscaleStrength"
                  :disabled="streaming" aria-label="Force de l'agrandissement"
                  title="Plus bas : plus fidèle à l'image, moins de détail ajouté"
                  @change="$emit('update:upscaleStrength', Number($event.target.value))">
            <option v-for="s in upscaleOpt.strengths" :key="s.value" :value="s.value">{{ strengthText(s) }}</option></select>
          <button type="button" class="btn ghost" :disabled="streaming" @click="$emit('upscale', zoom)"
                  title="Le modèle d'agrandissement redessine l'image à une plus grande taille (détails un peu changés)">Agrandir</button>
        </template>
        <button type="button" class="btn" @click="$emit('close-zoom')">Fermer</button>
      </div>
    </div>
    <!-- Le studio d'une voix clonée (« ＋ nouvelle voix… » de la barre) : fermé
         par ✕ ou Échap seulement, un clic à côté ne perd pas l'enregistrement. -->
    <div class="lightbox voice-modal" v-if="studioOpen && current" role="dialog" aria-label="Nouvelle voix clonée">
      <voice-studio :clone-model="current.model" :language="voiceLang" :incognito="!!current.incognito"
                    @saved="$emit('voice-saved', $event)" @close="$emit('close-studio')"></voice-studio>
    </div>
    <div class="lightbox file-preview" v-if="filePreview" @click.self="$emit('close-preview')" role="dialog" :aria-label="filePreview.name">
      <div class="file-preview-box">
        <header class="artifact-head">
          <h3 :title="filePreview.name">{{ filePreview.name }}</h3>
          <span class="dim">{{ size(filePreview.f.size || filePreview.text.length) }}</span>
          <span class="artifact-actions">
            <button type="button" class="btn ghost art-btn" @click="$emit('copy-preview', { name: filePreview.name, content: filePreview.text })"
                    :title="previewCopied === filePreview.name ? 'Copié' : 'Copier'" aria-label="Copier">
              <svg v-if="previewCopied === filePreview.name" class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12l5 5 9-10"/></svg>
              <svg v-else class="ico" viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h8"/></svg></button>
            <button type="button" class="btn ghost art-btn" @click="$emit('download-out', filePreview.f)"
                    :title="'Télécharger ' + filePreview.name" aria-label="Télécharger">
              <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg></button>
            <button type="button" class="btn ghost art-btn" title="Fermer (Échap)" aria-label="Fermer" @click="$emit('close-preview')">
              <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
          </span>
        </header>
        <div class="file-preview-body md" v-if="filePreview.html" v-html="filePreview.html"></div>
        <pre class="file-preview-body artifact-source" v-else>{{ filePreview.shown }}</pre>
        <p class="linux-note" v-if="filePreview.cut">Aperçu coupé : {{ filePreview.cut }} caractères de plus, dans le fichier téléchargé.</p>
      </div>
    </div>
  </teleport>
</template>
