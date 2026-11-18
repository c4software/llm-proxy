// @ts-check
/*
  Les TOURS D'OUTILS d'une réponse (ChatPanel.respond, et les modèles de
  l'appareil), sans Vue : l'état vit dans le message de la réponse —

    m.tools   les puces affichées (id, name, status, preview…)
    m.step    le tour en cours : {content, calls (faits), queue (à faire)}
    m.steps   les tours passés, rejoués au modèle (tools.stepMessages)
    m.waiting la carte d'un appel qui attend l'utilisateur

  chipFor(m, call)            la puce de l'appel (créée au besoin)
  recordCall(m, chip, call, result, image)  l'appel fait : sa puce, son
                              résultat au tour, retiré de la file
  endStep(m)                  le tour fini rejoint les tours passés
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

/** @param {any} m */
export function endStep(m) {
  (m.steps || (m.steps = [])).push({ content: m.step.content, calls: m.step.calls });
  m.step = null;
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
  (m.steps || (m.steps = [])).push({ content: st.content, calls: st.calls });
  m.step = null;
}
