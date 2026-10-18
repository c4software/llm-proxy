// @ts-check
// Presse-papiers : écrire aussitôt ; lire seulement sur un clic de la
// carte (il peut contenir n'importe quoi).
import { TOOL, FAMILY, defineTool, defineFamily, refused, REFUSE, clip, reasonOf, failure } from "./contract.js";

const MAX_READ = 20_000;

const clipboardWrite = defineTool({
  name: TOOL.CLIPBOARD_WRITE,
  description: "Copie un texte dans le presse-papiers de l'utilisateur (une commande, une "
    + "adresse, un message à coller ailleurs).",
  params: { text: { type: "string", description: "Le texte à copier." } },
  required: ["text"],
  summary: (a) => (a.text || "").slice(0, 80),
  async run(args) {
    const text = String(args.text ?? "");
    if (!text) return failure("`text` attendu.");
    try {
      await navigator.clipboard.writeText(text);
      return `Copié dans le presse-papiers (${text.length} caractères).`;
    } catch (e) {
      return failure("copie refusée par le navigateur (la page doit avoir le focus) : " + e.message);
    }
  },
});

const clipboardRead = defineTool({
  name: TOOL.CLIPBOARD_READ,
  description: "Demande à l'utilisateur le contenu de son presse-papiers, qu'il accepte ou "
    + "refuse sur une carte : quand il parle de « ce que j'ai copié ».",
  params: { reason: { type: "string", description: "Pourquoi, en une phrase, montrée à l'utilisateur." } },
  waits: true,
  prepare: reasonOf,
  summary: (a) => a.reason || "",
  view: (w) => ({
    title: "Le modèle demande le contenu de votre presse-papiers", detail: w.reason,
    actions: [{ id: "paste", label: "Coller le presse-papiers", primary: true }, REFUSE],
  }),
  async act(w, action) {
    if (action === "refuse") return refused("de donner son presse-papiers");
    try {
      const text = await navigator.clipboard.readText();
      return { result: text ? "Presse-papiers :\n" + clip(text, MAX_READ) : "Le presse-papiers est vide (ou n'a pas de texte)." };
    } catch (e) {
      return { result: failure("lecture refusée par le navigateur : " + e.message) };
    }
  },
});

export const FAMILIES = [defineFamily({
  key: FAMILY.CLIPBOARD, label: "Presse-papiers", hint: "copier ; lire sur une carte", on: true,
  available: () => !!navigator.clipboard,
  tools: [clipboardWrite, clipboardRead],
})];
