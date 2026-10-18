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

// Le contexte de l'appareil (device_context).
export const network = once(() => VueUse.useNetwork());
export const battery = once(() => VueUse.useBattery());
export const prefersDark = once(() => VueUse.usePreferredDark());
export const reducedMotion = once(() => VueUse.usePreferredReducedMotion());
export const windowSize = once(() => VueUse.useWindowSize());

// La synthèse vocale (speak) : le texte et la langue lus, à changer avant
// de parler.
export const speechText = Vue.ref("");
export const speechLang = Vue.ref(navigator.language || "fr-FR");
export const speech = once(() => VueUse.useSpeechSynthesis(speechText, { lang: speechLang }));
