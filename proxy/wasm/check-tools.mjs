// Les outils de la page (web/static/chat/tools), chargés hors du
// navigateur. Le registre (tools/index.js) vérifie à son chargement
// l'EXÉCUTION native : chaque outil contre son interface (PageTool), les
// listes TOOL et FAMILY dans les deux sens. Avec `--declarations
// <fichier>` (les DÉCLARATIONS du proxy, tools/page/, que pytest écrit :
// test_web_types.py), il les installe et exige que chaque déclaration ait
// son exécution, et chaque exécution native sa déclaration. Une dérive fait
// échouer ce script — et `npm run check`, et pytest.
//
// Les modules touchent au navigateur en se chargeant (Vue, document,
// navigator) : le minimum est simulé ici, rien n'est exécuté.
import { readFileSync, readdirSync } from "node:fs";

const noop = () => {};
globalThis.window = globalThis;
globalThis.document = { addEventListener: noop, hidden: false, createElement: () => ({}) };
globalThis.Vue = new Proxy({}, { get: () => (v) => ({ value: v, run: (f) => f() }) });
globalThis.VueUse = new Proxy({}, { get: () => () => new Proxy({}, { get: () => ({ value: false }) }) });
if (!globalThis.navigator) globalThis.navigator = {};
globalThis.location = { href: "http://localhost/ui" };
console.error = noop;                          // install() dit ses écarts : ils sont comptés ici

const tools = await import("../llm_proxy/ui/web/static/chat/tools/index.js");
const native = tools.BEHAVIORS.flatMap((f) => f.tools.map((t) => t.name));

const at = process.argv.indexOf("--declarations");
if (at > 0) {
  const page = JSON.parse(readFileSync(process.argv[at + 1], "utf8"));
  const errors = tools.install(page);
  const installed = new Set(tools.FAMILIES.flatMap((f) => f.tools.map((t) => t.name)));
  for (const n of native) if (!installed.has(n)) errors.push(`outil « ${n} » : exécuté, sans déclaration`);
  // Chaque `this.settings.<clé>` lue par l'exécution est déclarée par un
  // outil : une faute de frappe donnerait undefined, sans erreur.
  const declared = new Set(page.tools.flatMap((t) => Object.keys(t.settings || {})));
  const dir = new URL("../llm_proxy/ui/web/static/chat/tools/", import.meta.url);
  for (const file of readdirSync(dir, { recursive: true }).filter((f) => f.endsWith(".js"))) {
    const src = readFileSync(new URL(file, dir), "utf8");
    const keys = [...src.matchAll(/this\.settings\.(\w+)/g)].map((m) => m[1]);
    for (const m of src.matchAll(/\{([^{}]*)\}\s*=\s*this\.settings\b/g)) {
      keys.push(...m[1].split(",").map((k) => k.split(":")[0].trim()).filter(Boolean));
    }
    for (const k of keys) if (!declared.has(k)) errors.push(`${file} : settings « ${k} » non déclaré`);
  }
  if (errors.length) {
    process.stderr.write("Outils de la page, déclarations et exécution :\n  " + errors.join("\n  ") + "\n");
    process.exit(1);
  }
  console.log(`Outils de la page : ${tools.FAMILIES.length} familles, ${installed.size} outils — `
              + "déclarations et exécution conformes.");
} else {
  console.log(`Outils de la page : ${tools.BEHAVIORS.length} familles, ${native.length} outils — `
              + "exécution conforme (déclarations : pytest, test_web_types.py).");
}
