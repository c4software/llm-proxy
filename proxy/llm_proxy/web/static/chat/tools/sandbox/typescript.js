// @ts-check
/*
  TypeScript : la VÉRIFICATION des types (check_typescript), dans le bac
  (bac.js), par le compilateur TypeScript 6 (vendor/typescript.js, ≈ 7 Mo,
  téléchargé au premier appel). Rien n'est exécuté : run_javascript avec
  `language: "typescript"` exécute.

  Le projet vient du dossier ouvert : ses fichiers .ts, .tsx, .mts, .cts
  (pour résoudre les imports relatifs) et son tsconfig.json s'il en a un
  à la racine. Ne sont vérifiés que ceux que `files` désigne (un fichier,
  ou un sous-dossier : tout ce qu'il contient). Pas de node_modules : un
  module de paquet vaut `any`. `code` vérifie un texte À LA PLACE d'un
  fichier (`path`) — une modification avant de l'écrire.
*/
import { TOOL, PageTool, clip, failure, oneLine } from "../contract.js";
import { exec } from "./bac.js";
import { paths as pathsOf, inputs } from "./files.js";

const TIMEOUT = 60_000;              // le premier appel charge le compilateur
const MAX_OUT = 20_000;
const MAX_PROJECT = 3000;            // fichiers TypeScript du dossier, au plus
const MAX_BYTES = 30e6;
const TS = /\.(ts|tsx|mts|cts)$/;

// Exécutée dans le Worker du bac : elle ne voit que ses arguments.
async function driver(input, { mod }) {
  return mod.check(input);
}

const clean = (p) => String(p || "").trim().replace(/^\.?\/+/, "").replace(/\/+$/, "");

// Le projet ({chemin: texte}), les fichiers à vérifier, le tsconfig.
async function project(ctx, wanted, override) {
  const ws = ctx.ws;
  const files = {};
  let bytes = 0;
  if (ws) {
    const all = [...ws.files.keys()].filter((p) => TS.test(p));
    if (all.length > MAX_PROJECT) throw new Error(`plus de ${MAX_PROJECT} fichiers TypeScript dans le dossier`);
    for (const p of all) {
      bytes += ws.files.get(p).size;
      if (bytes > MAX_BYTES) throw new Error(`plus de ${MAX_BYTES / 1e6} Mo de TypeScript dans le dossier`);
      files[p] = await ws.files.get(p).text();
    }
  }
  // Tout le dossier, ou un sous-dossier, sans dossier ouvert : rien à
  // chercher — le dire, et comment vérifier un extrait.
  // Avec un extrait (`code`), ces chemins-là sont ignorés.
  if (!ws) {
    const folderish = (w) => w === "" || !/\.[cm]?tsx?$/.test(w);
    if (!override && wanted.some(folderish)) {
      throw new Error("aucun dossier ouvert dans la conversation ; pour vérifier un extrait de code, "
        + "le passer dans `code` (sans `files`)");
    }
    wanted = wanted.filter((w) => !folderish(w));
  }
  const roots = new Set();
  const missing = [];
  for (const w of wanted) {
    if (w in files) { roots.add(w); continue; }
    const under = Object.keys(files).filter((p) => !w || p.startsWith(w + "/"));
    if (under.length) { under.forEach((p) => roots.add(p)); continue; }
    missing.push(w);
  }
  // Un fichier hors du dossier : une pièce jointe.
  if (missing.length) {
    for (const f of await inputs(ctx, missing)) {
      files[f.name] = new TextDecoder().decode(f.bytes);
      roots.add(f.name);
    }
  }
  if (override) { files[override.path] = override.code; roots.add(override.path); }
  const tsconfig = ws && ws.files.has("tsconfig.json") ? await ws.files.get("tsconfig.json").text() : "";
  return { files, roots: [...roots], tsconfig };
}

export class CheckTypescript extends PageTool {
  constructor() {
    super({
      name: TOOL.CHECK_TYPESCRIPT,
      agent: true,
      description: "Vérifie les types de fichiers TypeScript du dossier ouvert (ou de pièces jointes) "
        + "avec le compilateur TypeScript 6, dans un bac isolé du navigateur, sans rien exécuter. "
        + "Le tsconfig.json à la racine du dossier est appliqué (sinon : strict, ES2022, modules "
        + "ESNext) ; les imports relatifs sont résolus dans le dossier ; les paquets (pas de "
        + "node_modules) valent `any`. Rend les erreurs « fichier(ligne,colonne): error TSxxxx: "
        + "message » (100 au plus). `code` seul : vérifier un extrait (celui de la conversation, "
        + "sans dossier) ; `code` + `path` : une version modifiée d'un fichier, avant de l'écrire. "
        + "60 s au plus (le premier appel charge le compilateur).",
      params: {
        files: { type: "array", items: { type: "string" },
                 description: "Fichiers ou sous-dossiers à vérifier (« . » : tout le dossier)." },
        code: { type: "string", description: "Un texte TypeScript à vérifier : seul (sans dossier ouvert, un extrait de la conversation), ou à la place du fichier `path`." },
        path: { type: "string", description: "Le chemin de ce texte dans le projet (défaut : extrait.ts)." },
      },
    });
  }

  /** @override @param {any} a */
  summary(a) { return oneLine([...(a.files || []), a.path, a.code].filter(Boolean).join(" · ")); }

  /** @override @param {any} args @param {import("../contract.js").Ctx} ctx */
  async run(args, ctx) {
    const wanted = pathsOf(args).map((p) => (p === "." ? "" : clean(p)));
    const override = typeof args.code === "string" && args.code.trim()
      ? { path: clean(args.path) || "extrait.ts", code: args.code } : null;
    if (!wanted.length && !override) return failure("`files` ou `code` attendu.");
    let input;
    try { input = await project(ctx, wanted, override); } catch (e) { return failure(e.message + "."); }
    if (!input.roots.length) return failure("aucun fichier TypeScript à vérifier.");
    const r = await exec({ driver, input, runtime: "typescript", timeout: TIMEOUT });
    if (!r.ok) return failure(r.error);
    const { diagnostics, total, checked, notes, version } = r.value;
    const head = `Vérifié : ${checked} fichier${checked > 1 ? "s" : ""} (TypeScript ${version}`
      + (input.tsconfig ? ", tsconfig.json du dossier" : ", réglages par défaut") + ").";
    const errors = diagnostics.filter((d) => d.category === "error").length;
    const lines = diagnostics.map((d) => `${d.file ? `${d.file}(${d.line},${d.col}): ` : ""}${d.category} TS${d.code}: ${d.message}`);
    const out = [head, ...notes,
      total ? `${total} diagnostic${total > 1 ? "s" : ""}, dont ${errors} erreur${errors > 1 ? "s" : ""}`
        + (total > diagnostics.length ? ` (${diagnostics.length} premiers)` : "") + " :" : "Aucune erreur de type.",
      ...lines];
    return clip(out.join("\n"), MAX_OUT);
  }
}
