// @ts-check
/*
  JavaScript : le code du modèle, corps d'une fonction async, exécuté dans
  le bac (bac.js) ; rend la valeur de son `return`, sa console, et les
  fichiers qu'il produit (files.js). `language: "typescript"` : ses types
  sont d'abord retirés (sucrase), sans être vérifiés — check_typescript
  (typescript.js) les vérifie. Le code dispose de :

    files                 {chemin: Uint8Array} des fichiers fournis
    readText(chemin)      l'un d'eux, en texte (UTF-8)
    readBytes(chemin)     l'un d'eux, en octets
    output(nom, données)  un fichier produit : texte, Uint8Array, Blob,
                          ou un OffscreenCanvas (en PNG)
    chart(config, {width, height, name})
                          un graphique Chart.js (chargé seulement s'il
                          sert), rendu en PNG — comme output
*/
import { TOOL, FAMILY, defineTool, defineFamily, clip, failure, oneLine } from "../contract.js";
import { exec } from "./bac.js";
import { FILES_PARAM, paths, inputs, deliver } from "./files.js";
import { checkTypescript } from "./typescript.js";

const TIMEOUT = 15_000;
const MAX_OUT = 20_000;

// Exécutée dans le Worker du bac : elle ne voit que ses arguments.
async function driver({ code, files, typescript }, { mods, fmt }) {
  const byName = Object.fromEntries(files.map((f) => [f.name, f.bytes]));
  function readBytes(path) {
    const p = String(path).replace(/^\.?\/+/, "");
    const hit = byName[p] ?? Object.entries(byName).find(([k]) => k.split("/").pop() === p)?.[1];
    if (!hit) throw new Error(`fichier « ${path} » non fourni (le passer dans \`files\`)`);
    return hit;
  }
  const readText = (path) => new TextDecoder().decode(readBytes(path));

  const produced = [];
  const pending = [];
  async function entry(name, data) {
    if (typeof data === "string") return { name, text: data };
    if (data instanceof Uint8Array) return { name, bytes: data };
    if (data instanceof ArrayBuffer) return { name, bytes: new Uint8Array(data) };
    if (typeof Blob !== "undefined" && data instanceof Blob) return { name, bytes: new Uint8Array(await data.arrayBuffer()) };
    if (typeof OffscreenCanvas !== "undefined" && data instanceof OffscreenCanvas) {
      return { name, bytes: new Uint8Array(await (await data.convertToBlob({ type: "image/png" })).arrayBuffer()) };
    }
    if (data !== null && typeof data === "object") return { name, text: JSON.stringify(data, null, 2) };
    throw new Error(`output(« ${name} ») : texte, Uint8Array, Blob ou OffscreenCanvas attendu`);
  }
  function output(name, data) {
    const i = produced.length;
    produced.push(null);
    const p = entry(String(name), data).then((e) => { produced[i] = e; });
    pending.push(p);
    return p;
  }

  let registered = false;
  async function chart(config, { width = 800, height = 500, name = "graphique.png" } = {}) {
    if (!mods.chart) throw new Error("Chart.js indisponible");
    const { Chart, registerables } = mods.chart;
    if (!registered) { Chart.register(...registerables); registered = true; }
    const canvas = new OffscreenCanvas(width, height);
    // Un fond blanc : un PNG transparent se lit mal sur un thème sombre.
    const background = { id: "fond", beforeDraw(c) {
      const x = c.ctx; x.save(); x.fillStyle = "#fff"; x.fillRect(0, 0, c.width, c.height); x.restore(); } };
    const c = new Chart(canvas, { ...config,
      options: { ...(config.options || {}), responsive: false, animation: false, devicePixelRatio: 1 },
      plugins: [...(config.plugins || []), background] });
    try { await output(name, canvas); } finally { c.destroy(); }
    return name;
  }

  let body = code;
  if (typescript) {
    // Le corps dans une fonction, le temps de retirer les types : un
    // `return` ou un `await` au premier niveau n'est pas un module valide.
    const wrapped = mods.sucrase.transform("async function __corps() {\n" + code + "\n}",
      { transforms: ["typescript"], disableESTransforms: true }).code;
    body = wrapped + "\nreturn __corps();";
  } else {
    body = "return (async () => {\n" + code + "\n})()";
  }
  const run = new Function("files", "readText", "readBytes", "output", "chart", body);
  const value = await run(byName, readText, readBytes, output, chart);
  await Promise.all(pending);
  return { value: value === undefined ? undefined : fmt(value), produced };
}

const runJavascript = defineTool({
  name: TOOL.RUN_JAVASCRIPT,
  description: "Exécute du JavaScript dans un bac isolé du navigateur (un Worker : aucun réseau, "
    + "aucun accès à la page ni au DOM), pour calculer exactement, transformer des données, "
    + "produire un fichier ou un graphique. Le code est le corps d'une fonction async : `return` "
    + "la valeur voulue ; console.log est rendu aussi. Fonctions fournies : readText(chemin) et "
    + "readBytes(chemin) lisent les fichiers passés dans `files` (dossier ouvert ou pièces jointes, "
    + "qui restent sur l'appareil) ; output(nom, données) produit un fichier sous la réponse (texte, "
    + "Uint8Array, Blob, OffscreenCanvas en PNG) ; chart(config, {width, height, name}) dessine un "
    + "graphique Chart.js 4 en PNG (config : {type, data, options}). Une image produite est "
    + "montrée à l'utilisateur et jointe au résultat si le modèle lit les images. "
    + "`language: \"typescript\"` accepte du TypeScript (types retirés, non vérifiés : "
    + "check_typescript les vérifie). 15 s au plus.",
  params: {
    code: { type: "string", description: "Le corps de la fonction (avec `return`)." },
    language: { type: "string", enum: ["javascript", "typescript"], description: "Le langage du code (défaut : javascript)." },
    ...FILES_PARAM,
  },
  required: ["code"],
  summary: (a) => oneLine([...(a.files || []), a.code].filter(Boolean).join(" · ")),
  async run(args, ctx) {
    const code = String(args.code || "");
    if (!code.trim()) return failure("`code` attendu.");
    let files;
    try { files = await inputs(ctx, paths(args)); } catch (e) { return failure(e.message + "."); }
    const typescript = args.language === "typescript";
    const runtime = [...(/\bchart\s*\(/.test(code) ? ["chart"] : []), ...(typescript ? ["sucrase"] : [])];
    const r = await exec({ driver, input: { code, files, typescript }, runtime, timeout: TIMEOUT });
    const out = [];
    let image = "";
    if (r.ok) {
      out.push(r.value.value === undefined ? "(aucune valeur rendue)" : "Résultat : " + r.value.value);
      const got = await deliver(ctx, r.value.produced.filter(Boolean));
      out.push(...got.lines);
      image = got.image;
    } else {
      out.push(failure(r.error));
    }
    if (r.logs.length) out.push("Console :\n" + r.logs.join("\n"));
    const result = clip(out.join("\n"), MAX_OUT);
    return image ? { result, image } : result;
  },
});

export const FAMILIES = [defineFamily({
  key: FAMILY.JAVASCRIPT, label: "JavaScript, TypeScript",
  hint: "du code dans un bac isolé, sans réseau : fichiers, graphiques ; vérifier les types",
  tools: [runJavascript, checkTypescript],
})];
