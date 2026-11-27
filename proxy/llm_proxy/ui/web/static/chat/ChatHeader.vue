<script>
// En-tête du chat : titre, sélecteur de modèle, rechargement du catalogue et
// boutons d'ouverture des panneaux (Incognito, Artifacts, Images, Terminal,
// Mémoire, Réglages).
export default {
  props: {
    current: { type: Object, required: true },
    models: { type: Array, required: true },
    modelGroups: { type: Array, required: true },
    modelsLoading: { type: Boolean, default: false },
    modelsTitle: { type: String, default: "" },
    streaming: { type: Boolean, default: false },
    artifactsOpen: { type: Boolean, default: false },
    assetsCount: { type: Number, default: 0 },
    assetsOpen: { type: Boolean, default: false },
    linuxOffered: { type: Boolean, default: false },
    linuxOpen: { type: Boolean, default: false },
    memoryEnabled: { type: Boolean, default: false },
    memoryOpen: { type: Boolean, default: false },
    settingsOpen: { type: Boolean, default: false },
  },
  emits: [
    "reload-models", "toggle-incognito", "toggle-artifacts", "toggle-assets",
    "toggle-linux", "toggle-memory", "toggle-settings",
  ],
};
</script>

<template>
  <header class="chat-head">
    <h2 :title="current.title">{{ current.title }}</h2>
    <label class="model-pick" title="Modèle">
      <select v-model="current.model" :disabled="streaming" :title="current.model">
        <option v-if="!models.length && current.model" :value="current.model">{{ modelsLoading ? "Chargement des modèles…" : current.model }}</option>
        <option v-if="!models.length && !current.model" value="" disabled>{{ modelsLoading ? "Chargement des modèles…" : "Aucun modèle" }}</option>
        <option v-if="current.model && models.length && !models.some((m) => m.id === current.model)"
                :value="current.model">{{ current.model }} ({{ modelsLoading ? "chargement…" : "absent du catalogue" }})</option>
        <optgroup v-for="g in modelGroups" :key="g.label" :label="g.label">
          <option v-for="m in g.models" :key="m.id" :value="m.id">{{ m.text }}</option>
        </optgroup>
      </select>
    </label>
    <button type="button" class="btn ghost reload-models" :class="{ busy: modelsLoading }"
            :disabled="modelsLoading" :title="modelsTitle" :aria-label="modelsTitle" @click="$emit('reload-models')">
      <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 12a8 8 0 1 1-2.3-5.7M20 4v5h-5"/></svg></button>
    <button type="button" class="btn ghost ico-btn incognito-btn" :disabled="streaming"
            :aria-pressed="String(!!current.incognito)" @click="$emit('toggle-incognito')"
            :title="current.incognito ? 'Quitter le mode incognito' : 'Conversation incognito : rien n’est gardé'">
      <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M2 11h20M5 11l2.5-6h9l2.5 6M10 16.5c1.3-.8 2.7-.8 4 0"/><circle cx="7" cy="16.5" r="3"/><circle cx="17" cy="16.5" r="3"/></svg>
      <span class="lbl">Incognito</span></button>
    <button type="button" class="btn ghost ico-btn" v-if="(current.artifacts || []).length"
            :aria-pressed="String(artifactsOpen)" @click="$emit('toggle-artifacts')"
            title="Les artifacts de la conversation (pages, documents, schémas)">
      <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 4h16v16H4zM4 9h16M9 9v11"/></svg>
      <span class="lbl">Artifacts ({{ current.artifacts.length }})</span></button>
    <button type="button" class="btn ghost ico-btn" v-if="assetsCount"
            :aria-pressed="String(assetsOpen)" @click="$emit('toggle-assets')"
            title="Les images générées dans la conversation">
      <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="M21 16l-5-5-9 9"/></svg>
      <span class="lbl">Images ({{ assetsCount }})</span></button>
    <button type="button" class="btn ghost ico-btn" v-if="linuxOffered"
            :aria-pressed="String(linuxOpen)" @click="$emit('toggle-linux')"
            title="Terminal : la VM Linux de la conversation, celle où le modèle exécute ses scripts">
      <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 5h18v14H3zM7 10l3 2-3 2M12 15h5"/></svg>
      <span class="lbl">Terminal</span></button>
    <button type="button" class="btn ghost ico-btn" v-if="memoryEnabled"
            :aria-pressed="String(memoryOpen)" @click="$emit('toggle-memory')"
            title="Mémoire : ce que les modèles ont retenu">
      <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 3h9l3 3v15H6zM9 9h6M9 13h6M9 17h4"/></svg>
      <span class="lbl">Mémoire</span></button>
    <button type="button" class="btn ghost ico-btn" :aria-pressed="String(settingsOpen)"
            @click="$emit('toggle-settings')" title="Réglages">
      <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h10M18 7h2M4 17h4M12 17h8M16 5v4M10 15v4"/></svg>
      <span class="lbl">Réglages</span></button>
  </header>
</template>
