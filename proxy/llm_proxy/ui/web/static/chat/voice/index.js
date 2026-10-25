// @ts-check
/*
  La VOIX du chat : ÉCOUTER une réponse (et l'outil `speak`,
  tools/device/speak.js), par l'un de deux moteurs au choix :

    browser.js    la synthèse du NAVIGATEUR : tout de suite, sans modèle
    proxy.js      un MODÈLE DE SYNTHÈSE du proxy (Qwen3-TTS) : ses voix
                  toutes faites, ou une voix CLONÉE de ce navigateur
                  (clones.js), créée dans VoiceStudio.js — enregistrée au
                  micro, d'un fichier, ou conçue d'après une description,
                  normalisée dans la page (audio.js)

  Une lecture à la fois (state.js) : en lancer une autre arrête la
  précédente. Les réglages (`prefs`) restent dans ce navigateur.
*/
import * as browser from "./browser.js";
import * as proxy from "./proxy.js";
import * as clones from "./clones.js";
import { state } from "./state.js";

export { state };
export { speakable } from "./text.js";

const KEY = "llm-proxy-chat-voice";

/** @typedef {{engine: string, voice: string, rate: number, proxyVoice: string,
 *   tone: string, language: string, cloneModel: string}} Prefs */

/** @type {Prefs} */
const DEFAULTS = { engine: "browser", voice: "", rate: 1, proxyVoice: "", tone: "", language: "", cloneModel: "" };

/** Les réglages de la voix, partagés par le chat et l'outil `speak` :
 * gardés par useLocalStorage (les clés manquantes reprennent leur défaut),
 * suivis d'un onglet à l'autre. @type {Prefs} */
export const prefs = VueUse.toReactive(VueUse.useLocalStorage(KEY, DEFAULTS, { mergeDefaults: true }));

// `proxyVoice` : « preset:<modèle>␀<voix> » ou « clone:<id> ».
const SEP = "\u0000";
/** @param {string} model @param {string} name */
export const presetKey = (model, name) => `preset:${model}${SEP}${name}`;
/** @param {string} id */
export const cloneKey = (id) => `clone:${id}`;

/** Le modèle de clonage choisi, sinon le premier. */
export const cloneModel = Vue.computed(() => {
  const all = proxy.models.value.clone;
  return all.includes(prefs.cloneModel) ? prefs.cloneModel : all[0] || "";
});

/** La voix du proxy choisie, si elle est encore utilisable.
 * @returns {import("./proxy.js").Voice | null} */
function proxyVoice() {
  const v = prefs.proxyVoice;
  if (v.startsWith("preset:")) {
    const [model, name] = v.slice(7).split(SEP);
    return proxy.models.value.custom.includes(model) ? { kind: "preset", model, name } : null;
  }
  if (v.startsWith("clone:") && cloneModel.value) return { kind: "clone", model: cloneModel.value, id: v.slice(6) };
  return null;
}

/** Le moteur qui lira : celui choisi, s'il est prêt. */
export const engine = Vue.computed(() => (prefs.engine === "proxy" && proxy.available.value && proxyVoice()
  ? "proxy" : browser.supported ? "browser" : ""));
export const supported = Vue.computed(() => !!engine.value);

/** Le nom de la voix qui lira, pour les infobulles. */
export const label = Vue.computed(() => {
  if (engine.value !== "proxy") return browser.voiceName(prefs.voice);
  const v = /** @type {any} */ (proxyVoice());
  if (v.kind === "preset") return v.name;
  const c = clones.clones.value.find((x) => x.id === v.id);
  return (c ? c.name : "voix clonée") + " (clonée)";
});

/** Lit `text` ; `id` désigne cette lecture (state.id), arrêtée par stop()
 * ou par une autre lecture. La promesse échoue avec la lecture.
 * @param {string} id @param {string} text
 * @param {{lang?: string, incognito?: boolean}} [opts] */
export async function speak(id, text, opts = {}) {
  if (engine.value === "proxy") {
    browser.stop();
    await proxy.speak(id, text, {
      voice: /** @type {any} */ (proxyVoice()), rate: prefs.rate, incognito: opts.incognito,
      tone: prefs.proxyVoice.startsWith("preset:") ? prefs.tone : "",
      language: prefs.language || proxy.language(opts.lang),
    });
    return;
  }
  proxy.stop();
  Object.assign(state, { error: "", errorId: "" });
  browser.speak(id, text, { voice: prefs.voice, rate: prefs.rate, lang: opts.lang });
}

export function stop() {
  state.id = "";
  browser.stop();
  proxy.stop();
}

// Le nom d'une voix clonée choisie, pour `label`, dès l'ouverture.
if (String(prefs.proxyVoice).startsWith("clone:")) clones.load();
