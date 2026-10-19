// Les runtimes des bacs du navigateur, construits dans un dossier servi
// par la page (défaut : ../llm_proxy/ui/web/static/vendor) :
//   php.js, php.wasm    PHP 8.3 (@php-wasm, GPL-2.0-or-later)
//   sql.js, sql.wasm    SQLite (sql.js, MIT)
//   pglite.js, pglite.wasm, pglite.data, initdb.wasm
//                       PostgreSQL (PGlite, Apache-2.0 et licence PostgreSQL)
//   chart.js            Chart.js (MIT), pour le bac JavaScript
//   sucrase.js          sucrase (MIT) : TypeScript exécuté, types retirés
//   html-to-image.js    html-to-image (MIT) : la capture d'un artifact
//                       (inspect_artifact), passée à son cadre par la page
//   typescript.js       le compilateur TypeScript 6 et ses lib.*.d.ts
//                       (Apache-2.0) : la vérification des types
//   xterm.js, xterm.css xterm.js (MIT) : le terminal du panneau Linux
//   versions.json       l'empreinte de chacun : la page le demande avec
//                       ?v=<empreinte> — un cache devant elle (un proxy)
//                       ne peut pas servir un runtime périmé
// La VM Linux (vendor/linux/) a son étape à elle : proxy/linux/build.sh.
// Usage : npm ci && npm run build [-- <dossier>]
import { build } from "esbuild";
import { createHash } from "node:crypto";
import { copyFile, mkdir, readdir, readFile, stat, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

const out = resolve(process.argv[2] || "../llm_proxy/ui/web/static/vendor");
await mkdir(out, { recursive: true });

// Les bibliothèques standard de TypeScript, embarquées dans son bundle.
const tsLib = "node_modules/typescript/lib";
const libs = {};
for (const f of (await readdir(tsLib)).filter((f) => /^lib\..*\.d\.ts$/.test(f))) {
  libs[f] = await readFile(`${tsLib}/${f}`, "utf8");
}
await writeFile("ts-libs.json", JSON.stringify(libs));

// Un module de Node importé STATIQUEMENT (PGlite : fs, path), jamais
// appelé dans un navigateur : un module vide, sinon le bundle ne se charge
// pas. Ceux importés à la demande restent externes.
const nodeStatic = { name: "node-statique", setup(b) {
  b.onResolve({ filter: /^(fs|path)$/ }, (a) => (a.kind === "import-statement" ? { path: a.path, namespace: "vide" } : undefined));
  b.onLoad({ filter: /.*/, namespace: "vide" }, () => ({ contents: "export {}", loader: "js" }));
} };

for (const name of ["php", "sql", "pglite", "chart", "sucrase", "typescript", "xterm"]) {
  await build({
    entryPoints: [`${name}.js`], outfile: `${out}/${name}.js`,
    bundle: true, format: "esm", platform: "browser", minify: true, legalComments: "eof",
    plugins: [nodeStatic],
    // Les chemins de Node que le bundle ne prend jamais dans un navigateur.
    external: ["worker_threads", "events", "fs", "path", "crypto", "os", "inspector", "perf_hooks", "source-map-support", "module"],
    // Chargé depuis une URL blob:, sans base où résoudre un chemin
    // relatif ; le binaire est passé à part, cette adresse n'est pas lue.
    define: { "import.meta.url": JSON.stringify(`https://vendor.invalid/${name}.js`) },
  });
}
await copyFile("node_modules/@php-wasm/web-8-3/asyncify/8_3_33/php_8_3.wasm", `${out}/php.wasm`);
await copyFile("node_modules/sql.js/dist/sql-wasm-browser.wasm", `${out}/sql.wasm`);
for (const f of ["pglite.wasm", "pglite.data", "initdb.wasm"]) {
  await copyFile(`node_modules/@electric-sql/pglite/dist/${f}`, `${out}/${f}`);
}
await copyFile("node_modules/html-to-image/dist/html-to-image.js", `${out}/html-to-image.js`);
await copyFile("node_modules/@xterm/xterm/css/xterm.css", `${out}/xterm.css`);
await copyFile("node_modules/@php-wasm/universal/LICENSE", `${out}/LICENSE-php-wasm`);
await copyFile("node_modules/sql.js/LICENSE", `${out}/LICENSE-sql.js`);
await copyFile("node_modules/@electric-sql/pglite/LICENSE", `${out}/LICENSE-pglite`);
await copyFile("node_modules/chart.js/LICENSE.md", `${out}/LICENSE-chart.js`);
await copyFile("node_modules/sucrase/LICENSE", `${out}/LICENSE-sucrase`);
await copyFile("node_modules/html-to-image/LICENSE", `${out}/LICENSE-html-to-image`);
await copyFile("node_modules/typescript/LICENSE.txt", `${out}/LICENSE-typescript`);
await copyFile("node_modules/@xterm/xterm/LICENSE", `${out}/LICENSE-xterm`);
const versions = {};
for (const f of (await readdir(out)).sort()) {
  if (f === "versions.json" || !(await stat(`${out}/${f}`)).isFile()) continue;
  versions[f] = createHash("sha256").update(await readFile(`${out}/${f}`)).digest("hex").slice(0, 12);
}
await writeFile(`${out}/versions.json`, JSON.stringify(versions, null, 2) + "\n");
console.log(`runtimes → ${out}`);
