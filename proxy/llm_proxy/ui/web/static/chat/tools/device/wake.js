// @ts-check
// Écran allumé : l'écran ne se met pas en veille (une recette, un
// pas-à-pas), tant que la page est visible ; repris à son retour
// (useWakeLock). L'appel laisse une CARTE DURABLE : l'état de l'écran, et
// de quoi le basculer — après un rechargement aussi.
import { TOOL, FAMILY, PageTool, defineFamily, failure } from "../contract.js";
import { wakeLock } from "./vueuse.js";

/** @typedef {import("../contract.js").Kept} Kept */

class KeepScreenOn extends PageTool {
  constructor() {
    super({
      name: TOOL.KEEP_SCREEN_ON,
      description: "Empêche l'écran de l'utilisateur de se mettre en veille (recette, pas-à-pas, "
        + "présentation), ou le rend à la normale. Tant que la page est visible ; une carte sous "
        + "la réponse permet à l'utilisateur de basculer lui-même.",
      params: { on: { type: "boolean", description: "true : écran allumé ; false : veille normale." } },
      required: ["on"],
    });
  }

  /** @override @param {any} a */
  summary(a) { return a.on ? "allumé" : "veille normale"; }

  /** @override @param {any} args */
  async run(args) {
    if (!args.on) {
      await wakeLock.release();
      return { result: "Écran rendu à la veille normale.", keep: {} };
    }
    try {
      await wakeLock.request("screen");
    } catch (e) {
      return failure("refusé par le navigateur : " + e.message);
    }
    return { result: "Écran maintenu allumé tant que la page est visible.", keep: {} };
  }

  /** @override */
  keptView() {
    return wakeLock.isActive.value
      ? { title: "Écran maintenu allumé", detail: "tant que la page est visible",
          actions: [{ id: "off", label: "Rendre à la veille" }] }
      : { title: "Écran en veille normale",
          actions: [{ id: "on", label: "Garder l'écran allumé", primary: true }] };
  }

  /** @override @param {Kept} k @param {string} action */
  async keptAct(k, action) {
    if (action === "off") await wakeLock.release();
    else await wakeLock.request("screen");
    return null;
  }
}

export const FAMILIES = [defineFamily({
  key: FAMILY.WAKE, label: "Écran allumé", hint: "empêcher la mise en veille",
  available: () => wakeLock.isSupported.value,
  tools: [new KeepScreenOn()],
})];
