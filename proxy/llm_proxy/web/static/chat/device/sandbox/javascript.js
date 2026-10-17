// JavaScript : le code du modèle, corps d'une fonction async, exécuté dans
// le bac (bac.js) ; rend la valeur de son `return` et sa console.
import { TOOL, FAMILY, defineTool, defineFamily, clip, failure, oneLine } from "../contract.js";
import { exec } from "./bac.js";

const TIMEOUT = 10_000;
const MAX_OUT = 20_000;

// Exécutée dans le Worker du bac : elle ne voit que ses arguments.
async function driver(code, { fmt }) {
  const value = await new Function("return (async () => {\n" + code + "\n})()")();
  return value === undefined ? undefined : fmt(value);
}

const runJavascript = defineTool({
  name: TOOL.RUN_JAVASCRIPT,
  description: "Exécute du JavaScript dans un bac isolé du navigateur (aucun réseau, aucun accès "
    + "à la page ni au DOM), pour calculer exactement : arithmétique, dates, statistiques, "
    + "transformation de données. Le code est le corps d'une fonction async : `return` la valeur "
    + "voulue ; console.log est rendu aussi. 10 s au plus.",
  params: { code: { type: "string", description: "Le corps de la fonction (avec `return`)." } },
  required: ["code"],
  summary: (a) => oneLine(a.code),
  async run(args) {
    const code = String(args.code || "");
    if (!code.trim()) return failure("`code` attendu.");
    const r = await exec({ driver, input: code, timeout: TIMEOUT });
    const out = [r.ok ? (r.value === undefined ? "(aucune valeur rendue)" : "Résultat : " + r.value)
                      : failure(r.error)];
    if (r.logs.length) out.push("Console :\n" + r.logs.join("\n"));
    return clip(out.join("\n"), MAX_OUT);
  },
});

export const FAMILIES = [defineFamily({
  key: FAMILY.JAVASCRIPT, label: "JavaScript", hint: "du code exécuté dans un bac isolé, sans réseau",
  tools: [runJavascript],
})];
