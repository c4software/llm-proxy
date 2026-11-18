// Chaque gabarit de composant de la page (`template: \`…\``) se compile
// avec le VRAI Vue, compilateur du navigateur compris : une expression
// qu'il refuse (un `if` dans un @change, que Vue enveloppe en
// `$event => (…)`) laissait sinon une vue entière blanche, sans qu'aucun
// test ne le voie.
import "./setup.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

// Le compilateur décode les entités par un <div> : de quoi lui répondre.
const ENTITIES = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " " };
const decode = (s) => s.replace(/&(#x?[0-9a-f]+|\w+);/gi, (all, e) =>
  e[0] === "#" ? String.fromCodePoint(e[1] === "x" || e[1] === "X" ? parseInt(e.slice(2), 16) : +e.slice(1))
    : ENTITIES[e] ?? all);
document.createElement = () => {
  let html = "";
  return {
    set innerHTML(v) { html = v; },
    get textContent() { return decode(html); },
    get children() {
      const m = /^<div foo="([\s\S]*)">/.exec(html);
      return [{ getAttribute: () => decode(m ? m[1] : "") }];
    },
  };
};

const STATIC = new URL("../../llm_proxy/ui/web/static/", import.meta.url).pathname;
function* files(dir) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) { if (name !== "vendor") yield* files(path); }
    else if (name.endsWith(".js") && !name.endsWith(".min.js") && !name.startsWith("vue")) yield path;
  }
}

const templates = [];
for (const path of files(STATIC)) {
  const src = readFileSync(path, "utf8");
  for (const m of src.matchAll(/template:\s*`((?:\\[\s\S]|[^`\\])*)`/g)) {
    // Le littéral tel que JavaScript le lit (échappements compris) : le
    // code de la page, sans `${}` dans ses gabarits.
    const text = new Function("return `" + m[1] + "`")();
    templates.push([path.slice(STATIC.length), src.slice(0, m.index).split("\n").length, text]);
  }
}

test("des gabarits sont trouvés", () => assert.ok(templates.length >= 10));

for (const [file, line, template] of templates) {
  test(`${file}:${line} se compile`, () => {
    const errors = [];
    try {
      Vue.compile(template, { onError: (e) => errors.push(e.message) });
    } catch (e) { errors.push(e.message); }
    assert.deepEqual(errors, []);
  });
}
