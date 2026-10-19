// @ts-check
/*
  Le CONTEXTE d'une conversation : ce qu'il occupe chez le modèle, et son
  RÉSUMÉ (/compact, ou automatique près de la limite).

  Un résumé est un message de la conversation, `role: "summary"` : ce qui
  le précède reste affiché, mais ne part plus au modèle ; le résumé part à
  sa place, à la suite du prompt système (apiMessages, ChatPanel.js). Le
  retirer (« Annuler le résumé ») rend tout l'historique.

    start(conv, upto)      l'indice du premier message envoyé : après le
                           dernier résumé réussi (0 sans résumé)
    preamble(summary)      le résumé, tel qu'il part au prompt système
    occupancy(conv, max)   {tokens, max, ratio, measured, metered} : la
                           dernière mesure du modèle (usage), plus une
                           estimation de ce qui a suivi
    breakdown(conv, total, extra)  la répartition estimée du contexte, par
                           part (prompt système, outils, messages…)
    request(msgs, focus)   la demande de résumé, à la suite de l'échange
    lighten(msgs)          le même échange, allégé (chaque message coupé en
                           son milieu, les images retirées) : une seconde
                           demande, quand le contexte déborde déjà
    AUTO_AT                le seuil du résumé automatique (part du contexte)
*/
export const AUTO_AT = 0.8;
const CHARS_PER_TOKEN = 3.5;      // une estimation, en français comme en code
const IMAGE_TOKENS = 1000;        // une image jointe, à peu près

/** @param {any} m */
const isSummary = (m) => m && m.role === "summary";
/** @param {any} m */
const done = (m) => isSummary(m) && !m.running && !m.error && !!(m.content || "").trim();

/** @param {any} conv @param {number} upto */
export function start(conv, upto) {
  for (let i = Math.min(upto, conv.messages.length) - 1; i >= 0; i--) {
    if (done(conv.messages[i])) return i + 1;
  }
  return 0;
}

/** @param {any} conv @param {number} upto @returns {any | null} */
export function lastSummary(conv, upto) {
  const k = start(conv, upto);
  return k ? conv.messages[k - 1] : null;
}

/** @param {any} s */
export const preamble = (s) => "RÉSUMÉ DE LA CONVERSATION JUSQU'ICI (les messages antérieurs ne te sont "
  + "plus envoyés ; ce résumé les remplace, tu peux t'y fier) :\n\n" + s.content.trim();

// Les caractères d'un message tel qu'il part (à peu près).
/** @param {any} m */
function chars(m) {
  let n = (m.content || "").length;
  for (const a of m.attachments || []) n += a.kind === "text" ? (a.data || "").length : IMAGE_TOKENS * CHARS_PER_TOKEN;
  for (const st of m.steps || []) {
    n += (st.content || "").length;
    for (const c of st.calls || []) n += (c.arguments || "").length + (c.result || "").length;
  }
  return n;
}

/** Le contexte occupé : la dernière mesure du modèle (prompt + réponse de
 * son dernier tour), plus l'estimation de ce qui a suivi ; sans mesure
 * depuis le dernier résumé, tout est estimé. `measured` : rien d'estimé ;
 * `metered` : une mesure en fait partie (elle compte les outils déclarés).
 * @param {any} conv @param {number | null | undefined} max
 * @returns {{tokens: number, max: number, ratio: number, measured: boolean, metered: boolean}} */
export function occupancy(conv, max) {
  const msgs = conv.messages;
  const from = start(conv, msgs.length);
  let k = msgs.length - 1;
  let tokens = 0, measured = false;
  for (; k >= from; k--) {
    const m = msgs[k], u = m.usage;
    if (m.role === "assistant" && u && u.prompt_tokens) {
      tokens = u.prompt_tokens + (u.completion_tokens || 0);
      measured = true;
      break;
    }
  }
  let extra = 0;
  if (!measured) {
    k = from - 1;
    const s = lastSummary(conv, msgs.length);
    extra += (conv.system || "").length + (s ? preamble(s).length : 0);
  }
  for (let i = k + 1; i < msgs.length; i++) if (!isSummary(msgs[i])) extra += chars(msgs[i]);
  tokens += Math.round(extra / CHARS_PER_TOKEN);
  const cap = Number(max) || 0;
  return { tokens, max: cap, ratio: cap ? tokens / cap : 0, measured: measured && !extra, metered: measured };
}

const PARTS = {
  system: "Prompt système", summary: "Résumé", guidance: "Consignes des outils",
  tools: "Définitions des outils", messages: "Messages", calls: "Appels d'outils et résultats",
  images: "Images", hosted: "Outils hébergés (proxy)", other: "Non attribué",
};

/** La répartition du contexte, par part, estimée en caractères — les
 * outils hébergés comptés par le proxy (m.hidden). `total`
 * (une mesure du modèle), s'il est donné, borne le tout : ce que
 * l'estimation n'explique pas est « non attribué » (le gabarit du modèle,
 * l'écart de l'estimation) ; une estimation qui le dépasse y est ramenée.
 * `extra` : ce qui part hors du fil, les consignes des outils et leurs
 * définitions (du JSON).
 * @param {any} conv @param {number} total @param {{guidance?: string, tools?: string}} [extra]
 * @returns {{key: string, label: string, tokens: number}[]} */
