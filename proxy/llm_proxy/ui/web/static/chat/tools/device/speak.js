// @ts-check
// Lecture à voix haute : la page lit un texte (chat/voice/ : le moteur, la
// voix et le débit choisis dans les réglages du chat — navigateur, ou
// modèle de synthèse du proxy) — un pas-à-pas mains libres, un résumé à
// écouter.
// L'appel lit tout de suite (l'utilisateur vient d'écrire : la page a eu
// son geste) et laisse une CARTE DURABLE, pour relire ou arrêter.
import { TOOL, FAMILY, PageTool, defineFamily, failure } from "../contract.js";
import * as voice from "../../voice/index.js";

/** @typedef {import("../contract.js").Kept} Kept */

const MAX_TEXT = 4000;

/** @param {string} ref @param {string} text @param {string} lang */
function say(ref, text, lang) {
  // Un échec se lit sur la carte (voice.state.error).
  voice.speak("tool:" + ref, text, { lang }).catch(() => {});
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
    const playing = voice.state.id === "tool:" + k.ref;
    const failed = voice.state.errorId === "tool:" + k.ref && voice.state.error;
    return { title: playing ? (voice.state.loading ? "Synthèse…" : "Lecture en cours…")
               : failed ? "Lecture impossible : " + failed : "Lu à voix haute", quote: k.text,
             actions: playing ? [{ id: "stop", label: "Arrêter" }]
               : [{ id: "say", label: "Relire", primary: true }] };
  }

  /** @override @param {Kept} k @param {string} action */
  keptAct(k, action) {
    if (action === "stop") voice.stop();
    else say(k.ref, k.text, k.lang);
    return null;
  }
}

export const FAMILIES = [defineFamily({
  key: FAMILY.SPEAK, label: "Lecture à voix haute", hint: "le navigateur lit un texte (synthèse vocale)",
  available: () => voice.supported.value,
  tools: [new Speak()],
})];
