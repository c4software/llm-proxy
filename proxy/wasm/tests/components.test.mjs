// Les composants .vue de la page ne s'exportent que PAR DÉFAUT : dans le
// build de production, la fonction de rendu (le <template> compilé) n'est
// attachée qu'à l'export par défaut — un composant importé sous son nom
// (`import { X } from "./X.vue"`) s'affiche vide, sans erreur, alors que
// les tests (compilation de développement) passent. Et le Vue de la page
// embarque son compilateur : la carte d'une extension, chargée à
// l'exécution, peut donner un `template` texte (docs/extensions.md).
import { test } from "vitest";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { createServer } from "vite";
import { buildPage } from "./page-build.mjs";

const STATIC = join(import.meta.dirname, "../../llm_proxy/ui/web/static");

test("un composant .vue ne s'exporte que par défaut", () => {
  const files = readdirSync(STATIC, { recursive: true })
    .filter((f) => f.endsWith(".vue") && !f.startsWith("vendor"));
  assert.ok(files.length >= 25, "composants trouvés");
  for (const file of files) {
    const src = readFileSync(join(STATIC, file), "utf8");
    const name = (/^export default (\w+);$/m.exec(src) || [])[1];
    if (!name) continue;                              // un gabarit seul (Loading.vue)
    assert.doesNotMatch(src, new RegExp(`^export (const|let|function) ${name}\\b|^export \\{[^}]*\\b${name}\\b`, "m"),
                        `${file} : ${name} exporté sous son nom`);
  }
});

test("le Vue de la page compile un gabarit texte (la carte d'une extension)", async () => {
  // `vue` tel que le build de la page le résout (vite.config.mjs), depuis
  // son entrée — pas celui que Node trouverait.
  const vite = await createServer({ configFile: join(import.meta.dirname, "../vite.config.mjs"),
                                    server: { middlewareMode: true, hmr: false }, logLevel: "silent",
                                    optimizeDeps: { noDiscovery: true, include: [] } });
  try {
    const got = await vite.environments.client.pluginContainer.resolveId("vue", join(STATIC, "main.js"));
    const vue = await import(pathToFileURL(got.id).href);
    const render = vue.compile(`<p class="ext">{{ n * 2 }} <b v-if="n">!</b></p>`);
    assert.equal(typeof render, "function", `${got.id} : sans compilateur de gabarits`);
  } finally {
    await vite.close();
  }
});

test("les fichiers que la page sert aux clients sont dans le build", async () => {
  // Les extensions TypeScript de pi, omp et OpenCode, les instructions de
  // base de Codex (Configuration → Clients) : dans un script, ou en ressource.
  const { dir, scripts } = await buildPage();
  const assets = readdirSync(join(dir, "assets"));
  const files = readdirSync(join(STATIC, "config")).filter((f) => /\.(ts|txt)$/.test(f));
  assert.ok(files.length >= 3, "fichiers trouvés");
  for (const f of files) {
    const head = readFileSync(join(STATIC, "config", f), "utf8").split("\n").find((l) => l.trim().length > 20);
    const stem = f.replace(/\.[^.]+$/, "");
    const inScript = Object.values(scripts).some((js) => js.includes(JSON.stringify(head).slice(1, -1))
                                                         || js.includes(head));
    assert.ok(inScript || assets.some((a) => a.startsWith(stem + "-")), `${f} absent du build`);
  }
});

