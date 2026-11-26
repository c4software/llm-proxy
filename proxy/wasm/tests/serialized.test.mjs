// Les fonctions de la page envoyées en TEXTE (String(fn)) à un bac ou à un
// cadre isolé : les `driver` des bacs (tools/sandbox/bac.js les évalue dans
// un Worker), `tabular` (lib d'un bac) et le pont des artifacts
// (chat/artifacts.js, copié dans le document du cadre). Évaluée là-bas,
// une fonction ne voit rien de son module : elle doit se compiler seule et
// ne nommer que ses paramètres, ses variables et les globals du Worker ou
// du document. Une référence à une constante du module, ou une aide que le
// build y injecterait, casserait le bac à l'exécution seulement : vérifié
// sur la source, et sur le BUNDLE construit (tests/page-build.mjs), où
// chaque fonction est retrouvée par la plus longue de ses chaînes.
import { test } from "vitest";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import ts from "typescript";
import { buildPage } from "./page-build.mjs";

const STATIC = join(import.meta.dirname, "../../llm_proxy/ui/web/static");
const SERIALIZED = [
  ["chat/tools/sandbox/sql.js", "driver"],
  ["chat/tools/sandbox/postgres.js", "driver"],
  ["chat/tools/sandbox/php.js", "driver"],
  ["chat/tools/sandbox/javascript.js", "driver"],
  ["chat/tools/sandbox/typescript.js", "driver"],
  ["chat/tools/sandbox/programs.js", "driver"],
  ["chat/tools/sandbox/tables.js", "tabular"],
  ["chat/artifacts.js", "bridge"],
];
// Les globals d'un Worker ou d'un document que Node n'a pas.
const BROWSER = new Set(["self", "window", "document", "parent", "postMessage", "importScripts",
  "addEventListener", "getComputedStyle", "innerWidth", "innerHeight", "scrollX", "scrollY",
  "Element", "Event", "MouseEvent", "KeyboardEvent", "HTMLElement", "Node", "location", "navigator",
  "OffscreenCanvas"]);

/** Ce qui reconnaît une fonction dans le bundle, et que la minification
 * garde : ses chaînes (par leur texte : le build change leurs guillemets)
 * et les propriétés qu'elle lit (`mod.runPHP`). */
function marks(node) {
  const strings = [], props = new Set();
  const visit = (n) => {
    if (ts.isStringLiteral(n) || ts.isNoSubstitutionTemplateLiteral(n)) strings.push(n.text);
    if (ts.isPropertyAccessExpression(n)) props.add(n.name.text);
    ts.forEachChild(n, visit);
  };
  visit(node);
  return { strings, props };
}

const parse = (name, src) => ts.createSourceFile(name, src, ts.ScriptTarget.ES2022, true, ts.ScriptKind.JS);
const sourceFn = (file, name) => {
  const tree = parse(file, readFileSync(join(STATIC, file), "utf8"));
  return { tree, fn: tree.statements.find((s) => ts.isFunctionDeclaration(s) && s.name?.text === name) };
};

/** Les noms qu'une fonction lit sans les déclarer. @param {ts.FunctionDeclaration} fn */
function freeNames(fn) {
  const declared = new Set();
  const used = new Set();
  const bind = (name) => {
    if (ts.isIdentifier(name)) declared.add(name.text);
    else for (const el of name.elements) if (!ts.isOmittedExpression(el)) bind(el.name);
  };
  const visit = (node) => {
    if ((ts.isParameter(node) || ts.isVariableDeclaration(node) || ts.isBindingElement(node)) && node.name) bind(node.name);
    if ((ts.isFunctionDeclaration(node) || ts.isClassDeclaration(node) || ts.isFunctionExpression(node)) && node.name) declared.add(node.name.text);
    if (ts.isCatchClause(node) && node.variableDeclaration) bind(node.variableDeclaration.name);
    if (ts.isIdentifier(node)) {
      const p = node.parent;
      const isName = (ts.isPropertyAccessExpression(p) && p.name === node)
        || ((ts.isPropertyAssignment(p) || ts.isMethodDeclaration(p) || ts.isPropertyDeclaration(p)
             || ts.isGetAccessor(p) || ts.isSetAccessor(p)) && p.name === node)
        || (ts.isBindingElement(p) && p.propertyName === node)
        || ts.isLabeledStatement(p) || ts.isBreakOrContinueStatement(p);
      if (!isName) used.add(node.text);
    }
    ts.forEachChild(node, visit);
  };
  visit(fn);
  return [...used].filter((n) => !declared.has(n) && !(n in globalThis) && !BROWSER.has(n)
                                 && n !== "undefined" && n !== "arguments");
}

test("chaque fonction envoyée en texte se compile seule et ne nomme rien de son module", () => {
  for (const [file, name] of SERIALIZED) {
    const { tree, fn } = sourceFn(file, name);
    assert.ok(fn, `${file} : fonction ${name} introuvable`);
    const text = fn.getText(tree).replace(/^export\s+/, "");
    assert.equal(typeof new Function(`return (${text})`)(), "function", `${file} : ${name}`);
    assert.deepEqual(freeNames(fn), [], `${file} : ${name} nomme hors d'elle-même`);
  }
});

test("dans le bundle construit, chacune reste autonome", async () => {
  const { scripts } = await buildPage();
  const tops = Object.entries(scripts).flatMap(([f, src]) =>
    parse(f, src).statements.filter(ts.isFunctionDeclaration).map((fn) => ({ fn, ...marks(fn) })));
  for (const [file, name] of SERIALIZED) {
    const src = sourceFn(file, name).fn;
    const own = marks(src);
    const longest = own.strings.sort((a, b) => b.length - a.length)[0] || "";
    // Le build peut joindre des chaînes concaténées : la sienne la contient.
    const found = tops.filter((t) => t.fn.parameters.length === src.parameters.length
                                     && [...own.props].every((p) => t.props.has(p))
                                     && (!longest || t.strings.some((s) => s.includes(longest))));
    assert.equal(found.length, 1, `${file} : ${name} retrouvée ${found.length} fois dans le bundle`);
    assert.deepEqual(freeNames(found[0].fn), [], `${file} : ${name}, construite, nomme hors d'elle-même`);
  }
});

