// @ts-check
/*
  Lire, chercher, RETOUCHER un texte — le même chemin pour les artifacts
  (page/artifacts.js : grep_artifact, read_artifact, edit_artifact) et
  pour les fichiers de la VM Linux (sandbox/linux.js : shell_grep,
  shell_read, shell_edit) : grep (où ?), une tranche de lignes numérotées
  (le texte exact), puis des remplacements de passages exacts (ou d'une
  expression régulière), tous ou aucun.

  Les messages nomment les outils de l'appelant (`names`) : le modèle lit
  quoi faire ensuite.
*/

/** Les outils et le lieu nommés dans les messages.
 * @typedef {{grep: string, read: string, at: string}} Names
 *   at : « dans la version 3 », « dans /work/app.py » */

export const MAX_LINE = 300;           // caractères d'une ligne rendue par grep
export const MAX_HITS = 60;            // lignes trouvées par grep, au plus
export const MAX_SLICE = 400;          // lignes d'une tranche

/** Une ligne numérotée : `   42→ texte` (le numéro ne fait pas partie du texte).
 * @param {number} n @param {string} line @param {string} [mark] */
export const numbered = (n, line, mark = "→") => `${String(n).padStart(5)}${mark} ${line}`;

const plural = (/** @type {number} */ k, /** @type {string} */ w) => `${k} ${w}${k > 1 ? "s" : ""}`;

/**
 * Les retouches (`edits` : [{old_string, new_string, replace_all, regex,
 * ignore_case}]) appliquées à `content`, toutes ou aucune.
 * @param {string} content @param {any[]} edits @param {Names} names
 * @returns {{content: string, passages: number} | {error: string}}
 */
export function applyEdits(content, edits, names) {
  if (!edits.length) return { error: "Error: `edits` attendu, au moins une retouche." };
  let passages = 0;                      // passages remplacés, toutes retouches
  for (const [k, e] of edits.entries()) {
    const from = String((e && e.old_string) ?? "");
    const to = String((e && e.new_string) ?? "");
    const where = edits.length > 1 ? `retouche ${k + 1} : ` : "";
    if (!from) return { error: `Error: ${where}\`old_string\` est vide.` };
    if (e.regex) {
      // Une expression régulière (JavaScript) ; `new_string` peut citer
      // ses groupes ($1, $<nom>). Unique, sauf replace_all.
      let re;
      try { re = new RegExp(from, "g" + (e.ignore_case ? "i" : "") + "m"); } catch (err) {
        return { error: `Error: ${where}expression invalide : ${err.message}. Rien n'a été modifié.` };
      }
      const hits = [...content.matchAll(re)].length;
      if (!hits) {
        return { error: `Error: ${where}l'expression ne trouve rien ${names.at} `
          + `(${names.grep} pour vérifier). Rien n'a été modifié.` };
      }
      if (hits > 1 && !e.replace_all) {
        return { error: `Error: ${where}l'expression trouve ${hits} passages ; la préciser, ou \`replace_all\`. `
          + "Rien n'a été modifié." };
      }
      content = content.replace(re, to);
      passages += e.replace_all ? hits : 1;
      continue;
    }
    const count = content.split(from).length - 1;
    if (!count) {
      return { error: `Error: ${where}passage introuvable ${names.at}. `
        + `Le texte doit être EXACT (espaces, retours à la ligne) ; ${names.grep} pour le retrouver, `
        + `${names.read} sur ses lignes pour le relire. Rien n'a été modifié.` };
    }
    if (count > 1 && !e.replace_all) {
      return { error: `Error: ${where}passage présent ${count} fois ; l'allonger pour qu'il soit `
        + "unique, ou `replace_all`. Rien n'a été modifié." };
    }
    content = e.replace_all ? content.split(from).join(to) : content.replace(from, () => to);
    passages += e.replace_all ? count : 1;
  }
  return { content, passages };
}

/** « (2 retouches, 3 passages remplacés) ». @param {number} edits @param {number} passages */
export const editCount = (edits, passages) =>
  `(${plural(edits, "retouche")}, ${plural(passages, "passage")} remplacé${passages > 1 ? "s" : ""})`;

/**
 * Une tranche de lignes numérotées (`start`, `end` : du modèle, rien de
 * validé ; 80 lignes par défaut, MAX_SLICE au plus).
 * @param {string} content @param {any} start @param {any} end
 * @returns {{total: number, start: number, end: number, body: string} | {error: string}}
 */
export function slice(content, start, end) {
  const lines = content.split("\n");
  const s = Math.max(1, Math.floor(Number(start) || 1));
  if (s > lines.length) return { error: `n'a que ${lines.length} lignes.` };
  const e = Math.min(lines.length, Math.floor(Number(end) || s + 80), s + MAX_SLICE - 1);
  return { total: lines.length, start: s, end: e,
           body: lines.slice(s - 1, e).map((l, k) => numbered(s + k, l)).join("\n") };
}

/** La recherche : `pattern` en texte, ou en expression régulière (`regex`) ;
 * sans tenir compte de la casse, sauf `ignore_case: false`.
 * @param {any} args @returns {{re: RegExp} | {error: string}} */
export function pattern(args) {
  const p = String(args.pattern || "");
  if (!p) return { error: "`pattern` attendu." };
  try {
    return { re: new RegExp(args.regex ? p : p.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), args.ignore_case === false ? "" : "i") };
  } catch (e) { return { error: `expression invalide : ${e.message}` }; }
}

/** Le contexte demandé (défaut 2, 10 au plus). @param {any} args */
export const contextOf = (args) => Math.max(0, Math.min(10, Math.floor(Number(args.context ?? 2))));

/**
 * Les lignes de `content` trouvées par `re`, numérotées, avec `ctx`
 * lignes autour (« → » : trouvée ; « · » : contexte ; « … » : saut), au
 * plus `max` trouvées.
 * @param {string} content @param {RegExp} re @param {number} ctx @param {number} [max]
 * @returns {{total: number, hits: number, shown: number, lines: string[]}}
 */
export function grep(content, re, ctx, max = MAX_HITS) {
  const lines = content.split("\n");
  const hits = [];
  lines.forEach((l, i) => { if (re.test(l)) hits.push(i); });
  const shown = hits.slice(0, max);
  const keep = new Map();                    // ligne → trouvée ?
  for (const h of shown) {
    for (let i = Math.max(0, h - ctx); i <= Math.min(lines.length - 1, h + ctx); i++) keep.set(i, keep.get(i) || i === h);
  }
  const out = [];
  let prev = -2;
  for (const i of [...keep.keys()].sort((a, b) => a - b)) {
    if (prev >= 0 && i > prev + 1) out.push("    …");
    const l = lines[i].length > MAX_LINE ? lines[i].slice(0, MAX_LINE) + " …" : lines[i];
    out.push(numbered(i + 1, l, keep.get(i) ? "→" : "·"));
    prev = i;
  }
  return { total: lines.length, hits: hits.length, shown: shown.length, lines: out };
}

/** « 3 lignes trouvées (les 60 premières) ». @param {{hits: number, shown: number}} g */
export const found = (g) => `${g.hits} ligne${g.hits > 1 ? "s" : ""} trouvée${g.hits > 1 ? "s" : ""}`
  + (g.hits > g.shown ? ` (les ${g.shown} premières)` : "");
