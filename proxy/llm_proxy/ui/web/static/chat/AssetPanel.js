/*
  Le panneau des IMAGES, à droite du fil (comme celui des artifacts) :
  les images GÉNÉRÉES dans la conversation (conversationImages) — celles
  jointes aux réponses (modèle d'images, graphique d'un script Linux ou
  PHP, capture, photo) et celles qu'un outil hébergé a rendues en lien
  markdown (/v1/files/…, image_generation). Pas les images que
  l'utilisateur a jointes à ses questions.

  Deux vues (le choix reste dans ce navigateur) : UNE image en grand,
  Précédent et Suivant (boutons, flèches ← → du clavier hors d'un champ),
  la planche des vignettes en dessous ; ou la GRILLE de toutes, à taille
  de case réglable (curseur, gardée aussi), où un clic ouvre l'image en
  grand.
  Elle se télécharge, se retouche (une image que la page tient : data URL
  ou /ui/assets), ou se retrouve dans le fil. Largeur, Large, Plein écran :
  SidePanel.js.
*/
import { SidePanel } from "./SidePanel.js";
import { ownImage } from "./history.js";

const { computed, watch, nextTick, ref } = Vue;

// Une image markdown : ![alt](url "titre"). `chart:` est un graphique
// interactif (une carte), pas un fichier.
const MD_IMAGE = /!\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)/g;

/** Les images générées de la conversation, dans l'ordre du fil, chacune
 * une fois : {key, name, data, msgId}. @param {any} conv */
export function conversationImages(conv) {
  const out = [];
  const seen = new Set();
  const add = (m, name, data) => {
    if (!data || seen.has(data)) return;
    seen.add(data);
    out.push({ key: data, name: name || "image", data, msgId: m.id });
  };
  for (const m of (conv && conv.messages) || []) {
    if (m.role !== "assistant") continue;
    for (const a of m.attachments || []) if (a.kind === "image") add(m, a.name, a.data);
    for (const [, alt, url] of String(m.content || "").matchAll(MD_IMAGE)) {
      if (url.startsWith("chart:")) continue;
      const file = url.split(/[?#]/)[0].split("/").pop();
      let name = file;
      try { name = decodeURIComponent(file); } catch { /* « % » mal formé : le nom tel quel */ }
      add(m, alt || name, url);
    }
  }
  return out;
}

// Une touche tapée dans un champ ne navigue pas.
const typing = (el) => !!el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));

