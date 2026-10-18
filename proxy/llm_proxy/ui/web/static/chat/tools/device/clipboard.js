// @ts-check
// Presse-papiers : écrire aussitôt — le texte copié reste en CARTE
// DURABLE sous la réponse, à recopier à volonté ; lire seulement sur un
// clic de la carte (il peut contenir n'importe quoi).
import { TOOL, FAMILY, PageTool, defineFamily, refused, REFUSE, REASON, clip, reasonOf, failure } from "../contract.js";

import { clipboard } from "./vueuse.js";

/** @typedef {import("../contract.js").Card} Card */
/** @typedef {import("../contract.js").Kept} Kept */

const MAX_READ = 20_000;

class ClipboardWrite extends PageTool {
  constructor() {
    super({
      name: TOOL.CLIPBOARD_WRITE,
      description: "Copie un texte dans le presse-papiers de l'utilisateur (une commande, une "
        + "adresse, un message à coller ailleurs).",
      params: { text: { type: "string", description: "Le texte à copier." } },
      required: ["text"],
    });
  }

  /** @override @param {any} a */
  summary(a) { return (a.text || "").slice(0, 80); }

  /** @override @param {any} args */
  async run(args) {
    const text = String(args.text ?? "");
    if (!text) return failure("`text` attendu.");
    const keep = { text };
    try {
      await navigator.clipboard.writeText(text);
    } catch (e) {
      // La page n'a pas le focus : la carte permet de copier d'un clic.
      return { result: failure("copie refusée par le navigateur (la page doit avoir le focus) : "
        + e.message + " Le texte est en carte sous la réponse : l'utilisateur peut le copier d'un clic."), keep };
    }
    return { result: `Copié dans le presse-papiers (${text.length} caractères) ; le texte reste en carte `
      + "sous la réponse, à recopier.", keep };
  }

  /** @override @param {Kept} k */
  keptView(k) {
    return { title: "Texte à copier", quote: k.text, actions: [{ id: "copy", label: "Copier", primary: true }] };
  }

  /** @override @param {Kept} k */
  async keptAct(k) {
    await clipboard.copy(k.text);
    return "Copié.";
  }
}

class ClipboardRead extends PageTool {
  constructor() {
    super({
      name: TOOL.CLIPBOARD_READ,
      description: "Demande à l'utilisateur le contenu de son presse-papiers, qu'il accepte ou "
        + "refuse sur une carte : quand il parle de « ce que j'ai copié ».",
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
  view(w) {
    return { title: "Le modèle demande le contenu de votre presse-papiers", detail: w.reason,
             actions: [{ id: "paste", label: "Coller le presse-papiers", primary: true }, REFUSE] };
  }

  /** @override @param {Card} w @param {string} action */
  async act(w, action) {
    if (action === "refuse") return refused("de donner son presse-papiers");
    try {
      const text = await navigator.clipboard.readText();
      return { result: text ? "Presse-papiers :\n" + clip(text, MAX_READ) : "Le presse-papiers est vide (ou n'a pas de texte)." };
    } catch (e) {
      return { result: failure("lecture refusée par le navigateur : " + e.message) };
    }
  }
}

export const FAMILIES = [defineFamily({
  key: FAMILY.CLIPBOARD, label: "Presse-papiers", hint: "copier ; lire sur une carte", on: true,
  available: () => !!navigator.clipboard,
  tools: [new ClipboardWrite(), new ClipboardRead()],
})];
