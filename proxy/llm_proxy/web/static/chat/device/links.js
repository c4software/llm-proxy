// Liens : ouvrir une adresse dans un nouvel onglet, sur un clic de la
// carte, où l'adresse se lit en entier.
import { TOOL, FAMILY, defineTool, defineFamily, refused, REFUSE, failure } from "./contract.js";

// Une adresse http(s) complète, normalisée — ou null.
export function webUrl(url) {
  try {
    const u = new URL(String(url || ""));
    return ["http:", "https:"].includes(u.protocol) ? u.href : null;
  } catch { return null; }
}

const openLink = defineTool({
  name: TOOL.OPEN_LINK,
  description: "Propose à l'utilisateur d'ouvrir une adresse web dans un nouvel onglet (un "
    + "itinéraire, une recherche, une page de documentation), sur une carte qu'il accepte ou "
    + "refuse.",
  params: {
    url: { type: "string", description: "L'adresse complète, http ou https." },
    reason: { type: "string", description: "Pourquoi l'ouvrir, en une phrase (facultatif)." },
  },
  required: ["url"],
  waits: true,
  summary: (a) => a.url || "",
  prepare(args) {
    const url = webUrl(args.url);
    return url ? { url, reason: String(args.reason || "").trim().slice(0, 300) }
      : { error: failure("`url` doit être une adresse http ou https complète.") };
  },
  view: (w) => ({ title: "Le modèle propose d'ouvrir une page", detail: w.reason, code: w.url,
                  actions: [{ id: "open", label: "Ouvrir dans un onglet", primary: true }, REFUSE] }),
  act(w, action) {
    if (action === "refuse") return refused("d'ouvrir la page");
    window.open(w.url, "_blank", "noopener,noreferrer");
    return { result: `Page ouverte dans un nouvel onglet : ${w.url}` };
  },
});

export const FAMILIES = [defineFamily({
  key: FAMILY.LINKS, label: "Liens", hint: "ouvrir une adresse, sur une carte", on: true,
  tools: [openLink],
})];
