// Les runtimes des bacs du navigateur, construits dans un dossier servi
// par la page (défaut : ../llm_proxy/web/static/vendor) :
//   php.js, php.wasm    PHP 8.3 (@php-wasm, GPL-2.0-or-later)
//   sql.js, sql.wasm    SQLite (sql.js, MIT)
// Usage : npm ci && npm run build [-- <dossier>]
import { build } from "esbuild";
import { copyFile, mkdir } from "node:fs/promises";
import { resolve } from "node:path";

const out = resolve(process.argv[2] || "../llm_proxy/web/static/vendor");
await mkdir(out, { recursive: true });

for (const name of ["php", "sql"]) {
  await build({
    entryPoints: [`${name}.js`], outfile: `${out}/${name}.js`,
    bundle: true, format: "esm", platform: "browser", minify: true, legalComments: "eof",
    // Les chemins de Node que le bundle ne prend jamais dans un navigateur.
    external: ["worker_threads", "events", "fs", "path", "crypto"],
    // Chargé depuis une URL blob:, sans base où résoudre un chemin
    // relatif ; le binaire est passé à part, cette adresse n'est pas lue.
    define: { "import.meta.url": JSON.stringify(`https://vendor.invalid/${name}.js`) },
  });
}
await copyFile("node_modules/@php-wasm/web-8-3/asyncify/8_3_33/php_8_3.wasm", `${out}/php.wasm`);
await copyFile("node_modules/sql.js/dist/sql-wasm-browser.wasm", `${out}/sql.wasm`);
await copyFile("node_modules/@php-wasm/universal/LICENSE", `${out}/LICENSE-php-wasm`);
await copyFile("node_modules/sql.js/LICENSE", `${out}/LICENSE-sql.js`);
console.log(`runtimes → ${out}`);
