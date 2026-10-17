/*
  Le DOSSIER ouvert dans une conversation du chat (Joindre → Dossier…) :
  il reste dans le navigateur — rien n'est envoyé d'avance. Le modèle
  reçoit trois outils, que la PAGE exécute elle-même quand il les appelle
  (le proxy lui rend ces appels en `tool_calls`, comme à tout client) :

    folder_list    le contenu d'un sous-dossier (ou tout, récursif)
    folder_read    un fichier texte, par tranches de lignes numérotées
    folder_search  une chaîne ou une expression dans les fichiers texte

  Le modèle ne lit donc que ce dont il a besoin : un dépôt de mille
  fichiers ne coûte que ce qu'il en ouvre.

  ÉCRIRE : folder_write (créer ou remplacer un fichier) et folder_edit
  (remplacer un passage exact) ne s'appliquent qu'après votre accord — la
  page montre le diff, « Appliquer » ou « Refuser », et la réponse attend.
  Appliqué : écrit dans le dossier quand le navigateur le permet (Chrome,
  Edge : dossier ouvert par showDirectoryPicker, accès en écriture demandé
  à la première écriture), sinon le fichier modifié est téléchargé. Dans
  les deux cas, le modèle relit ensuite la nouvelle version. Ignorés à l'ouverture : .git,
  node_modules, environnements Python et caches. Un dossier n'est gardé
  qu'en mémoire : après un rechargement, la conversation dit son nom et
  demande de le rouvrir (le navigateur ne rend pas l'accès tout seul).

  Chaque résultat est un TEXTE pour le modèle ; une erreur commence par
  « Error: », comme ceux des outils du proxy.
*/

const IGNORED = new Set([".git", "node_modules", "__pycache__", ".venv", "venv", ".tox",
                         ".mypy_cache", ".pytest_cache", ".ruff_cache", ".DS_Store"]);
const MAX_LIST = 500;          // lignes d'une liste
const MAX_READ_LINES = 1500;   // lignes d'une lecture
const MAX_READ_CHARS = 60000;  // caractères d'une lecture
const MAX_MATCHES = 200;       // résultats d'une recherche
const MAX_SEARCH_FILE = 2e6;   // un fichier plus gros n'est pas fouillé

// Les déclarations, à la forme chat/completions.
export const TOOLS = [
  { type: "function", function: {
    name: "folder_list",
    description: "Liste le contenu du dossier ouvert par l'utilisateur dans le chat. "
      + "Sans `path` : sa racine. Les sous-dossiers finissent par « / », avec leur nombre "
      + "de fichiers ; les fichiers ont leur taille. `recursive` : tous les fichiers sous `path`.",
    parameters: { type: "object", properties: {
      path: { type: "string", description: "Sous-dossier relatif à la racine ; vide = la racine." },
      recursive: { type: "boolean", description: "Lister tous les fichiers sous `path`." },
    } } } },
  { type: "function", function: {
    name: "folder_read",
    description: "Lit un fichier texte du dossier ouvert, lignes numérotées. Un long fichier "
      + "se lit par tranches : `start_line` et `end_line` (incluses, à partir de 1).",
    parameters: { type: "object", required: ["path"], properties: {
      path: { type: "string", description: "Chemin du fichier, relatif à la racine du dossier." },
      start_line: { type: "integer", description: "Première ligne (défaut : 1)." },
      end_line: { type: "integer", description: "Dernière ligne (défaut : jusqu'à la limite d'une lecture)." },
    } } } },
  { type: "function", function: {
    name: "folder_search",
    description: "Cherche un texte dans les fichiers texte du dossier ouvert ; rend "
      + "« chemin:ligne: texte » pour chaque ligne trouvée.",
    parameters: { type: "object", required: ["pattern"], properties: {
      pattern: { type: "string", description: "Le texte cherché (ou une expression régulière si `regex`)." },
      path: { type: "string", description: "Ne chercher que sous ce sous-dossier." },
      regex: { type: "boolean", description: "`pattern` est une expression régulière (JavaScript)." },
      ignore_case: { type: "boolean", description: "Sans tenir compte de la casse (défaut : oui)." },
    } } } },
];
// Écrire : soumis à l'accord de l'utilisateur (prepareWrite, apply).
export const WRITE_TOOLS = [
  { type: "function", function: {
    name: "folder_write",
    description: "Crée ou remplace entièrement un fichier texte du dossier ouvert. "
      + "L'utilisateur voit le diff et accepte ou refuse ; le résultat le dit. Pour une "
      + "modification localisée, préférer folder_edit.",
    parameters: { type: "object", required: ["path", "content"], properties: {
      path: { type: "string", description: "Chemin relatif à la racine du dossier." },
      content: { type: "string", description: "Le contenu complet du fichier." },
    } } } },
  { type: "function", function: {
    name: "folder_edit",
    description: "Remplace un passage EXACT d'un fichier du dossier ouvert (lire le fichier "
      + "d'abord). `old_text` doit y être une seule fois, sauf `replace_all`. L'utilisateur "
      + "voit le diff et accepte ou refuse.",
    parameters: { type: "object", required: ["path", "old_text", "new_text"], properties: {
      path: { type: "string" },
      old_text: { type: "string", description: "Le passage actuel, à l'identique (espaces compris)." },
      new_text: { type: "string", description: "Ce qui le remplace." },
      replace_all: { type: "boolean", description: "Remplacer toutes les occurrences." },
    } } } },
];
export const TOOL_NAMES = new Set(TOOLS.map((t) => t.function.name));
export const WRITE_NAMES = new Set(WRITE_TOOLS.map((t) => t.function.name));