export function breakdown(conv, total, extra = {}) {
  const msgs = conv.messages;
  const from = start(conv, msgs.length);
  const s = lastSummary(conv, msgs.length);
  /** @type {Record<string, number>} */
  const n = { system: (conv.system || "").trim().length, summary: s ? preamble(s).length : 0,
              guidance: (extra.guidance || "").length, tools: (extra.tools || "").length,
              messages: 0, calls: 0, images: 0, hosted: 0 };
  // Les échanges d'outils hébergés que le proxy a réinsérés, absents de la
  // page : comptés par lui (x_hidden) pour la requête mesurée.
  if (total) {
    for (let i = msgs.length - 1; i >= from; i--) {
      const m = msgs[i];
      if (m.role === "assistant" && m.usage && m.usage.prompt_tokens) { n.hosted = m.hidden || 0; break; }
    }
  }
  for (const m of msgs.slice(from)) {
    if (isSummary(m)) continue;
    for (const a of m.attachments || []) {
      if (a.kind === "text") n.messages += (a.data || "").length;
      else n.images += IMAGE_TOKENS * CHARS_PER_TOKEN;
    }
    if ((m.steps || []).length) {
      // Ce qui part d'une réponse à outils : ses tours, puis son dernier texte.
      for (const st of m.steps) {
        n.messages += (st.content || "").length;
        for (const c of st.calls || []) n.calls += (c.arguments || "").length + (c.result || "").length;
      }
      n.messages += (m.final || "").length;
    } else {
      n.messages += (m.content || "").length;
    }
  }
  const chars = Object.values(n).reduce((a, b) => a + b, 0);
  const guess = chars / CHARS_PER_TOKEN;
  const per = total && guess > total ? total / chars : 1 / CHARS_PER_TOKEN;
  if (total && guess < total) n.other = (total - guess) * CHARS_PER_TOKEN;
  return Object.entries(n).filter(([, c]) => c > 0)
    .map(([key, c]) => ({ key, label: PARTS[/** @type {keyof PARTS} */ (key)], tokens: Math.round(c * per) }));
}

/** Une estimation, en jetons, d'un texte. @param {string} text */
export const estimate = (text) => Math.round((text || "").length / CHARS_PER_TOKEN);

const INSTRUCTION = "Résume toute la conversation ci-dessus (y compris un éventuel résumé précédent "
  + "du prompt système) : ce résumé va la REMPLACER, et la suite de l'échange s'appuiera dessus "
  + "seul. Garde ce qui sert à continuer, de façon dense et factuelle :\n"
  + "- le but de l'utilisateur, ses demandes et ses préférences exprimées ;\n"
  + "- les décisions prises, les contraintes, ce qui a été écarté et pourquoi ;\n"
  + "- les faits établis, exacts : chiffres, noms, chemins de fichiers, identifiants, extraits "
  + "de code ou de commandes indispensables ;\n"
  + "- le travail fait : artifacts (par leur identifiant), fichiers écrits, résultats d'outils "
  + "qui comptent ;\n"
  + "- les questions ouvertes et la prochaine étape.\n"
  + "Écris dans la langue de la conversation, en Markdown sobre (titres courts, listes), sans "
  + "préambule ni conclusion, 700 mots au plus. N'appelle aucun outil.";

/** Les messages de la demande de résumé : l'échange, puis la consigne
 * (accolée au dernier message s'il est de l'utilisateur : certains
 * gabarits refusent deux messages de suite du même rôle).
 * @param {any[]} msgs @param {string} [focus] */
export function request(msgs, focus = "") {
  const ask = INSTRUCTION + (focus ? `\n\nEn priorité, selon l'utilisateur : ${focus}` : "");
  const out = msgs.slice();
  const last = out[out.length - 1];
  if (last && last.role === "user" && typeof last.content === "string") {
    out[out.length - 1] = { ...last, content: last.content + "\n\n---\n\n" + ask };
  } else {
    out.push({ role: "user", content: ask });
  }
  return out;
}

// Le début et la fin d'un long texte.
/** @param {string} t @param {number} n */
const clipMiddle = (t, n) => (t.length <= n ? t
  : t.slice(0, Math.ceil(n * 0.6)) + `\n[… ${t.length - n} caractères coupés …]\n` + t.slice(-Math.floor(n * 0.4)));

/** @param {any[]} msgs */
export function lighten(msgs) {
  return msgs.map((m) => {
    const cap = m.role === "tool" ? 600 : m.role === "system" ? 6000 : 2500;
    let c = m.content;
    if (Array.isArray(c)) c = c.filter((p) => p.type === "text").map((p) => p.text).join("\n") + " [image retirée]";
    return { ...m, content: typeof c === "string" ? clipMiddle(c, cap) : c };
  });
}

/** Un nombre de jetons, court : 950, 12,3 k, 131 k. @param {number} n */
export function kTokens(n) {
  if (n < 1000) return String(Math.round(n));
  const k = n / 1000;
  return (k < 100 ? k.toFixed(1).replace(".", ",").replace(/,0$/, "") : String(Math.round(k))) + " k";
}
