// La page CONSTRUITE pour les tests (vite.config.mjs), dans un dossier
// temporaire : le build de vérification de `npm run check` ne touche pas à
// web/dist, que sert un proxy local. Rend le dossier et le texte de ses
// scripts ; le dossier est retiré à la fin du fichier de tests (afterAll).
import { mkdtempSync, readFileSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { build } from "vite";
import { afterAll } from "vitest";

let built = null;
let made = null;
// Enregistré au chargement (la collecte du fichier de tests), pas pendant
// un test : Vitest n'y prend plus de crochet.
afterAll(() => { if (made) rmSync(made, { recursive: true, force: true }); });

/** @returns {Promise<{ dir: string, scripts: Record<string, string> }>} */
export function buildPage() {
  if (!built) {
    built = (async () => {
      const dir = made = mkdtempSync(join(tmpdir(), "llm-proxy-page-"));
      await build({ configFile: join(import.meta.dirname, "../vite.config.mjs"), logLevel: "warn",
                    build: { outDir: dir, emptyOutDir: true } });
      const scripts = {};
      for (const f of readdirSync(join(dir, "assets")).filter((n) => n.endsWith(".js"))) {
        scripts[f] = readFileSync(join(dir, "assets", f), "utf8");
      }
      return { dir, scripts };
    })();
  }
  return built;
}
