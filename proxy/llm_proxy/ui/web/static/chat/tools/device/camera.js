// @ts-check
// Caméra : une photo, montrée en aperçu avant d'être envoyée.
import { TOOL, FAMILY, PageTool, defineFamily, refused, REFUSE, REASON, reasonOf } from "../contract.js";
import { hasMedia, live, stream, stop, camera, frame, jpeg, imageName, denied } from "./media.js";

/** @typedef {import("../contract.js").Card} Card */

const TITLE = "Le modèle demande une photo";

class TakePhoto extends PageTool {
  constructor() {
    super({
      name: TOOL.TAKE_PHOTO,
      description: "Demande à l'utilisateur une photo prise avec sa caméra (une étiquette, un "
        + "document, un objet), qu'il vérifie avant de l'envoyer. Il faut un modèle qui lit les images.",
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
  release(w) { stop(w); }

  /** @override @param {Card} w */
  view(w) {
    if (w.preview) {
      return { title: TITLE, detail: w.reason, preview: w.preview,
               actions: [{ id: "send", label: "Envoyer la photo", primary: true },
                         { id: "start", label: "Reprendre" }, REFUSE] };
    }
    const on = live(w);
    return { title: TITLE, detail: w.reason, video: on ? stream(w) : undefined, note: w.note,
             actions: on ? [{ id: "shoot", label: "Prendre la photo", primary: true }, REFUSE]
               : [{ id: "start", label: "Activer la caméra", primary: true }, REFUSE] };
  }

  /** @override @param {Card} w @param {string} action */
  async act(w, action) {
    if (action === "refuse") return refused("la photo");
    if (action === "send") {
      return { result: "Photo envoyée par l'utilisateur : l'image suit.", image: w.preview, name: imageName("photo") };
    }
    w.note = "";
    try {
      if (action === "start") {
        w.preview = "";
        await camera(w);
      } else if (action === "shoot" && stream(w)) {
        w.preview = jpeg(await frame(stream(w)));
        stop(w);
      }
    } catch (e) {
      return denied(w, e);
    }
    return null;
  }
}

export const FAMILIES = [defineFamily({
  key: FAMILY.CAMERA, label: "Caméra", hint: "une photo, avec aperçu",
  available: () => hasMedia("getUserMedia"),
  tools: [new TakePhoto()],
})];
