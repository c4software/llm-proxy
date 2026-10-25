// @ts-check
// Le texte à lire, commun aux moteurs : une réponse sans son Markdown,
// puis découpée en passages d'au plus `max` caractères, aux fins de phrase.

const MAX_SPEECH = 20_000;      // caractères lus au plus

/** Le texte d'une réponse, tel qu'on le lit : sans code, liens ni balises.
 * @param {string} markdown */
export function speakable(markdown) {
  const t = String(markdown || "")
    .replace(/```[\s\S]*?(```|$)/g, " (code) ")
    .replace(/`([^`]*)`/g, "$1")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/^#{1,6}\s+/gm, "").replace(/^\s*[-*+]\s+/gm, "").replace(/[*_~>|]/g, "")
    .replace(/\s+/g, " ").trim();
  return t.length > MAX_SPEECH ? t.slice(0, MAX_SPEECH) + "…" : t;
}

/** Le texte en passages : par phrases, regroupées jusqu'à `max` ; le
 * premier jusqu'à `first` seulement (il fait attendre la lecture).
 * @param {string} text @param {number} max @param {number} [first] */
export function sentences(text, max, first = max) {
  const out = [];
  let cur = "";
  const limit = () => (out.length ? max : first);
  for (const s of text.match(/[^.!?;:…]+[.!?;:…]*\s*/g) || [text]) {
    // Une « phrase » trop longue : coupée aux espaces.
    const parts = s.length <= max ? [s] : s.match(new RegExp(`.{1,${max}}(\\s|$)`, "g")) || [s];
    for (const p of parts) {
      if (cur && (cur + p).length > limit()) { out.push(cur.trim()); cur = ""; }
      cur += p;
    }
  }
  if (cur.trim()) out.push(cur.trim());
  return out;
}
