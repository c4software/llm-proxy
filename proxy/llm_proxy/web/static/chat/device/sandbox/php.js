// PHP : un script PHP 8.3 exécuté dans le bac (bac.js), en WASM — le
// runtime (≈ 18 Mo) n'est téléchargé qu'au premier appel. Rend la sortie,
// les erreurs et le code de sortie.
import { TOOL, FAMILY, defineTool, defineFamily, clip, failure, oneLine } from "../contract.js";
import { exec } from "./bac.js";

const TIMEOUT = 30_000;              // le premier appel compile le runtime
const MAX_OUT = 20_000;

// Exécutée dans le Worker du bac : elle ne voit que ses arguments.
async function driver(code, { mod, wasm }) {
  return mod.runPHP(code, wasm);
}

const runPhp = defineTool({
  name: TOOL.RUN_PHP,
  description: "Exécute un script PHP 8.3 (CLI, sans serveur web) dans un bac isolé du navigateur, "
    + "sans réseau ni fichiers de l'utilisateur : pour tester une fonction, montrer ce qu'affiche "
    + "du code PHP, calculer. Rend la sortie (echo, print_r, var_dump), les erreurs et le code de "
    + "sortie. Chaque appel part de zéro. 30 s au plus.",
  params: { code: { type: "string", description: "Le script, `<?php` au début (ajouté s'il manque)." } },
  required: ["code"],
  summary: (a) => oneLine(String(a.code || "").replace(/^\s*<\?php/, "")),
  async run(args) {
    let code = String(args.code || "");
    if (!code.trim()) return failure("`code` attendu.");
    if (!/^\s*<\?php/.test(code)) code = "<?php\n" + code;
    const r = await exec({ driver, input: code, runtime: "php", timeout: TIMEOUT });
    if (!r.ok) return failure(r.error);
    const { stdout, stderr, exitCode } = r.value;
    const out = [stdout ? "Sortie :\n" + stdout : "(aucune sortie)"];
    if (stderr) out.push("Erreurs :\n" + stderr);
    if (exitCode) out.push(`Code de sortie : ${exitCode}`);
    return clip(out.join("\n"), MAX_OUT);
  },
});

export const FAMILIES = [defineFamily({
  key: FAMILY.PHP, label: "PHP", hint: "PHP 8.3 en WASM, dans un bac isolé (≈ 18 Mo au premier appel)",
  available: () => typeof WebAssembly === "object",
  tools: [runPhp],
})];
