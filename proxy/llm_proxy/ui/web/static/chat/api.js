// @ts-check
/*
  Les MESSAGES d'une requête du chat, tels que l'API les veut — sans Vue,
  testés hors du navigateur (proxy/wasm/tests/api.test.mjs).

    apiContent(m, state)          un message de l'utilisateur : texte seul,
                                  ou parties (texte, fichiers, images)
    apiMessages(conv, upto, g)    l'échange jusqu'au message `upto` (exclu)
    requestTools(conv, ctx, o)    les outils d'une requête : hébergés, de
                                  la page, leurs consignes, leur état

  EN AJOUT SEUL (tools/contract.js) : une requête commence exactement
  comme la précédente — même prompt système, messages passés tels qu'ils
  ont été envoyés. L'état des outils, qui change, est gardé sur le message
  de l'utilisateur de son tour (`m.state`, fixé à l'envoi) et joint quand
  il diffère du précédent : rejoué à l'identique ensuite.
*/
import * as Compact from "./compact.js";
import { prompt } from "../lib/prompts.js";
import * as tools from "./tools/index.js";

/** Le message tel que l'API le veut : texte seul, ou parties (texte,
 * fichiers texte, images) quand il y a des pièces jointes. `state` : l'état
 * du moment des outils (artifacts, dossier, réseau de la VM), joint quand
 * il a changé depuis le tour précédent.
 * @param {any} m @param {string} [state] */
export function apiContent(m, state = "") {
  const files = (m.attachments || []).filter((/** @type {any} */ a) => a.kind === "text");
  const images = (m.attachments || []).filter((/** @type {any} */ a) => a.kind === "image" && a.data);
  let text = m.content || "";
  for (const f of files) {
    text += `\n\n<file name="${f.name.replace(/"/g, "'")}">\n${f.data}\n</file>`;
  }
  if (state) text += "\n\n" + prompt("messages.state", { state });
  if (!images.length) return text;
  return [
    ...(text ? [{ type: "text", text }] : []),
    ...images.map((/** @type {any} */ a) => ({ type: "image_url", image_url: { url: a.data } })),
  ];
}

/** `guidance` : les consignes des outils actifs (tools.guidance), à la
 * suite du prompt système de la conversation — figées. Après un RÉSUMÉ
 * (compact.js) : lui, au prompt système, et ce qui le suit — pas ce qui
 * le précède.
 * @param {any} conv @param {number} upto @param {string} [guidance] @returns {any[]} */
export function apiMessages(conv, upto, guidance = "") {
  const out = [];
  const from = Compact.start(conv, upto);
  const summary = from ? Compact.preamble(conv.messages[from - 1]) : "";
  const system = [(conv.system || "").trim(), summary, guidance].filter(Boolean).join("\n\n");
  if (system) out.push({ role: "system", content: system });
  let shown = "";
  for (const m of conv.messages.slice(from, upto)) {
    if (m.role === "summary") continue;
    if (m.role === "user") {
      const now = typeof m.state === "string" ? m.state : shown;
      const joined = now === shown ? "" : now || prompt("messages.nothing");
      shown = now;
      out.push({ role: "user", content: apiContent(m, joined) });
    }
    // Une réponse en erreur ou vide n'est pas renvoyée : le modèle ne
    // l'a jamais écrite.
    else if ((m.steps || []).length) {
      // Une réponse qui a appelé des outils de la page : ses appels et
      // leurs résultats, puis son dernier texte.
      out.push(...tools.stepMessages(m.steps));
      if (m.final) out.push({ role: "assistant", content: m.final });
    }
    else if (m.content) out.push({ role: "assistant", content: m.content });
    else if ((m.attachments || []).length) {
      out.push({ role: "assistant", content: prompt("messages.generated_image",
        { names: m.attachments.map((/** @type {any} */ a) => a.name).join(", ") }) });
    }
  }
  return out;
}

/** Les outils d'une requête. Incognito : rien de persistant, donc pas
 * l'outil `memory` ; ce que le modèle ne sait pas faire (`caps`) n'est pas
 * envoyé. Ceux de la page (tools/) : ceux des familles actives — le
 * dossier s'il est ouvert —, et leurs consignes au prompt système. Pose
 * `ctx.hosted` : les fonctions des outils hébergés présentés (`groups`,
 * ceux de /ui/session), ce qu'un programme du Code Mode peut appeler.
 * @param {any} conv @param {any} ctx
 * @param {{caps: any, available: Set<string>, families: string[],
 *   groups?: {type: string, tools: string[]}[]}} o */
export function requestTools(conv, ctx, { caps, available, families, groups = [] }) {
  const hosted = caps.hostedTools ? conv.tools.filter((/** @type {string} */ t) => available.has(t)
    && !(conv.incognito && t === "memory")) : [];
  ctx.hosted = groups.filter((g) => hosted.includes(g.type)).flatMap((g) => g.tools);
  if (!caps.folder) ctx.ws = null;
  const page = tools.specs(ctx, families)
    .filter((/** @type {any} */ t) => !caps.pageTools || caps.pageTools.includes(t.function.name));
  return { hosted, page, guidance: tools.guidance(ctx, families),
           state: tools.state(ctx, families) };
}
