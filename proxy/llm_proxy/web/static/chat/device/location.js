// @ts-check
// Position : lue seulement sur un clic de la carte — une permission du
// navigateur déjà accordée n'y suffit pas. Approximative : arrondie au
// centième de degré (≈ 1 km).
import { TOOL, FAMILY, defineTool, defineFamily, refused, REFUSE, reasonOf, failure } from "./contract.js";

const getLocation = defineTool({
  name: TOOL.GET_LOCATION,
  description: "Demande à l'utilisateur sa position (latitude, longitude), qu'il accepte ou "
    + "refuse sur une carte dans le chat. Seulement quand la réponse en dépend vraiment (météo, "
    + "itinéraire, lieux proches) ; dire pourquoi dans `reason`.",
  params: { reason: { type: "string", description: "Pourquoi la position est utile, en une phrase, montrée à l'utilisateur." } },
  required: ["reason"],
  waits: true,
  prepare: reasonOf,
  summary: (a) => a.reason || "",
  view: (w) => ({
    title: "Le modèle demande votre position", detail: w.reason, note: w.busy ? "lecture de la position…" : "",
    actions: [{ id: "approx", label: "Position approximative", primary: true, title: "Arrondie à environ 1 km" },
              { id: "precise", label: "Position précise" }, REFUSE],
  }),
  act(w, action) {
    if (action === "refuse") return refused("de donner sa position");
    const precise = action === "precise";
    w.busy = true;
    return new Promise((resolve) => {
      navigator.geolocation.getCurrentPosition((p) => {
        const { latitude, longitude, accuracy } = p.coords;
        resolve({ result: precise
          ? `Position : latitude ${latitude.toFixed(5)}, longitude ${longitude.toFixed(5)} (précision ≈ ${Math.round(accuracy)} m).`
          : `Position approximative (≈ 1 km) : latitude ${latitude.toFixed(2)}, longitude ${longitude.toFixed(2)}.` });
      }, (e) => resolve({ result: failure("position non obtenue ("
        + (e.code === 1 ? "permission refusée par le navigateur" : e.code === 3 ? "délai dépassé" : "position indisponible") + ").") }),
      { enableHighAccuracy: precise, timeout: 20_000, maximumAge: 300_000 });
    });
  },
});

export const FAMILIES = [defineFamily({
  key: FAMILY.LOCATION, label: "Position", hint: "demandée sur une carte, approximative ou précise",
  available: () => "geolocation" in navigator,
  tools: [getLocation],
})];
