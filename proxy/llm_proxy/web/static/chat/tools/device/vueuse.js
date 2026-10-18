// @ts-check
/*
  VueUse (vueuse.iife.min.js, chargé après Vue) pour les outils de
  l'appareil : ses composables, créés UNE fois, dans une portée détachée
  — ils vivent autant que la page, hors de tout composant. Chacun dit
  s'il est pris en charge (`isSupported`), ce que lisent les familles.
*/
const scope = Vue.effectScope(true);

/** Un composable créé dans la portée des outils.
 * @template T @param {() => T} make @returns {T} */
export const once = (make) => scope.run(make);

export const clipboard = once(() => VueUse.useClipboard({ legacy: true }));
export const sharer = once(() => VueUse.useShare());
export const eyeDropper = once(() => VueUse.useEyeDropper());
// Repris tout seul quand la page redevient visible.
export const wakeLock = once(() => VueUse.useWakeLock());
