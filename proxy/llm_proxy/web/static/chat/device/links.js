// Liens et partage : ouvrir une adresse dans un nouvel onglet, ou
// partager un texte vers une autre application (le partage du système) —
// les deux sur un clic de la carte, où l'adresse se lit en entier.
import { fn, refused, REFUSE } from "./common.js";

export const FAMILIES = [
  { key: "links", label: "Liens", hint: "ouvrir une adresse, sur une carte", on: true,
    tools: [fn("open_link", "Propose à l'utilisateur d'ouvrir une adresse web dans un nouvel "
      + "onglet (un itinéraire, une recherche, une page de documentation), sur une carte qu'il "
      + "accepte ou refuse.", {
      url: { type: "string", description: "L'adresse complète, http ou https." },
      reason: { type: "string", description: "Pourquoi l'ouvrir, en une phrase (facultatif)." },
    }, ["url"])] },
  { key: "share", label: "Partage", hint: "vers une autre application, sur une carte",
    available: () => !!navigator.share,
    tools: [fn("share", "Propose à l'utilisateur de partager un texte (et une adresse) vers une "
      + "autre application — messagerie, mail, notes — par le partage du système.", {
      title: { type: "string", description: "Un titre (facultatif)." },
      text: { type: "string", description: "Le texte à partager." },
      url: { type: "string", description: "Une adresse (facultatif)." },
    }, ["text"])] },
];

export const waits = () => true;

function web(url) {
  try {
    const u = new URL(String(url || ""));
    return ["http:", "https:"].includes(u.protocol) ? u.href : null;
  } catch { return null; }
}

export function prepare(name, args) {
  if (name === "open_link") {
    const url = web(args.url);
    return url ? { url, reason: String(args.reason || "").trim().slice(0, 300) }
      : { error: "Error: `url` doit être une adresse http ou https complète." };
  }
  if (!String(args.text || "").trim()) return { error: "Error: `text` attendu." };
  if (args.url && !web(args.url)) return { error: "Error: `url` doit être une adresse http ou https." };
  return {};
}

export function view(w) {
  if (w.tool === "open_link") {
    return { title: "Le modèle propose d'ouvrir une page", detail: w.reason, code: w.url,
             actions: [{ id: "open", label: "Ouvrir dans un onglet", primary: true }, REFUSE] };
  }
  return { title: "Le modèle propose de partager", detail: String(w.args.title || ""),
           quote: String(w.args.text) + (w.args.url ? "\n" + w.args.url : ""),
           actions: [{ id: "share", label: "Partager…", primary: true }, REFUSE] };
}

export async function act(w, action) {
  if (action === "refuse") return refused(w.tool === "open_link" ? "d'ouvrir la page" : "de partager");
  if (w.tool === "open_link") {
    window.open(w.url, "_blank", "noopener,noreferrer");
    return { result: `Page ouverte dans un nouvel onglet : ${w.url}` };
  }
  try {
    await navigator.share({ title: w.args.title || undefined, text: w.args.text,
                            url: w.args.url || undefined });
    return { result: "Partagé par l'utilisateur." };
  } catch (e) {
    return e.name === "AbortError" ? refused("de partager")
      : { result: "Error: partage impossible : " + e.message };
  }
}
