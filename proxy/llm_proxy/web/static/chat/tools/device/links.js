// @ts-check
// Liens : une adresse à ouvrir dans un nouvel onglet. L'appel laisse une
// CARTE DURABLE sous la réponse, où l'adresse se lit en entier : elle
// s'ouvre sur un clic, et se rouvre plus tard (la carte survit à un
// rechargement).
import { TOOL, FAMILY, PageTool, defineFamily, failure } from "../contract.js";
import { clipboard } from "./vueuse.js";

/** @typedef {import("../contract.js").Kept} Kept */

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
      description: "Propose à l'utilisateur d'ouvrir une adresse web (un itinéraire, une recherche, "
        + "une page de documentation) : une carte sous la réponse, où il lit l'adresse et l'ouvre "
        + "dans un nouvel onglet quand il veut. Le résultat ne dit pas s'il l'a ouverte.",
      params: {
        url: { type: "string", description: "L'adresse complète, http ou https." },
        reason: { type: "string", description: "Pourquoi l'ouvrir, en une phrase (facultatif)." },
      },
      required: ["url"],
    });
  }

  /** @override @param {any} a */
  summary(a) { return a.url || ""; }

  /** @override @param {any} args */
  run(args) {
    const url = webUrl(args.url);
    if (!url) return failure("`url` doit être une adresse http ou https complète.");
    return { result: `Lien proposé sous la réponse : ${url}`,
             keep: { url, reason: String(args.reason || "").trim().slice(0, 300) } };
  }

  /** @override @param {Kept} k */
  keptView(k) {
    return { title: "Lien proposé", detail: k.reason, code: k.url,
             actions: [{ id: "open", label: "Ouvrir dans un onglet", primary: true }, { id: "copy", label: "Copier" }] };
  }

  /** @override @param {Kept} k @param {string} action */
  async keptAct(k, action) {
    if (action === "copy") { await clipboard.copy(k.url); return "Copiée."; }
    window.open(k.url, "_blank", "noopener,noreferrer");
    return null;
  }
}

export const FAMILIES = [defineFamily({
  key: FAMILY.LINKS, label: "Liens", hint: "une adresse à ouvrir, en carte sous la réponse", on: true,
  tools: [new OpenLink()],
})];
