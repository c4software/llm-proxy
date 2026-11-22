// @ts-check
// PHP : un script PHP 8.3 exécuté dans le bac (bac.js), en WASM — le
// runtime (≈ 18 Mo) n'est téléchargé qu'au premier appel. Les fichiers de
// `files` sont posés sous /data, le dossier courant ; ce que le script
// écrit sous /out est rendu sous la réponse (files.js). Rend la sortie,
// les erreurs et le code de sortie.
import { TOOL, FAMILY, PageTool, defineFamily, clip, failure, oneLine } from "../contract.js";
import { exec } from "./bac.js";
import { paths, inputs, deliver } from "./files.js";

const MAX_OUT = 20_000;

// Exécutée dans le Worker du bac : elle ne voit que ses arguments.
async function driver({ code, files }, { mod, wasm }) {
  return mod.runPHP(code, wasm, files);
}

class RunPhp extends PageTool {
  constructor() {
    super({ name: TOOL.RUN_PHP });
  }

  /** @override @param {any} a */
  summary(a) { return oneLine([...(a.files || []), String(a.code || "").replace(/^\s*<\?php/, "")].filter(Boolean).join(" · ")); }

  /** @override @param {any} args @param {import("../contract.js").Ctx} ctx */
  async run(args, ctx) {
    let code = String(args.code || "");
    if (!code.trim()) return failure("`code` attendu.");
    if (!/^\s*<\?php/.test(code)) code = "<?php\n" + code;
    let files;
    try { files = await inputs(ctx, paths(args)); } catch (e) { return failure(e.message + "."); }
    const r = await exec({ driver, input: { code, files }, runtime: "php", timeout: this.settings.timeout * 1000, signal: ctx.signal });
    if (!r.ok) return failure(r.error);
    const { stdout, stderr, exitCode, outputs } = r.value;
    const out = [stdout ? "Sortie :\n" + stdout : "(aucune sortie)"];
    if (stderr) out.push("Erreurs :\n" + stderr);
    if (exitCode) out.push(`Code de sortie : ${exitCode}`);
    const got = await deliver(ctx, (outputs || []).map((f) => ({ name: f.name, bytes: new Uint8Array(f.bytes) })));
    out.push(...got.lines);
    const result = clip(out.join("\n"), MAX_OUT);
    return got.image ? { result, image: got.image } : result;
  }
}

export const FAMILIES = [defineFamily({
  key: FAMILY.PHP,
  available: () => typeof WebAssembly === "object",
  tools: [new RunPhp()],
})];
