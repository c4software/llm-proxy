// @ts-check
// QR et codes-barres : lus à la caméra ou sur une capture d'écran
// (BarcodeDetector) ; seul le texte lu part au modèle, pas d'image.
import { TOOL, FAMILY, defineTool, defineFamily, refused, REFUSE, REASON, reasonOf } from "./contract.js";
import { hasMedia, live, stop, camera, screenshot, every, player, denied } from "./media.js";

const SCAN_EVERY = 400;              // ms entre deux lectures à la caméra

const read = (codes) => codes.map((c) => `${c.format} : ${c.rawValue}`).join("\n");

const scanCode = defineTool({
  name: TOOL.SCAN_CODE,
  description: "Demande à l'utilisateur de lire un QR code ou un code-barres, à la caméra ou "
    + "sur son écran ; rend le texte lu (pas d'image).",
  params: REASON,
  waits: true,
  prepare: reasonOf,
  summary: (a) => a.reason || "",
  release: stop,
  view(w) {
    if (w.found) {
      return { title: "Code lu", detail: w.reason, quote: w.found,
               actions: [{ id: "send", label: "Envoyer", primary: true }, { id: "start", label: "Relire" }, REFUSE] };
    }
    const on = live(w);
    return { title: "Le modèle demande de lire un code", detail: w.reason, video: on,
             note: w.note || (on ? "présentez le code à la caméra…" : ""),
             actions: on ? [REFUSE] : [{ id: "start", label: "Caméra", primary: true },
                                       { id: "screen", label: "Sur l'écran" }, REFUSE] };
  },
  async act(w, action) {
    if (action === "refuse") { stop(w); return refused("de lire un code"); }
    if (action === "send") { stop(w); return { result: "Code lu :\n" + w.found }; }
    w.note = "";
    try {
      if (action === "screen") {
        w.found = read(await new window.BarcodeDetector().detect(await screenshot()));
        if (!w.found) w.note = "aucun code trouvé sur cette capture.";
      } else if (action === "start") {
        w.found = "";
        const detector = new window.BarcodeDetector();
        const video = await player(await camera(w));
        every(w, SCAN_EVERY, async () => {
          const codes = await detector.detect(video).catch(() => []);
          if (codes.length) { w.found = read(codes); stop(w); }
        });
      }
    } catch (e) {
      return denied(w, e);
    }
    return null;
  },
});

export const FAMILIES = [defineFamily({
  key: FAMILY.CODES, label: "QR et codes-barres", hint: "lus à la caméra ou à l'écran",
  available: () => "BarcodeDetector" in window && hasMedia("getUserMedia"),
  tools: [scanCode],
})];
