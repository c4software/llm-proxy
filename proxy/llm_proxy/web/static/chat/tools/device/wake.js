// @ts-check
// Écran allumé : l'écran ne se met pas en veille (une recette, un
// pas-à-pas), tant que la page est visible ; repris à son retour.
import { TOOL, FAMILY, PageTool, defineFamily, failure } from "../contract.js";

let lock = null;
let wanted = false;

async function take() {
  lock = await navigator.wakeLock.request("screen");
  lock.addEventListener("release", () => { lock = null; });
}

document.addEventListener("visibilitychange", () => {
  if (wanted && !lock && !document.hidden) take().catch(() => {});
});

class KeepScreenOn extends PageTool {
  constructor() {
    super({
      name: TOOL.KEEP_SCREEN_ON,
      description: "Empêche l'écran de l'utilisateur de se mettre en veille (recette, pas-à-pas, "
        + "présentation), ou le rend à la normale. Tant que la page est visible.",
      params: { on: { type: "boolean", description: "true : écran allumé ; false : veille normale." } },
      required: ["on"],
    });
  }

  /** @override @param {any} a */
  summary(a) { return a.on ? "allumé" : "veille normale"; }

  /** @override @param {any} args */
  async run(args) {
    wanted = !!args.on;
    if (!wanted) {
      if (lock) await lock.release();
      return "Écran rendu à la veille normale.";
    }
    try {
      if (!lock) await take();
      return "Écran maintenu allumé tant que la page est visible.";
    } catch (e) {
      wanted = false;
      return failure("refusé par le navigateur : " + e.message);
    }
  }
}

export const FAMILIES = [defineFamily({
  key: FAMILY.WAKE, label: "Écran allumé", hint: "empêcher la mise en veille",
  available: () => "wakeLock" in navigator,
  tools: [new KeepScreenOn()],
})];
