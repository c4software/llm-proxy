// Le minimum du navigateur pour charger les modules de la page hors de
// lui (comme check-tools.mjs), avec le VRAI Vue : rien n'est affiché.
import { readFileSync } from "node:fs";
import vm from "node:vm";

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
