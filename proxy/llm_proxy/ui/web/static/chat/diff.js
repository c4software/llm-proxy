/*
  Un diff de lignes, pour montrer une écriture proposée dans le dossier
  ouvert : les lignes communes autour des changements (3 de contexte),
  les retirées (-) et les ajoutées (+). Plus long commun sous-suite, borné :
  au-delà, le fichier est montré remplacé en entier.
*/
const MAX_CELLS = 4e6;
const CONTEXT = 3;

// [{type: " " | "-" | "+" | "…", text}]
export function lineDiff(before, after) {
  const a = before === null ? [] : before.split("\n");
  const b = after.split("\n");
  let ops;
  if (a.length * b.length > MAX_CELLS) {
    ops = [...a.map((text) => ({ type: "-", text })), ...b.map((text) => ({ type: "+", text }))];
  } else {
    // Longueurs des plus longues sous-suites communes, depuis la fin.
    const n = a.length, m = b.length;
    const L = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
    for (let i = n - 1; i >= 0; i--) {
      for (let j = m - 1; j >= 0; j--) {
        L[i][j] = a[i] === b[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
      }
    }
    ops = [];
    let i = 0, j = 0;
    while (i < n && j < m) {
      if (a[i] === b[j]) { ops.push({ type: " ", text: a[i] }); i++; j++; }
      else if (L[i + 1][j] >= L[i][j + 1]) ops.push({ type: "-", text: a[i++] });
      else ops.push({ type: "+", text: b[j++] });
    }
    while (i < n) ops.push({ type: "-", text: a[i++] });
    while (j < m) ops.push({ type: "+", text: b[j++] });
  }
  // Le contexte : seules les lignes communes proches d'un changement.
  const near = ops.map((o, k) => o.type !== " " || ops.slice(Math.max(0, k - CONTEXT), k + CONTEXT + 1)
    .some((x) => x.type !== " "));
  const out = [];
  ops.forEach((o, k) => {
    if (near[k]) out.push(o);
    else if (out.length && out[out.length - 1].type !== "…") out.push({ type: "…", text: "" });
  });
  return out;
}

export const counts = (ops) => ({
  added: ops.filter((o) => o.type === "+").length,
  removed: ops.filter((o) => o.type === "-").length,
});
