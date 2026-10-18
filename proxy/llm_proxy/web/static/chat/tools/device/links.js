// @ts-check
// Liens : ouvrir une adresse dans un nouvel onglet, sur un clic de la
// carte, où l'adresse se lit en entier.
import { TOOL, FAMILY, PageTool, defineFamily, refused, REFUSE, failure } from "../contract.js";

/** @typedef {import("../contract.js").Card} Card */

/** Une adresse http(s) complète, normalisée — ou null. @param {any} url */
export function webUrl(url) {
  try {
    const u = new URL(String(url || ""));
    return ["http:", "https:"].includes(u.protocol) ? u.href : null;
  } catch { return null; }
}

class OpenLink extends PageTool {
  constructor() {
    super({
      name: TOOL.OPEN_LINK,
      description: "Propose à l'utilisateur d'ouvrir une adresse web dans un nouvel onglet (un "
        + "itinéraire, une recherche, une page de documentation), sur une carte qu'il accepte ou "
        + "refuse.",
      params: {
        url: { type: "string", description: "L'adresse complète, http ou https." },
        reason: { type: "string", description: "Pourquoi l'ouvrir, en une phrase (facultatif)." },
      },
      required: ["url"],
    });
  }

  /** @override */
  waits() { return true; }
  /** @override @param {any} a */
  summary(a) { return a.url || ""; }

  /** @override @param {any} args */
  prepare(args) {
    const url = webUrl(args.url);
    return url ? { url, reason: String(args.reason || "").trim().slice(0, 300) }
      : { error: failure("`url` doit être une adresse http ou https complète.") };
  }

  /** @override @param {Card} w */
  view(w) {
    return { title: "Le modèle propose d'ouvrir une page", detail: w.reason, code: w.url,
             actions: [{ id: "open", label: "Ouvrir dans un onglet", primary: true }, REFUSE] };
  }

  /** @override @param {Card} w @param {string} action */
  act(w, action) {
    if (action === "refuse") return refused("d'ouvrir la page");
    window.open(w.url, "_blank", "noopener,noreferrer");
    return { result: `Page ouverte dans un nouvel onglet : ${w.url}` };
  }
}

export const FAMILIES = [defineFamily({
  key: FAMILY.LINKS, label: "Liens", hint: "ouvrir une adresse, sur une carte", on: true,
  tools: [new OpenLink()],
})];
