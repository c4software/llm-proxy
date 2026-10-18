// Le REGISTRE des outils de la page (web/static/chat/tools), chargé hors
// du navigateur : il vérifie au chargement chaque outil contre son
// interface (PageTool) et les listes TOOL et FAMILY dans les deux sens
// (tools/index.js). Une dérive fait échouer ce script — et `npm run
// check`, et pytest.
//
// Les modules touchent au navigateur en se chargeant (Vue, document,
// navigator) : le minimum est simulé ici, rien n'est exécuté.
const noop = () => {};
globalThis.window = globalThis;
globalThis.document = { addEventListener: noop, hidden: false, createElement: () => ({}) };
globalThis.Vue = new Proxy({}, { get: () => (v) => ({ value: v, run: (f) => f() }) });
globalThis.VueUse = new Proxy({}, { get: () => () => new Proxy({}, { get: () => ({ value: false }) }) });
if (!globalThis.navigator) globalThis.navigator = {};
globalThis.location = { href: "http://localhost/ui" };

const tools = await import("../llm_proxy/ui/web/static/chat/tools/index.js");
const names = tools.FAMILIES.flatMap((f) => f.tools.map((t) => t.name));

// Le MANIFESTE (famille → outils), que lit le proxy pour [ui].page_tools_disabled
// (ui/page_tools.py) : tiré du registre, jamais écrit à la main. Périmé, ce
// script échoue ; `node check-tools.mjs --write` le réécrit.
import { readFileSync, writeFileSync } from "node:fs";
const MANIFEST = new URL("../llm_proxy/ui/page_tools.json", import.meta.url);
const manifest = JSON.stringify({ families: Object.fromEntries(tools.FAMILIES.map((f) => [f.key, {
  label: f.label, fixed: !!f.fixed, tools: f.tools.map((t) => t.name) }])) }, null, 2) + "\n";
let current = "";
try { current = readFileSync(MANIFEST, "utf8"); } catch { /* absent */ }
if (process.argv.includes("--write")) {
  writeFileSync(MANIFEST, manifest);
  console.log("Manifeste réécrit : llm_proxy/ui/page_tools.json");
} else if (current !== manifest) {
  console.error("Outils de la page : llm_proxy/ui/page_tools.json est périmé — `node check-tools.mjs --write` (proxy/wasm)");
  process.exit(1);
}
console.log(`Outils de la page : ${tools.FAMILIES.length} familles, ${names.length} outils — conformes.`);