// Les arguments d'une écriture → ce que la carte montre (avant, après),
// ou {error} : rien à demander à l'utilisateur.
export async function prepareWrite(ws, name, args) {
  const path = clean(args.path);
  if (!path || path.split("/").some((p) => p === ".." || p === "")) {
    return { error: "Error: `path` invalide (relatif à la racine, sans « .. »)." };
  }
  if ([...ws.files.keys()].some((p) => p.startsWith(path + "/"))) {
    return { error: `Error: « ${path} » est un dossier.` };
  }
  const before = ws.files.has(path) ? await text(ws, path) : null;
  if (ws.files.has(path) && before === null) return { error: `Error: « ${path} » est un fichier binaire.` };
  let after;
  if (name === "folder_write") {
    after = String(args.content ?? "");
  } else {
    if (before === null) return { error: `Error: aucun fichier « ${path} » à modifier (folder_write pour le créer).` };
    const old = String(args.old_text ?? "");
    if (!old) return { error: "Error: `old_text` est vide." };
    const n = before.split(old).length - 1;
    if (!n) return { error: `Error: \`old_text\` introuvable dans « ${path} » (le relire, à l'identique).` };
    if (n > 1 && !args.replace_all) {
      return { error: `Error: \`old_text\` figure ${n} fois dans « ${path} » : préciser le passage, ou replace_all.` };
    }
    after = args.replace_all ? before.split(old).join(String(args.new_text ?? ""))
      : before.replace(old, () => String(args.new_text ?? ""));
  }
  if (before === after) return { error: `Error: rien ne change dans « ${path} ».` };
  return { kind: "write", path, before, after, created: before === null };
}

// L'écriture acceptée : sur le disque si le dossier le permet, sinon
// téléchargée. Le dossier en mémoire prend la nouvelle version.
export async function apply(ws, path, after, download) {
  const name = path.split("/").pop();
  let where = "téléchargé (ce navigateur ne permet pas d'écrire dans le dossier)";
  if (ws.root) {
    if ((await ws.root.queryPermission({ mode: "readwrite" })) !== "granted"
        && (await ws.root.requestPermission({ mode: "readwrite" })) !== "granted") {
      throw new Error("écriture refusée par le navigateur");
    }
    let dir = ws.root;
    for (const part of path.split("/").slice(0, -1)) dir = await dir.getDirectoryHandle(part, { create: true });
    const handle = await dir.getFileHandle(name, { create: true });
    const out = await handle.createWritable();
    await out.write(after);
    await out.close();
    where = "écrit dans le dossier";
  } else {
    download({ name, content: after });
  }
  ws.files.set(path, new File([after], name, { type: "text/plain" }));
  ws.cache.set(path, after);
  ws.count = ws.files.size;
  return where;
}

