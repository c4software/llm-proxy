// @ts-check
// QR et codes-barres : lus à la caméra ou sur une capture d'écran
// (BarcodeDetector) ; seul le texte lu part au modèle, pas d'image. Le
// code lu reste en CARTE DURABLE : à copier, ou à ouvrir si c'est une
// adresse.
import { TOOL, FAMILY, PageTool, defineFamily, refused, REFUSE, REASON, reasonOf } from "../contract.js";
import { hasMedia, live, stream, stop, camera, screenshot, every, player, denied } from "./media.js";

import { clipboard } from "./vueuse.js";
import { webUrl } from "./links.js";

/** @typedef {import("../contract.js").Card} Card */
/** @typedef {import("../contract.js").Kept} Kept */

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
    if (action === "send") return { result: "Code lu :\n" + w.found, keep: { found: w.found } };
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

  /** @override @param {Kept} k */
  keptView(k) {
    const urls = k.found.split("\n").map((l) => webUrl(l.replace(/^[^:]+ : /, ""))).filter(Boolean);
    return { title: "Code lu", quote: k.found,
             actions: [{ id: "copy", label: "Copier", primary: true },
                       ...(urls.length ? [{ id: "open", label: "Ouvrir l'adresse", title: urls[0] }] : [])] };
  }

  /** @override @param {Kept} k @param {string} action */
  async keptAct(k, action) {
    if (action === "open") {
      const url = k.found.split("\n").map((l) => webUrl(l.replace(/^[^:]+ : /, ""))).find(Boolean);
      if (url) window.open(url, "_blank", "noopener,noreferrer");
      return null;
    }
    await clipboard.copy(k.found.replace(/^[^:\n]+ : /gm, ""));
    return "Copié.";
  }
}

export const FAMILIES = [defineFamily({
  key: FAMILY.CODES, label: "QR et codes-barres", hint: "lus à la caméra ou à l'écran",
  available: () => "BarcodeDetector" in window && hasMedia("getUserMedia"),
  tools: [new ScanCode()],
})];
