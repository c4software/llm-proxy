// Écran allumé : l'écran ne se met pas en veille (une recette, un
// pas-à-pas), tant que la page est visible ; repris à son retour.
import { fn } from "./common.js";

let lock = null;
let wanted = false;

export const FAMILIES = [{
  key: "wake", label: "Écran allumé", hint: "empêcher la mise en veille",
  available: () => "wakeLock" in navigator,
  tools: [fn("keep_screen_on", "Empêche l'écran de l'utilisateur de se mettre en veille "
    + "(recette, pas-à-pas, présentation), ou le rend à la normale. Tant que la page est visible.",
  { on: { type: "boolean", description: "true : écran allumé ; false : veille normale." } }, ["on"])],
}];

export const waits = () => false;

async function take() {
  lock = await navigator.wakeLock.request("screen");
  lock.addEventListener("release", () => { lock = null; });
}

document.addEventListener("visibilitychange", () => {
  if (wanted && !lock && !document.hidden) take().catch(() => {});
});

export async function run(name, args) {
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
    return "Error: refusé par le navigateur : " + e.message;
  }
}
