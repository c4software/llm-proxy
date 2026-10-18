// @ts-check
// QR et codes-barres : lus à la caméra ou sur une capture d'écran
// (BarcodeDetector) ; seul le texte lu part au modèle, pas d'image.
import { TOOL, FAMILY, PageTool, defineFamily, refused, REFUSE, REASON, reasonOf } from "../contract.js";
import { hasMedia, live, stream, stop, camera, screenshot, every, player, denied } from "./media.js";

/** @typedef {import("../contract.js").Card} Card */

const SCAN_EVERY = 400;              // ms entre deux lectures à la caméra

/** @param {any[]} codes */
const read = (codes) => codes.map((c) => `${c.format} : ${c.rawValue}`).join("\n");

class ScanCode extends PageTool {
  constructor() {
    super({
      name: TOOL.SCAN_CODE,
      description: "Demande à l'utilisateur de lire un QR code ou un code-barres, à la caméra ou "
        + "sur son écran ; rend le texte lu (pas d'image).",
      params: REASON,
    });
  }

  /** @override */
  waits() { return true; }
  /** @override @param {any} args */
  prepare(args) { return reasonOf(args); }
  /** @override @param {any} a */
  summary(a) { return a.reason || ""; }
  /** @override @param {Card} w */
  release(w) { stop(w); }

  /** @override @param {Card} w */
  view(w) {
    if (w.found) {
      return { title: "Code lu", detail: w.reason, quote: w.found,
               actions: [{ id: "send", label: "Envoyer", primary: true }, { id: "start", label: "Relire" }, REFUSE] };
    }
    const on = live(w);
    return { title: "Le modèle demande de lire un code", detail: w.reason, video: on ? stream(w) : undefined,
             note: w.note || (on ? "présentez le code à la caméra…" : ""),
             actions: on ? [REFUSE] : [{ id: "start", label: "Caméra", primary: true },
                                       { id: "screen", label: "Sur l'écran" }, REFUSE] };
  }

  /** @override @param {Card} w @param {string} action */
  async act(w, action) {
    if (action === "refuse") return refused("de lire un code");
    if (action === "send") return { result: "Code lu :\n" + w.found };
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
  }
}

export const FAMILIES = [defineFamily({
  key: FAMILY.CODES, label: "QR et codes-barres", hint: "lus à la caméra ou à l'écran",
  available: () => "BarcodeDetector" in window && hasMedia("getUserMedia"),
  tools: [new ScanCode()],
})];
