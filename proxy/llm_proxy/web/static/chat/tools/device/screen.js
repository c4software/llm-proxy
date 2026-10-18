// @ts-check
// Capture d'écran : un onglet, une fenêtre ou l'écran, montré en aperçu
// avant d'être envoyé (Envoyer, Reprendre, Refuser).
import { TOOL, FAMILY, PageTool, defineFamily, refused, REFUSE, REASON, reasonOf } from "../contract.js";
import { hasMedia, screenshot, jpeg, imageName, denied } from "./media.js";

/** @typedef {import("../contract.js").Card} Card */

class CaptureScreen extends PageTool {
  constructor() {
    super({
      name: TOOL.CAPTURE_SCREEN,
      description: "Demande à l'utilisateur une capture de son écran (un onglet, une fenêtre ou "
        + "l'écran entier, qu'il choisit et vérifie avant de l'envoyer) : pour voir une erreur, une "
        + "interface, un document. Il faut un modèle qui lit les images.",
      params: REASON,
      required: ["reason"],
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
    return {
      title: "Le modèle demande une capture d'écran", detail: w.reason, preview: w.preview, note: w.note,
      actions: w.preview
        ? [{ id: "send", label: "Envoyer la capture", primary: true }, { id: "pick", label: "Reprendre" }, REFUSE]
        : [{ id: "pick", label: "Choisir quoi capturer", primary: true }, REFUSE],
    };
  }

  /** @override @param {Card} w @param {string} action */
  async act(w, action) {
    if (action === "refuse") return refused("la capture d'écran");
    if (action === "send") {
      return { result: "Capture d'écran envoyée par l'utilisateur : l'image suit.",
               image: w.preview, name: imageName("capture") };
    }
    w.note = "";
    try {
      w.preview = jpeg(await screenshot());
    } catch (e) {
      return denied(w, e);
    }
    return null;
  }
}

export const FAMILIES = [defineFamily({
  key: FAMILY.SCREEN, label: "Capture d'écran", hint: "un onglet, une fenêtre ou l'écran, avec aperçu",
  available: () => hasMedia("getDisplayMedia"),
  tools: [new CaptureScreen()],
})];
