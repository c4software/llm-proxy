<script>
// @ts-check
// Panneau « Réglages » d'une conversation : prompt système, aperçu du prompt
// invisible, température, affichage du raisonnement, voix, notifications et
// export Markdown.
import VoiceSettings from "./voice/VoiceSettings.vue";
import { kTokens } from "./compact.js";

export default {
  components: { VoiceSettings },
  props: {
    current: { type: Object, required: true },
    promptOpen: { type: Boolean, default: false },
    hiddenPrompt: { type: Object, default: null },
    notifySupported: { type: Boolean, default: false },
    notifyOn: { type: Boolean, default: false },
  },
  emits: [
    "update:promptOpen", "update:system", "update:temperature",
    "update:showReasoning", "toggle-notify", "export",
  ],
  setup() {
    return { kTokens };
  },
};
</script>

<template>
  <div class="chat-settings">
    <label class="field wide">
      <span>Prompt système</span>
      <textarea :value="current.system" rows="3" spellcheck="false"
                @input="$emit('update:system', /** @type {HTMLTextAreaElement} */ ($event.target).value)"
                placeholder="Instructions données au modèle avant la conversation (facultatif)."></textarea>
    </label>
    <div class="field wide hidden-prompt">
      <label class="check"><input type="checkbox" :checked="promptOpen"
                                  @change="$emit('update:promptOpen', /** @type {HTMLInputElement} */ ($event.target).checked)">
        Afficher le prompt invisible <span class="dim">— ce que le prochain message enverra en plus
        du fil{{ hiddenPrompt ? " : ≈ " + kTokens(hiddenPrompt.total) + " tokens" : "" }}</span></label>
      <div class="hidden-body" v-if="hiddenPrompt">
        <p class="dim">Prompt système envoyé · ≈ {{ kTokens(hiddenPrompt.systemTokens) }} tokens
          (dont consignes des outils ≈ {{ kTokens(hiddenPrompt.guidanceTokens) }})</p>
        <pre>{{ hiddenPrompt.system || "(aucun)" }}</pre>
        <p class="dim">Outils déclarés ({{ hiddenPrompt.specs.length }}) · ≈ {{ kTokens(hiddenPrompt.toolTokens) }} tokens</p>
        <details v-for="d in hiddenPrompt.specs" :key="d.from + d.name">
          <summary><code>{{ d.name }}</code> <span class="dim">{{ d.from }} · ≈ {{ kTokens(d.tokens) }} tokens</span></summary>
          <pre>{{ d.json }}</pre>
        </details>
      </div>
    </div>
    <div class="field">
      <span>Température</span>
      <div class="temp">
        <label class="check"><input type="checkbox" :checked="current.temperature === null"
          @change="$emit('update:temperature', /** @type {HTMLInputElement} */ ($event.target).checked ? null : 0.7)"> celle du modèle</label>
        <template v-if="current.temperature !== null">
          <input type="range" min="0" max="2" step="0.05" :value="current.temperature"
                 @input="$emit('update:temperature', Number(/** @type {HTMLInputElement} */ ($event.target).value))">
          <b>{{ Number(current.temperature).toFixed(2) }}</b>
        </template>
      </div>
    </div>
    <div class="field">
      <span>Affichage</span>
      <label class="check"><input type="checkbox" :checked="current.showReasoning"
                                  @change="$emit('update:showReasoning', /** @type {HTMLInputElement} */ ($event.target).checked)">
        montrer le raisonnement du modèle</label>
    </div>
    <voice-settings :incognito="!!current.incognito"></voice-settings>
    <div class="field" v-if="notifySupported">
      <span>Notifications</span>
      <label class="check"><input type="checkbox" :checked="notifyOn" @change="$emit('toggle-notify', $event)">
        prévenir quand une réponse se termine (onglet en arrière-plan)</label>
    </div>
    <div class="field">
      <span>Conversation</span>
      <div><button type="button" class="btn ghost" :disabled="!current.messages.length"
                   @click="$emit('export', current)">Exporter en Markdown</button></div>
    </div>
  </div>
</template>
