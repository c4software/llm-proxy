// Le minimum du navigateur pour charger les modules de la page hors de
// lui (comme check-tools.mjs), avec le VRAI Vue : rien n'est affiché.
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { parse as parseToml } from "smol-toml";
import { install as installPrompts } from "../../llm_proxy/ui/web/static/lib/prompts.js";

export const listeners = {};
globalThis.window = globalThis;
globalThis.document = { addEventListener: (t, f) => { listeners[t] = f; }, hidden: false,
  visibilityState: "visible", createElement: () => ({}) };
globalThis.VueUse = new Proxy({}, { get: () => () => new Proxy({}, { get: () => ({ value: false }) }) });
if (!globalThis.navigator) globalThis.navigator = {};
globalThis.location = { href: "http://localhost/ui" };
if (!globalThis.Vue) {
  vm.runInThisContext(readFileSync(new URL("../../llm_proxy/ui/web/static/vue.global.prod.js", import.meta.url), "utf8")
    + "\nglobalThis.Vue = Vue;");
}

// Les prompts de la page (lib/prompts.js) : ceux du VRAI catalogue du proxy,
// comme /ui/session les donne (prompts.for_page : le domaine chat, l'état
// des familles).

const catalog = (name) => parseToml(readFileSync(new URL(`../../llm_proxy/prompts/${name}.toml`, import.meta.url), "utf8"));
/** @type {Record<string, string>} */
export const PAGE_PROMPTS = {};
for (const [subject, entries] of Object.entries(catalog("chat"))) {
  for (const [name, text] of Object.entries(entries)) PAGE_PROMPTS[`${subject}.${name}`] = text;
}
for (const [subject, entries] of Object.entries(catalog("page"))) {
  for (const [name, text] of Object.entries(entries)) {
    if (subject.startsWith("family.") && name.startsWith("state")) PAGE_PROMPTS[`${subject}.${name}`] = text;
  }
}
installPrompts(PAGE_PROMPTS);
