// @ts-check
/*
  Les TOURS D'OUTILS d'une réponse (ChatPanel.respond, et les modèles de
  l'appareil), sans Vue : l'état vit dans le message de la réponse —

    m.tools   les puces affichées (id, name, status, preview…)
    m.step    le tour en cours : {content, calls (faits), queue (à faire)}
    m.steps   les tours passés, rejoués au modèle (tools.stepMessages)
    m.waiting la carte d'un appel qui attend l'utilisateur
    m.cuts    les coupures d'AFFICHAGE à l'arrivée des outils hébergés
              (le proxy les exécute dans un même tour de la page) :
              positions comme celles des tours, jamais rejouées

  chipFor(m, call)            la puce de l'appel (créée au besoin)
  recordCall(m, chip, call, result, image)  l'appel fait : sa puce, son
                              résultat au tour, retiré de la file
  endStep(m)                  le tour fini rejoint les tours passés
  pushStep(m, st)             un tour (fini, clos, abandonné) rejoint les
                              passés, avec où il s'arrête dans la réflexion
                              et le texte du message (reasoningEnd,
                              contentEnd) : de quoi l'afficher à sa place
  hostedCut(m)                une puce d'outil hébergé vient d'être
                              ajoutée : ce qui précède (réflexion, texte)
                              s'affiche avant elle, ce qui suit après ; les
                              puces arrivées d'affilée vont ensemble
  timeline(m)                 la réponse en CHRONOLOGIE pour l'affichage :
                              par tour (et par coupure d'outils hébergés),
                              sa réflexion, son texte, ses puces ;
                              le tour en cours (ou le texte final) en dernier
  closeStep(m, reason)        un tour laissé en plan (arrêt, coupure, page
                              rechargée) : chaque appel sans résultat en
                              reçoit un, d'erreur — l'échange reste
                              rejouable (un appel sans résultat serait
                              refusé par le modèle). Sans effet si une
                              carte attend.
*/
import { failure } from "./tools/contract.js";

/** @param {any} m @param {{id: string, name: string, arguments: string}} c */
export function chipFor(m, c) {
  let chip = m.tools.find((/** @type {any} */ t) => t.id === c.id);
  if (!chip) {
    m.tools.push({ id: c.id, name: c.name, arguments: c.arguments, status: "running" });
    chip = m.tools[m.tools.length - 1];
  } else if (chip.status === "writing") {
    Object.assign(chip, { arguments: c.arguments, status: "running" });
  }
  return chip;
}

/** @param {any} m @param {any} chip @param {any} c @param {string} result @param {string} [image] */
export function recordCall(m, chip, c, result, image = "") {
  Object.assign(chip, { status: result.startsWith("Error:") ? "error" : "done",
                        chars: result.length, preview: result.slice(0, 600) });
  m.step.calls.push({ id: c.id, name: c.name, arguments: c.arguments, result,
                      ...(image ? { image } : {}) });
  m.step.queue.shift();
}

/** @param {any} m @param {any} st */
export function pushStep(m, st) {
  (m.steps || (m.steps = [])).push({ content: st.content, calls: st.calls,
    reasoningEnd: (m.reasoning || "").length, contentEnd: (m.content || "").length,
    toolsEnd: (m.tools || []).length });
  m.step = null;
}

/** @param {any} m */
export function hostedCut(m) {
  const here = { reasoningEnd: (m.reasoning || "").length, contentEnd: (m.content || "").length,
                 toolsEnd: (m.tools || []).length };
  const cuts = m.cuts || (m.cuts = []);
  const last = cuts.at(-1);
  // Rien d'écrit ni d'autre puce depuis la dernière coupure : la même.
  if (last && last.reasoningEnd === here.reasoningEnd && last.contentEnd === here.contentEnd
      && last.toolsEnd === here.toolsEnd - 1) last.toolsEnd = here.toolsEnd;
  else cuts.push(here);
}

/** @param {any} m */
export function endStep(m) {
  pushStep(m, m.step);
  if (m.content && !m.content.endsWith("\n")) m.content += "\n\n";
}

/** @param {any} m @param {string} reason */
export function closeStep(m, reason) {
  const st = m.step;
  if (!st || m.waiting) return;
  const result = failure(reason);
  for (const q of st.queue || []) {
    if (st.calls.some((/** @type {any} */ x) => x.id === q.id)) continue;
    st.calls.push({ id: q.id, name: q.name, arguments: q.arguments, result });
    const chip = (m.tools || []).find((/** @type {any} */ t) => t.id === q.id);
    if (chip) Object.assign(chip, { status: "error", preview: result, chars: result.length });
  }
  pushStep(m, st);
}