// Ouvrir un dossier AVEC l'accès en écriture possible (Chrome, Edge) :
// le sélecteur du système, puis le parcours de ses fichiers.
export const canPick = () => typeof window.showDirectoryPicker === "function";
export async function pickFolder() {
  const root = await window.showDirectoryPicker({ mode: "read" });
  const files = new Map();
  let ignored = 0;
  async function walk(dir, prefix) {
    for await (const [entry, handle] of dir.entries()) {
      if (IGNORED.has(entry)) { ignored++; continue; }
      const path = prefix ? prefix + "/" + entry : entry;
      if (handle.kind === "directory") await walk(handle, path);
      else files.set(path, await handle.getFile());
    }
  }
  await walk(root, "");
  return { name: root.name, files, count: files.size, ignored, cache: new Map(), root };
}

// Un dossier choisi (<input webkitdirectory>) → {name, files, count, ignored}.
// `files` : chemin relatif à la racine → File.
export function openFolder(fileList) {
  const files = new Map();
  let name = "", ignored = 0;
  for (const file of fileList) {
    const parts = (file.webkitRelativePath || file.name).split("/");
    name = name || (parts.length > 1 ? parts[0] : "dossier");
    const rel = parts.length > 1 ? parts.slice(1) : parts;
    if (rel.some((p) => IGNORED.has(p))) { ignored++; continue; }
    files.set(rel.join("/"), file);
  }
  return { name, files, count: files.size, ignored, cache: new Map() };
}

const clean = (p) => String(p || "").replace(/^\.?\/+/, "").replace(/\/+$/, "");
const under = (path, dir) => !dir || path === dir || path.startsWith(dir + "/");
const kb = (n) => n >= 1e6 ? (n / 1e6).toFixed(1) + " Mo" : Math.max(1, Math.round(n / 1e3)) + " Ko";

async function text(ws, path) {
  if (ws.cache.has(path)) return ws.cache.get(path);
  const t = await ws.files.get(path).text();
  // Un octet nul dans le début : binaire, pas lisible comme du texte.
  const value = t.slice(0, 8000).includes("\u0000") ? null : t;
  ws.cache.set(path, value);
  return value;
}

function list(ws, args) {
  const dir = clean(args.path);
  const paths = [...ws.files.keys()].filter((p) => under(p, dir) && p !== dir).sort();
  if (!paths.length) {
    return ws.files.has(dir) ? `Error: « ${dir} » est un fichier, pas un dossier (folder_read le lit).`
      : `Error: aucun dossier « ${dir} » dans « ${ws.name} ».`;
  }
  const rel = (p) => (dir ? p.slice(dir.length + 1) : p);
  let lines;
  if (args.recursive) {
    lines = paths.map((p) => `${rel(p)}  (${kb(ws.files.get(p).size)})`);
  } else {
    const dirs = new Map(), files = [];
    for (const p of paths) {
      const r = rel(p), cut = r.indexOf("/");
      if (cut < 0) files.push(`${r}  (${kb(ws.files.get(p).size)})`);
      else dirs.set(r.slice(0, cut), (dirs.get(r.slice(0, cut)) || 0) + 1);
    }
    lines = [...[...dirs].map(([d, n]) => `${d}/  (${n} fichier${n > 1 ? "s" : ""})`), ...files];
  }
  const head = `${ws.name}/${dir ? dir + "/" : ""} — ${lines.length} entrée${lines.length > 1 ? "s" : ""}`;
  const shown = lines.slice(0, MAX_LIST);
  return [head, ...shown, ...(lines.length > MAX_LIST
    ? [`… ${lines.length - MAX_LIST} de plus (préciser \`path\`)`] : [])].join("\n");
}

