// @ts-check
// Partage : un texte vers une autre application (le partage du système),
// sur un clic de la carte.
import { TOOL, FAMILY, PageTool, defineFamily, refused, REFUSE, failure } from "../contract.js";
import { webUrl } from "./links.js";

/** @typedef {import("../contract.js").Card} Card */

class Share extends PageTool {
  constructor() {
    super({
      name: TOOL.SHARE,
      description: "Propose à l'utilisateur de partager un texte (et une adresse) vers une autre "
        + "application — messagerie, mail, notes — par le partage du système.",
      params: {
        title: { type: "string", description: "Un titre (facultatif)." },
        text: { type: "string", description: "Le texte à partager." },
        url: { type: "string", description: "Une adresse (facultatif)." },
      },
      required: ["text"],
    });
  }

  /** @override */
  waits() { return true; }
  /** @override @param {any} a */
  summary(a) { return a.title || (a.text || "").slice(0, 80); }

  /** @override @param {any} args */
  prepare(args) {
    if (!String(args.text || "").trim()) return { error: failure("`text` attendu.") };
    if (args.url && !webUrl(args.url)) return { error: failure("`url` doit être une adresse http ou https.") };
    return {};
  }

  /** @override @param {Card} w */
  view(w) {
    return { title: "Le modèle propose de partager", detail: String(w.args.title || ""),
             quote: String(w.args.text) + (w.args.url ? "\n" + w.args.url : ""),
             actions: [{ id: "share", label: "Partager…", primary: true }, REFUSE] };
  }

  /** @override @param {Card} w @param {string} action */
  async act(w, action) {
    if (action === "refuse") return refused("de partager");
    try {
      await navigator.share({ title: w.args.title || undefined, text: w.args.text,
                              url: w.args.url || undefined });
      return { result: "Partagé par l'utilisateur." };
    } catch (e) {
      return e.name === "AbortError" ? refused("de partager")
        : { result: failure("partage impossible : " + e.message) };
    }
  }
}

export const FAMILIES = [defineFamily({
  key: FAMILY.SHARE, label: "Partage", hint: "vers une autre application, sur une carte",
  available: () => !!navigator.share,
  tools: [new Share()],
})];
