// @ts-check
// Lecture à voix haute : le navigateur lit un texte (synthèse vocale,
// useSpeechSynthesis) — un pas-à-pas mains libres, une réponse à écouter.
// L'appel lit tout de suite (l'utilisateur vient d'écrire : la page a eu
// son geste) et laisse une CARTE DURABLE, pour relire ou arrêter.
import { TOOL, FAMILY, PageTool, defineFamily, failure } from "../contract.js";
import { speech, speechText, speechLang } from "./vueuse.js";

/** @typedef {import("../contract.js").Kept} Kept */

const MAX_TEXT = 4000;
// La carte dont le texte est en cours de lecture.
const reading = Vue.ref("");

/** @param {string} id @param {string} text @param {string} lang */
function say(id, text, lang) {
  speechText.value = text;
  speechLang.value = lang;
  reading.value = id;
  speech.speak();
}

class Speak extends PageTool {
  constructor() {
    super({
      name: TOOL.SPEAK,
      description: "Lit un texte à voix haute sur l'appareil de l'utilisateur (synthèse vocale du "
        + "navigateur) : une étape d'un pas-à-pas mains libres, un résumé à écouter. Un texte "
        + "court et oral, sans Markdown ; une carte sous la réponse permet de le relire.",
      params: {
        text: { type: "string", description: `Le texte à lire (${MAX_TEXT} caractères au plus).` },
        lang: { type: "string", description: "La langue, au format BCP 47 (« fr-FR », « en-US ») ; défaut : celle du navigateur." },
      },
      required: ["text"],
    });
  }

  /** @override @param {any} a */
  summary(a) { return (a.text || "").slice(0, 80); }

  /** @override @param {any} args */
  run(args) {
    const text = String(args.text || "").replace(/[*_#`>]/g, "").trim().slice(0, MAX_TEXT);
    if (!text) return failure("`text` attendu.");
    const lang = String(args.lang || navigator.language || "fr-FR");
    const ref = crypto.randomUUID();
    say(ref, text, lang);
    return { result: `Lu à voix haute (${text.length} caractères) ; une carte sous la réponse permet de le relire.`,
             keep: { ref, text, lang } };
  }

  /** @override @param {Kept} k */
  keptView(k) {
    const playing = speech.isPlaying.value && reading.value === k.ref;
    return { title: playing ? "Lecture en cours…" : "Lu à voix haute", quote: k.text,
             actions: playing ? [{ id: "stop", label: "Arrêter" }]
               : [{ id: "say", label: "Relire", primary: true }] };
  }

  /** @override @param {Kept} k @param {string} action */
  keptAct(k, action) {
    if (action === "stop") speech.stop();
    else say(k.ref, k.text, k.lang);
    return null;
  }
}

export const FAMILIES = [defineFamily({
  key: FAMILY.SPEAK, label: "Lecture à voix haute", hint: "le navigateur lit un texte (synthèse vocale)",
  available: () => speech.isSupported.value,
  tools: [new Speak()],
})];
