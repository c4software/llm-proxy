/*
  `ask_user` : le modèle pose de 1 à 4 questions à choix dans le fil, et
  sa réponse ATTEND la vôtre. Chaque question a ses options (choix unique
  ou multiple, chacune avec sa description) et toujours « Autre… », en
  texte libre ; un onglet par question (`header`). La carte vit dans le
  message (m.waiting) : elle survit à un rechargement, et la réponse
  reprend où elle s'était arrêtée. Écrire un nouveau message au lieu de
  répondre clôt la question, sans réponse.
*/

export const TOOL = { type: "function", function: {
  name: "ask_user",
  description: "Pose à l'utilisateur de 1 à 4 questions à choix, dans le chat, et attend "
    + "sa réponse avant de continuer. À utiliser quand une décision lui revient ou qu'une "
    + "précision manque pour bien répondre — pas pour des confirmations inutiles. Chaque "
    + "question a 2 à 6 options courtes, chacune avec une brève description de ce "
    + "qu'elle implique ; l'utilisateur peut aussi répondre librement.",
  parameters: { type: "object", required: ["questions"], properties: {
    questions: { type: "array", minItems: 1, maxItems: 4, items: {
      type: "object", required: ["question", "header", "options"], properties: {
        question: { type: "string", description: "La question, complète." },
        header: { type: "string", description: "Un titre très court (1 à 3 mots), l'onglet de la question : « Budget », « Hébergement »." },
        options: { type: "array", minItems: 2, maxItems: 6, items: {
          type: "object", required: ["label"], properties: {
            label: { type: "string", description: "La réponse, courte." },
            description: { type: "string", description: "Ce qu'elle implique, en une phrase." },
          } } },
        multiple: { type: "boolean", description: "Plusieurs options peuvent être choisies." },
      } } },
  } } } };

// Une option : {label, description} — un modèle qui écrit une simple
// chaîne est accepté aussi (et les questions gardées avant les
// descriptions en sont faites).
export function option(o) {
  if (o && typeof o === "object") {
    return { label: String(o.label || "").trim(), description: String(o.description || "").trim() };
  }
  return { label: String(o == null ? "" : o).trim(), description: "" };
}

// Les arguments du modèle → l'état de la carte, ou {error}.
export function prepare(args) {
  const qs = Array.isArray(args.questions) ? args.questions.slice(0, 4) : [];
  const questions = qs.filter((x) => x && typeof x.question === "string" && x.question.trim())
    .map((x, n) => ({
      question: x.question.trim(),
      header: String(x.header || "").trim().slice(0, 24) || `Question ${n + 1}`,
      options: (Array.isArray(x.options) ? x.options : []).map(option)
        .filter((o) => o.label).slice(0, 6),
      multiple: !!x.multiple, picked: [], other: "", otherOn: false,
    }));
  if (!questions.length) return { error: "Error: `questions` attendu, de 1 à 4 questions avec leurs options." };
  return { kind: "ask", questions };
}

// Une question a-t-elle sa réponse ?
export const answered = (q) => q.picked.length > 0 || (q.other || "").trim() !== "";

// La carte remplie → le résultat rendu au modèle.
export function result(waiting) {
  return waiting.questions.map((q, n) => {
    const parts = [...q.picked];
    if ((q.other || "").trim()) parts.push("autre : « " + q.other.trim() + " »");
    return `${n + 1}. ${q.question}\n→ ${parts.length ? parts.join(" ; ") : "(sans réponse)"}`;
  }).join("\n");
}

export const NO_ANSWER = "L'utilisateur n'a pas répondu : il a écrit un nouveau message à la place.";
