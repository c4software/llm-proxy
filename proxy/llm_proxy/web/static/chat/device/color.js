// Pipette : une couleur prise n'importe où à l'écran, sur un clic de la
// carte (l'API EyeDropper ne s'ouvre que sur un geste).
import { fn, refused, REFUSE } from "./common.js";

export const FAMILIES = [{
  key: "color", label: "Pipette", hint: "une couleur prise à l'écran",
  available: () => "EyeDropper" in window,
  tools: [fn("pick_color", "Demande à l'utilisateur de prendre une couleur n'importe où à son "
    + "écran (pipette) ; rend son code hexadécimal.",
  { reason: { type: "string", description: "Pourquoi, en une phrase (facultatif)." } })],
}];

export const waits = () => true;
export const prepare = (name, args) => ({ reason: String(args.reason || "").trim().slice(0, 300) });
export const view = (w) => ({
  title: "Le modèle demande une couleur", detail: w.reason,
  actions: [{ id: "pick", label: "Prendre une couleur", primary: true }, REFUSE],
});

export async function act(w, action) {
  if (action === "refuse") return refused("de prendre une couleur");
  try {
    const { sRGBHex } = await new window.EyeDropper().open();
    return { result: `Couleur prise : ${sRGBHex}` };
  } catch (e) {
    return e.name === "AbortError" ? null : { result: "Error: pipette indisponible : " + e.message };
  }
}