// Où finit le texte de chaque tour, pour une réponse d'avant les positions :
// chaque texte de tour retrouvé à la suite du précédent ; null si l'un ne
// l'est pas.
/** @param {string} content @param {any[]} steps @returns {number[] | null} */
function locate(content, steps) {
  const ends = [];
  let at = 0;
  for (const st of steps) {
    const text = st.content || "";
    const pos = text ? content.indexOf(text, at) : at;
    if (pos < 0) return null;
    at = pos + text.length;
    ends.push(at);
  }
  return ends;
}

// Des positions (tours, coupures) qui tiennent : croissantes et dans les
// bornes.
/** @param {any[]} marks @param {number[]} sizes */
function ordered(marks, sizes) {
  return ["contentEnd", "reasoningEnd", "toolsEnd"].every((key, j) => {
    let prev = 0;
    return marks.every((st) => {
      const v = st[key];
      if (typeof v !== "number" || v < prev || v > sizes[j]) return false;
      prev = v;
      return true;
    });
  });
}

// Les positions notées par pushStep, si elles tiennent encore : croissantes,
// dans les bornes, et le texte de chaque tour à sa place (une réponse non
// diffusée remplace tout le texte).
/** @param {any[]} steps @param {string} content @param {number[]} sizes */
function positioned(steps, content, sizes) {
  let c0 = 0;
  return ordered(steps, sizes) && steps.every((st) => {
    const ok = content.slice(c0, st.contentEnd).trim() === (st.content || "").trim();
    c0 = st.contentEnd;
    return ok;
  });
}

// Le tour de chaque puce, sans positions : celui de ses appels ; une puce
// qui n'est dans aucun (un outil hébergé, exécuté par le proxy) va au tour
// de la puce qui la suit — elle est arrivée pendant son flux —, sinon au
// dernier segment.
/** @param {any[]} chips @param {any[]} steps @returns {number[]} */
function owners(chips, steps) {
  const owner = new Map();
  steps.forEach((st, k) => (st.calls || []).forEach((/** @type {any} */ c) => owner.set(c.id, k)));
  const out = new Array(chips.length);
  let next = steps.length;
  for (let j = chips.length - 1; j >= 0; j--) {
    if (owner.has(chips[j].id)) next = owner.get(chips[j].id);
    out[j] = next;
  }
  return out;
}

/** La réponse `m` en chronologie : `head`, une réflexion d'un seul tenant
 * (réponse d'avant les positions), et les segments — un par tour passé
 * et par coupure d'outils hébergés, puis le dernier (le tour en cours, ou le texte final) — chacun avec sa
 * réflexion, son texte (et où il commence dans `m.content`) et ses puces.
 * @param {any} m
 * @returns {{head: string, segments: {reasoning: string, content: string, tools: any[], start: number}[]}} */
export function timeline(m) {
  const reasoning = m.reasoning || "", content = m.content || "", chips = m.tools || [];
  const steps = m.steps || [];
  const sizes = [content.length, reasoning.length, chips.length];
  const whole = { head: "", segments: [{ reasoning, content, tools: chips, start: 0 }] };
  if (!steps.length && !(m.cuts || []).length) return whole;
  if (positioned(steps, content, sizes)) {
    // Les coupures des outils hébergés, entre les tours ; ignorées si elles
    // ne tiennent plus avec eux.
    const at = (/** @type {any} */ x) => [x.contentEnd, x.reasoningEnd, x.toolsEnd];
    const before = (/** @type {any} */ a, /** @type {any} */ b) =>
      at(a).reduce((d, v, j) => d || v - at(b)[j], 0);
    const merged = [...steps, ...(m.cuts || [])].sort(before);
    const marks = ordered(merged, sizes) ? merged : steps;
    const segments = [];
    const ends = [...marks, { contentEnd: content.length, reasoningEnd: reasoning.length, toolsEnd: chips.length }];
    let c0 = 0, r0 = 0, t0 = 0;
    for (const st of ends) {
      segments.push({ reasoning: reasoning.slice(r0, st.reasoningEnd), content: content.slice(c0, st.contentEnd),
                      tools: chips.slice(t0, st.toolsEnd), start: c0 });
      c0 = st.contentEnd; r0 = st.reasoningEnd; t0 = st.toolsEnd;
    }
    return { head: "", segments };
  }
  if (!steps.length) return whole;
  const ends = locate(content, steps);
  if (!ends) return whole;
  const own = owners(chips, steps);
  const segments = [];
  let c0 = 0;
  [...ends, content.length].forEach((end, k) => {
    segments.push({ reasoning: "", content: content.slice(c0, end),
                    tools: chips.filter((_, j) => own[j] === k), start: c0 });
    c0 = end;
  });
  return { head: reasoning, segments };
}
