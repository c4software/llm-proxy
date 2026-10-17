// Presse-papiers : écrire aussitôt ; lire seulement sur un clic de la
// carte (il peut contenir n'importe quoi).
import { fn, refused, REFUSE, clip } from "./common.js";

const MAX_READ = 20_000;

export const FAMILIES = [{
  key: "clipboard", label: "Presse-papiers", hint: "copier ; lire sur une carte", on: true,
  available: () => !!navigator.clipboard,
  tools: [
    fn("clipboard_write", "Copie un texte dans le presse-papiers de l'utilisateur (une "
      + "commande, une adresse, un message à coller ailleurs).",
    { text: { type: "string", description: "Le texte à copier." } }, ["text"]),
    fn("clipboard_read", "Demande à l'utilisateur le contenu de son presse-papiers, qu'il "
      + "accepte ou refuse sur une carte : quand il parle de « ce que j'ai copié ».",
    { reason: { type: "string", description: "Pourquoi, en une phrase, montrée à l'utilisateur." } }),
  ],
}];

export const waits = (name) => name === "clipboard_read";

export async function run(name, args) {
  const text = String(args.text ?? "");
  if (!text) return "Error: `text` attendu.";
  try {
    await navigator.clipboard.writeText(text);
    return `Copié dans le presse-papiers (${text.length} caractères).`;
  } catch (e) {
    return "Error: copie refusée par le navigateur (la page doit avoir le focus) : " + e.message;
  }
}

export const prepare = (name, args) => ({ reason: String(args.reason || "").trim().slice(0, 300) });

export const view = (w) => ({
  title: "Le modèle demande le contenu de votre presse-papiers", detail: w.reason,
  actions: [{ id: "paste", label: "Coller le presse-papiers", primary: true }, REFUSE],
});

export async function act(w, action) {
  if (action === "refuse") return refused("de donner son presse-papiers");
  try {
    const text = await navigator.clipboard.readText();
    return { result: text ? "Presse-papiers :\n" + clip(text, MAX_READ) : "Le presse-papiers est vide (ou n'a pas de texte)." };
  } catch (e) {
    return { result: "Error: lecture refusée par le navigateur : " + e.message };
  }
}