async function read(ws, args) {
  const path = clean(args.path);
  if (!ws.files.has(path)) {
    return `Error: aucun fichier « ${path} » dans « ${ws.name} » (folder_list pour voir ce qui existe).`;
  }
  const t = await text(ws, path);
  if (t === null) return `Error: « ${path} » est un fichier binaire, illisible comme du texte.`;
  const all = t.split(/\r?\n/);
  const start = Math.max(1, Math.floor(Number(args.start_line) || 1));
  if (start > all.length) return `Error: « ${path} » n'a que ${all.length} ligne(s).`;
  let end = Math.min(all.length, Math.floor(Number(args.end_line) || start + MAX_READ_LINES - 1),
                     start + MAX_READ_LINES - 1);
  const out = [];
  let chars = 0;
  for (let n = start; n <= end; n++) {
    const line = `${String(n).padStart(5)}  ${all[n - 1]}`;
    if (chars + line.length > MAX_READ_CHARS && out.length) { end = n - 1; break; }
    out.push(line);
    chars += line.length + 1;
  }
  const more = end < all.length ? ` — la suite : start_line ${end + 1}` : "";
  return `${path} — lignes ${start} à ${end} sur ${all.length}${more}\n${out.join("\n")}`;
}

async function search(ws, args) {
  if (!args.pattern) return "Error: `pattern` est requis.";
  const dir = clean(args.path);
  const flags = args.ignore_case === false ? "" : "i";
  let re;
  try {
    re = args.regex ? new RegExp(args.pattern, flags)
      : new RegExp(String(args.pattern || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), flags);
  } catch (e) { return `Error: expression invalide — ${e.message}`; }
  const found = [];
  let scanned = 0;
  for (const [path, file] of [...ws.files].sort(([a], [b]) => a.localeCompare(b))) {
    if (!under(path, dir) || file.size > MAX_SEARCH_FILE) continue;
    const t = await text(ws, path);
    if (t === null) continue;
    scanned++;
    const lines = t.split(/\r?\n/);
    for (let n = 0; n < lines.length && found.length <= MAX_MATCHES; n++) {
      if (re.test(lines[n])) found.push(`${path}:${n + 1}: ${lines[n].trim().slice(0, 200)}`);
    }
    if (found.length > MAX_MATCHES) break;
  }
  if (!found.length) return `Aucun résultat pour « ${args.pattern} » (${scanned} fichier(s) texte parcouru(s)).`;
  const shown = found.slice(0, MAX_MATCHES);
  return shown.join("\n") + (found.length > MAX_MATCHES ? "\n… d'autres résultats : préciser la recherche." : "");
}

// Un appel du modèle → son résultat, toujours un texte.
export async function run(ws, name, argsJson) {
  let args = {};
  try { args = argsJson ? JSON.parse(argsJson) : {}; } catch {
    return "Error: arguments illisibles (JSON attendu).";
  }
  try {
    if (name === "folder_list") return list(ws, args);
    if (name === "folder_read") return await read(ws, args);
    if (name === "folder_search") return await search(ws, args);
  } catch (e) { return `Error: ${e.message}`; }
  return `Error: outil inconnu « ${name} ».`;
}

// Ce qu'une réponse a fait avec le dossier, pour l'historique renvoyé au
// modèle : chaque étape, un message assistant à `tool_calls` puis un
// message `tool` par appel.
export function stepMessages(steps) {
  const out = [];
  for (const step of steps || []) {
    out.push({ role: "assistant", content: step.content || null,
      tool_calls: step.calls.map((c) => ({ id: c.id, type: "function",
        function: { name: c.name, arguments: c.arguments || "{}" } })) });
    for (const c of step.calls) out.push({ role: "tool", tool_call_id: c.id, content: c.result });
  }
  return out;
}
