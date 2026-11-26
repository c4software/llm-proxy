// @ts-check
/*
  `ask_user` : le modèle pose de 1 à 4 questions à choix, et sa réponse
  ATTEND la vôtre. Chaque question a ses options (choix unique ou
  multiple, chacune avec sa description) et toujours « Autre… », en texte
  libre ; un onglet par question (`header`). La carte s'affiche dans la
  FEUILLE du bas (AskSheet) ; elle vit dans le message (m.waiting) et
  survit à un rechargement. Écrire un nouveau message au lieu de répondre
  clôt la question, sans réponse.

  Plusieurs questions : un onglet par question, puis un RÉCAPITULATIF
  (chaque question et sa réponse, en une ligne : un clic y revient) ; le
  bouton principal fait « Suivant » dès que la question a sa réponse,
  « Envoyer les réponses » sur le récapitulatif.

  Le clavier, dans la feuille : 1…9 choisit, ←/→ change d'onglet, Entrée
  fait le bouton principal (Suivant, Envoyer), Échap réduit.
*/
import { TOOL, FAMILY, PageTool, defineFamily, failure } from "../contract.js";
import AskSheet, { option, answered } from "./AskSheet.vue";


class AskUser extends PageTool {
  constructor() {
    super({ name: TOOL.ASK_USER, placement: "sheet", component: AskSheet });
  }

  /** @override */
  waits() { return true; }

  /** @override @param {any} args */
  prepare(args) {
    const qs = Array.isArray(args.questions) ? args.questions.slice(0, 4) : [];
    const questions = qs.filter((x) => x && typeof x.question === "string" && x.question.trim())
      .map((x, n) => ({
        question: x.question.trim(),
        header: String(x.header || "").trim().slice(0, 24) || `Question ${n + 1}`,
        options: (Array.isArray(x.options) ? x.options : []).map(option)
          .filter((o) => o.label).slice(0, 6),
        multiple: !!x.multiple, picked: [], other: "", otherOn: false,
      }));
    if (!questions.length) return { error: failure("`questions` attendu, de 1 à 4 questions avec leurs options.") };
    return { questions };
  }

  /** @override @param {import("../contract.js").Card} w @param {string} action */
  act(w, action) {
    if (action === "skip") return { result: "L'utilisateur a passé la question sans répondre." };
    if (!w.questions.every(answered)) return null;
    return { result: w.questions.map((q, n) => {
      const parts = [...q.picked];
      if ((q.other || "").trim()) parts.push("autre : « " + q.other.trim() + " »");
      return `${n + 1}. ${q.question}\n→ ${parts.length ? parts.join(" ; ") : "(sans réponse)"}`;
    }).join("\n") };
  }

  /** @override */
  notice() { return "Une question vous attend"; }

  /** @override @param {import("../contract.js").Card} w */
  pending(w) {
    const n = w.questions.length;
    return `${n > 1 ? n + " questions attendent" : "Une question attend"} votre réponse`;
  }

  /** @override @param {any} a */
  summary(a) {
    return Array.isArray(a.questions)
      ? a.questions.map((q) => (q && q.question) || "").filter(Boolean).join(" · ").slice(0, 140) : "";
  }
}

export const FAMILIES = [defineFamily({ key: FAMILY.ASK, tools: [new AskUser()] })];