export const AssetPanel = {
  components: { SidePanel },
  props: {
    images: { type: Array, default: () => [] },
    selected: { type: String, default: "" },
    busy: { type: Boolean, default: false },
  },
  emits: ["close", "select", "retouch", "locate"],
  setup(props, { emit }) {
    const index = computed(() => {
      const k = props.images.findIndex((a) => a.key === props.selected);
      return k >= 0 ? k : props.images.length - 1;
    });
    const img = computed(() => props.images[index.value] || null);
    const grid = VueUse.useLocalStorage("llm-proxy-asset-grid", false);
    const cell = VueUse.useLocalStorage("llm-proxy-asset-cell", 160);    // px, côté d'une case
    const open = (key) => { emit("select", key); grid.value = false; };
    const go = (step) => {
      const n = props.images.length;
      if (n > 1) emit("select", props.images[(index.value + step + n) % n].key);
    };
    VueUse.onKeyStroke(["ArrowLeft", "ArrowRight"], (e) => {
      if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey || typing(e.target)
          || document.querySelector(".lightbox")) return;
      e.preventDefault();
      go(e.key === "ArrowLeft" ? -1 : 1);
    });
    // La case de l'image choisie reste visible dans la grille, comme sa
    // vignette dans la planche.
    const gridEl = ref(null);
    // La vignette affichée reste visible dans la planche.
    const strip = ref(null);
    watch([index, grid], () => nextTick(() => {
      const box = grid.value ? gridEl.value : strip.value;
      const el = box && box.querySelector("[aria-current=true]");
      if (el) el.scrollIntoView({ block: "nearest", inline: "nearest" });
    }), { immediate: true });
    return { index, img, go, strip, gridEl, grid, cell, open, ownImage,
             pick: (key) => emit("select", key), close: () => emit("close") };
  },
  template: `
  <side-panel v-if="img" storage="llm-proxy-asset-width" @close="close">
    <template #head>
      <h3 v-if="grid">Images</h3>
      <h3 v-else :title="img.name">{{ img.name }}</h3>
      <span class="tag" title="Image affichée / images de la conversation">{{ index + 1 }} / {{ images.length }}</span>
    </template>
    <template #actions>
      <button type="button" class="btn ghost art-btn" :aria-pressed="String(grid)" @click="grid = !grid"
              :title="grid ? 'Une image en grand' : 'Grille : toutes les images'" :aria-label="grid ? 'Vue image' : 'Vue grille'">
        <svg v-if="grid" class="ico" viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="M21 16l-5-5-9 9"/></svg>
        <svg v-else class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z"/></svg></button>
      <template v-if="!grid">
      <button type="button" class="btn ghost art-btn" :disabled="images.length < 2" @click="go(-1)"
              title="Image précédente (←)" aria-label="Image précédente">
        <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M15 5l-7 7 7 7"/></svg></button>
      <button type="button" class="btn ghost art-btn" :disabled="images.length < 2" @click="go(1)"
              title="Image suivante (→)" aria-label="Image suivante">
        <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M9 5l7 7-7 7"/></svg></button>
      <button type="button" class="btn ghost art-btn" @click="$emit('locate', img)"
              title="La retrouver dans la conversation" aria-label="Voir dans la conversation">
        <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6"/><path d="M20 20l-4.5-4.5"/></svg></button>
      <a class="btn ghost art-btn" :href="img.data" :download="img.name" target="_blank" rel="noopener noreferrer"
         :title="'Télécharger ' + img.name" aria-label="Télécharger">
        <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg></a>
      <button type="button" class="btn ghost art-btn" v-if="ownImage(img.data)" :disabled="busy"
              @click="$emit('retouch', img)" title="Retoucher : la joindre au prochain message" aria-label="Retoucher">
        <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20l4-1 11-11-3-3L5 16l-1 4zM14 6l3 3"/></svg></button>
      </template>
    </template>
    <template v-if="grid">
      <label class="asset-zoom" title="Taille des cases de la grille">
        <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z"/></svg>
        <input type="range" min="72" max="480" step="8" v-model.number="cell" aria-label="Taille des cases"
               @dblclick="cell = 160">
        <span class="dim">{{ cell }} px</span>
      </label>
      <div class="asset-grid" ref="gridEl" role="list" aria-label="Images de la conversation"
           :style="{ '--cell': cell + 'px' }">
        <button type="button" v-for="(a, k) in images" :key="a.key" role="listitem" :aria-current="String(k === index)"
                :title="a.name + ' — ouvrir en grand'" @click="open(a.key)">
          <img :src="a.data" :alt="a.name" loading="lazy"></button>
      </div>
    </template>
    <template v-else>
    <div class="asset-view">
      <button type="button" class="asset-nav prev" v-if="images.length > 1" @click="go(-1)" aria-label="Image précédente">
        <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M15 5l-7 7 7 7"/></svg></button>
      <img :src="img.data" :alt="img.name" :key="img.key">
      <button type="button" class="asset-nav next" v-if="images.length > 1" @click="go(1)" aria-label="Image suivante">
        <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M9 5l7 7-7 7"/></svg></button>
    </div>
    <div class="asset-strip" ref="strip" v-if="images.length > 1" role="list" aria-label="Images de la conversation">
      <button type="button" v-for="(a, k) in images" :key="a.key" role="listitem" :aria-current="String(k === index)"
              :title="a.name" @click="pick(a.key)"><img :src="a.data" :alt="a.name" loading="lazy"></button>
    </div>
    </template>
  </side-panel>`,
};
