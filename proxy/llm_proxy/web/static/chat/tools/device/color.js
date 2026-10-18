// @ts-check
// Pipette : une couleur prise n'importe où à l'écran, sur un clic de la
// carte (l'API EyeDropper ne s'ouvre que sur un geste). La couleur prise
// reste en CARTE DURABLE : sa pastille, son code à copier, et de quoi en
// prendre une autre.
import { TOOL, FAMILY, PageTool, defineFamily, refused, REFUSE, reasonOf, failure } from "../contract.js";
import { clipboard, eyeDropper } from "./vueuse.js";

/** @typedef {import("../contract.js").Card} Card */
/** @typedef {import("../contract.js").Kept} Kept */

/** La pipette → le code hexadécimal, ou null si l'utilisateur l'annule. */
async function pick() {
  try {
    const got = await eyeDropper.open();
    return got ? got.sRGBHex : null;
  } catch (e) {
    if (e.name === "AbortError") return null;
    throw e;
  }
}

class PickColor extends PageTool {
  constructor() {
    super({
      name: TOOL.PICK_COLOR,
      description: "Demande à l'utilisateur de prendre une couleur n'importe où à son écran "
        + "(pipette) ; rend son code hexadécimal.",
      params: { reason: { type: "string", description: "Pourquoi, en une phrase (facultatif)." } },
    });
  }

  /** @override */
  waits() { return true; }
  /** @override @param {any} args */
  prepare(args) { return reasonOf(args); }
  /** @override @param {any} a */
  summary(a) { return a.reason || ""; }

  /** @override @param {Card} w */
  view(w) {
    return { title: "Le modèle demande une couleur", detail: w.reason,
             actions: [{ id: "pick", label: "Prendre une couleur", primary: true }, REFUSE] };
  }

  /** @override @param {Card} w @param {string} action */
  async act(w, action) {
    if (action === "refuse") return refused("de prendre une couleur");
    try {
      const color = await pick();
      return color ? { result: `Couleur prise : ${color}`, keep: { color } } : null;
    } catch (e) {
      return { result: failure("pipette indisponible : " + e.message) };
    }
  }

  /** @override @param {Kept} k */
  keptView(k) {
    return { title: "Couleur prise", swatch: k.color, code: k.color,
             actions: [{ id: "copy", label: "Copier", primary: true }, { id: "pick", label: "Prendre une autre couleur" }] };
  }

  /** @override @param {Kept} k @param {string} action */
  async keptAct(k, action) {
    if (action === "pick") {
      if (!eyeDropper.isSupported.value) return "Pas de pipette dans ce navigateur.";
      const color = await pick();
      if (color) k.color = color;
      return null;
    }
    await clipboard.copy(k.color);
    return "Copié.";
  }
}

export const FAMILIES = [defineFamily({
  key: FAMILY.COLOR, label: "Pipette", hint: "une couleur prise à l'écran",
  available: () => eyeDropper.isSupported.value,
  tools: [new PickColor()],
})];
