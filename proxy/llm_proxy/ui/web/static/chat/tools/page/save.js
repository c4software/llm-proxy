// @ts-check
/*
  `save_file` : le modèle propose un fichier (CSV, script, Markdown…), en
  puce sous la réponse, à télécharger. Le fichier est gardé dans le
  message (`files`). saveFile sert aussi aux autres outils qui produisent
  un fichier texte (l'agenda, les bacs de code).
*/
import { TOOL, FAMILY, PageTool, defineFamily, failure } from "../contract.js";

const MAX_FILE = 2e6;          // caractères d'un fichier produit

const MIME = { csv: "text/csv", json: "application/json", md: "text/markdown", html: "text/html",
  svg: "image/svg+xml", xml: "application/xml", js: "text/javascript", py: "text/x-python",
  txt: "text/plain", yaml: "text/yaml", yml: "text/yaml", toml: "text/plain", sh: "text/x-shellscript",
  ics: "text/calendar" };
/** @param {string} name */
const ext = (name) => (String(name).split(".").pop() || "").toLowerCase();

/** Un fichier texte sous la réponse (`live.files`) → le texte du résultat.
 * @param {any} live @param {{name?: string, content?: string, mime?: string}} args */
export function saveFile(live, args) {
  const name = String(args.name || "").split(/[\\/]/).pop().trim();
  if (!name) return failure("`name` est requis.");
  const content = String(args.content ?? "");
  if (content.length > MAX_FILE) return failure(`fichier trop long (${content.length} caractères, ${MAX_FILE} au plus).`);
  const type = args.mime || MIME[ext(name)] || "text/plain";
  (live.files || (live.files = [])).push({ name, type, content, size: new Blob([content]).size });
  return `Fichier « ${name} » proposé au téléchargement sous la réponse.`;
}

class SaveFile extends PageTool {
  constructor() {
    super({ name: TOOL.SAVE_FILE });
  }

  /** @override @param {any} args @param {import("../contract.js").Ctx} ctx */
  run(args, ctx) { return saveFile(ctx.live, args); }

  /** @override @param {any} a */
  summary(a) { return a.name || ""; }
}

export const FAMILIES = [defineFamily({ key: FAMILY.SAVE, tools: [new SaveFile()] })];
