// @ts-check
// Partage : un texte (et une adresse) vers une autre application, par le
// partage du système. L'appel laisse une CARTE DURABLE sous la réponse :
// le partage ne part que d'un clic (le navigateur l'exige), et peut se
// refaire plus tard — la carte survit à un rechargement. Là où le
// partage manque (un autre appareil), la carte copie le texte.
import { TOOL, FAMILY, PageTool, defineFamily, failure } from "../contract.js";
import { webUrl } from "./links.js";

/** @typedef {import("../contract.js").Kept} Kept */

const canShare = () => !!navigator.share;
/** @param {Kept} k */
const full = (k) => k.text + (k.url ? "\n" + k.url : "");

class Share extends PageTool {
  constructor() {
    super({
      name: TOOL.SHARE,
      description: "Propose à l'utilisateur de partager un texte (et une adresse) vers une autre "
        + "application — messagerie, mail, notes — par le partage du système : une carte sous la "
        + "réponse, avec un bouton Partager… qu'il utilise quand il veut (et autant de fois qu'il "
        + "veut). Le résultat ne dit pas s'il a partagé.",
      params: {
        title: { type: "string", description: "Un titre (facultatif)." },
        text: { type: "string", description: "Le texte à partager." },
        url: { type: "string", description: "Une adresse (facultatif)." },
      },
      required: ["text"],
    });
  }

  /** @override @param {any} a */
  summary(a) { return a.title || (a.text || "").slice(0, 80); }

  /** @override @param {any} args */
  run(args) {
    const text = String(args.text || "").trim();
    if (!text) return failure("`text` attendu.");
    const url = args.url ? webUrl(args.url) : "";
    if (url === null) return failure("`url` doit être une adresse http ou https.");
    const title = String(args.title || "").trim().slice(0, 120);
    return { result: "Carte de partage proposée sous la réponse : l'utilisateur la partage quand il veut.",
             keep: { title, text, url } };
  }

  /** @override @param {Kept} k */
  keptView(k) {
    return { title: k.title || "À partager", quote: full(k),
             actions: [...(canShare() ? [{ id: "share", label: "Partager…", primary: true }] : []),
                       { id: "copy", label: "Copier", primary: !canShare() }] };
  }

  /** @override @param {Kept} k @param {string} action */
  async keptAct(k, action) {
    if (action === "copy") {
      await navigator.clipboard.writeText(full(k));
      return "Copié.";
    }
    try {
      await navigator.share({ title: k.title || undefined, text: k.text, url: k.url || undefined });
      return "Partagé.";
    } catch (e) {
      if (e.name === "AbortError") return null;
      throw new Error("Partage impossible : " + e.message);
    }
  }
}

export const FAMILIES = [defineFamily({
  key: FAMILY.SHARE, label: "Partage", hint: "une carte sous la réponse, à partager quand on veut",
  available: canShare,
  tools: [new Share()],
})];
