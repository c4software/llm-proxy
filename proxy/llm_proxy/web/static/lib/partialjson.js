/*
  Lire un JSON qui s'écrit encore (les arguments d'un appel d'outil en
  flux, la réponse JSON de Gemini Nano) : la valeur d'une chaîne, même
  inachevée.
*/
// La valeur de la chaîne `key` de `raw`, même inachevée ; « » si elle
// n'a pas commencé.
export function partialString(raw, key) {
  const at = raw.search(new RegExp(`"${key}"\\s*:\\s*"`));
  if (at < 0) return "";
  let k = raw.indexOf('"', raw.indexOf(":", at)) + 1;
  let out = "";
  for (; k < raw.length; k++) {
    const ch = raw[k];
    if (ch === '"') break;
    if (ch !== "\\") { out += ch; continue; }
    const nx = raw[k + 1];
    if (nx === undefined) break;
    if (nx === "u") {
      if (k + 5 >= raw.length) break;
      out += String.fromCharCode(parseInt(raw.slice(k + 2, k + 6), 16));
      k += 5;
    } else {
      out += { n: "\n", t: "\t", r: "", b: "", f: "" }[nx] ?? nx;
      k += 1;
    }
  }
  return out;
}
