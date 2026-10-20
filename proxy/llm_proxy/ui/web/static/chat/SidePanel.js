/*
  Le PANNEAU LATÉRAL commun, à droite du fil : celui des artifacts
  (ArtifactPanel) et celui du terminal (LinuxPanel) — la même apparence,
  le même code. Il porte :

    la largeur      tirée par la poignée du bord gauche (souris ou clavier),
                    gardée dans ce navigateur (`storage`) ; double-clic :
                    celle par défaut ; « Large » la remplace tant qu'il est actif
    l'en-tête       slot `head` (titre, choix) ; slot `actions` (les boutons
                    du panneau), puis Réseau, slot `actions-end`, Large,
                    Pleine page, Plein écran (`fullscreen`), Fermer ; écran
                    étroit : Pleine page seule, sans Large ni Plein écran
    le réseau       `network` (true, false ; null : pas de bouton) : couper
                    d'un clic, ouvrir après l'avertissement (slot
                    `network-warning`) ; émet `network` (vrai ou faux).
                    L'avertissement se referme quand `subject` change
    le corps        slot par défaut, avec `dragging` (pendant le glissement,
                    un cadre ne doit pas prendre la souris)
*/
const { ref, computed, watch } = Vue;

const MIN = 320;                     // px, du panneau comme du fil
const NARROW = "(max-width: 1100px)"; // le panneau passe par-dessus le fil (dashboard.css)

export const SidePanel = {
  props: {
    storage: { type: String, required: true },
    fullscreen: { type: Boolean, default: true },
    network: { type: Boolean, default: null },
    networkTitles: { type: Array, default: () => ["Réseau ouvert : couper", "Réseau bloqué : l’ouvrir"] },
    subject: { type: String, default: "" },
  },
  emits: ["close", "network"],
  setup(props, { emit }) {
    const panel = ref(null);
    const wide = ref(false);
    const width = VueUse.useLocalStorage(props.storage, 0);
    const dragging = ref(false);
    function clampWidth(w) {
      const host = panel.value && panel.value.parentElement;
      const max = host ? host.getBoundingClientRect().width - MIN : window.innerWidth - MIN;
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
    // Plein écran : le panneau entier, son en-tête compris (de quoi en sortir).
    const { isSupported, isFullscreen: full, toggle: toggleFull } = VueUse.useFullscreen(panel);
    const canFull = computed(() => props.fullscreen && isSupported.value);
    // « Pleine page » étend le panneau à toute la zone du chat, le fil
    // caché (en CSS, le terminal compris), la barre du haut gardée ; seul
    // ce bouton l'ouvre et la referme. Écran étroit : « Large » n'y change
    // rien, et le plein écran d'un élément manque sur iPhone — elle seule.
    const narrow = VueUse.useMediaQuery(NARROW);
    const page = ref(false);
    const sized = computed(() => !!width.value && !wide.value && !full.value);
    const panelStyle = computed(() => (sized.value && !page.value ? { width: width.value + "px" } : {}));

    // Le réseau : coupé d'un clic, ouvert après l'avertissement.
    const asking = ref(false);
    watch(() => props.subject, () => { asking.value = false; });
    function toggleNetwork() {
      if (props.network) { emit("network", false); return; }
      asking.value = !asking.value;
    }
    function allowNetwork() {
      asking.value = false;
      emit("network", true);
    }
    return { panel, wide, width, dragging, sized, panelStyle, startResize, keyResize,
             resetWidth: () => { width.value = 0; }, canFull, full, toggleFull, narrow, page,
             asking, toggleNetwork, allowNetwork };
  },
  template: `
  <section class="artifact-side" :class="{ wide, sized, dragging, page }" ref="panel" :style="panelStyle">
    <div class="artifact-resize" role="separator" aria-orientation="vertical" tabindex="0"
         aria-label="Largeur du panneau" title="Tirer pour redimensionner ; double-clic : largeur par défaut"
         :aria-valuenow="width || undefined" @pointerdown="startResize" @dblclick="resetWidth" @keydown="keyResize"></div>
    <header class="artifact-head">
      <slot name="head"></slot>
      <span class="artifact-actions">
        <slot name="actions"></slot>
        <button type="button" class="btn ghost art-btn net" v-if="network !== null" :aria-pressed="String(network)"
                @click="toggleNetwork" :title="network ? networkTitles[0] : networkTitles[1]" aria-label="Réseau">
          <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c3 3.5 3 14.5 0 18M12 3c-3 3.5-3 14.5 0 18"/></svg></button>
        <slot name="actions-end"></slot>
        <button type="button" class="btn ghost art-btn" v-if="!narrow && !page" :aria-pressed="String(wide)" @click="wide = !wide"
                :title="wide ? 'Réduire le panneau' : 'Élargir le panneau'" aria-label="Large">
          <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12h18M7 8l-4 4 4 4M17 8l4 4-4 4"/></svg></button>
        <button type="button" class="btn ghost art-btn" :aria-pressed="String(page)" @click="page = !page"
                :title="page ? 'Quitter la pleine page : revoir la conversation' : 'Pleine page : le panneau à la place de la conversation'" aria-label="Pleine page">
          <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><path v-if="!page" d="M14 8h3v3M17 8l-4 4M10 16H7v-3M7 16l4-4"/><path v-else d="M13 9v3h3M13 12l4-4M11 15v-3H8M11 12l-4 4"/></svg></button>
        <button type="button" class="btn ghost art-btn" v-if="canFull && !narrow" :aria-pressed="String(full)" @click="toggleFull()"
                :title="full ? 'Quitter le plein écran (Échap)' : 'Plein écran'" aria-label="Plein écran">
          <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path v-if="!full" d="M3 3h6M3 3v6M21 3h-6M21 3v6M3 21h6M3 21v-6M21 21h-6M21 21v-6"/><path v-else d="M9 3v6H3M15 3v6h6M9 21v-6H3M15 21v-6h6"/></svg></button>
        <button type="button" class="btn ghost art-btn" title="Fermer" aria-label="Fermer" @click="$emit('close')">
          <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
      </span>
    </header>
    <div class="artifact-warn" v-if="asking" role="alertdialog" aria-label="Ouvrir le réseau">
      <p><slot name="network-warning"></slot></p>
      <div class="card-actions">
        <button type="button" class="btn" @click="allowNetwork">Autoriser</button>
        <button type="button" class="btn ghost" @click="asking = false">Annuler</button>
      </div>
    </div>
    <slot :dragging="dragging"></slot>
  </section>`,
};
