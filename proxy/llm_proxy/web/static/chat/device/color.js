// Pipette : une couleur prise n'importe où à l'écran, sur un clic de la
// carte (l'API EyeDropper ne s'ouvre que sur un geste).
import { TOOL, FAMILY, defineTool, defineFamily, refused, REFUSE, reasonOf, failure } from "./contract.js";

const pickColor = defineTool({
  name: TOOL.PICK_COLOR,
  description: "Demande à l'utilisateur de prendre une couleur n'importe où à son écran "
    + "(pipette) ; rend son code hexadécimal.",
  params: { reason: { type: "string", description: "Pourquoi, en une phrase (facultatif)." } },
  waits: true,
  prepare: reasonOf,
  summary: (a) => a.reason || "",
  view: (w) => ({
    title: "Le modèle demande une couleur", detail: w.reason,
    actions: [{ id: "pick", label: "Prendre une couleur", primary: true }, REFUSE],
  }),
  async act(w, action) {
    if (action === "refuse") return refused("de prendre une couleur");
    try {
      const { sRGBHex } = await new window.EyeDropper().open();
      return { result: `Couleur prise : ${sRGBHex}` };
    } catch (e) {
      return e.name === "AbortError" ? null : { result: failure("pipette indisponible : " + e.message) };
    }
  },
});

export const FAMILIES = [defineFamily({
  key: FAMILY.COLOR, label: "Pipette", hint: "une couleur prise à l'écran",
  available: () => "EyeDropper" in window,
  tools: [pickColor],
})];
