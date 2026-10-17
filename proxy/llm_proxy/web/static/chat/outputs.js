/*
  Ce que le modèle PRODUIT dans le chat, sans exécuteur de code et sans
  passer par le proxy :

    save_file      un fichier (CSV, script, Markdown…) en puce sous la
                   réponse : à télécharger
    show_artifact  une page, un document, un schéma ou un SVG, montré dans
                   le panneau latéral ; un même `id` en fait une nouvelle
                   version

  Les deux sont gardés avec la conversation (le message pour un fichier,
  conv.artifacts pour un artifact).

  ISOLATION D'UN ARTIFACT : il est rendu dans un cadre `sandbox` sans
  `allow-same-origin` — une origine à lui, sans accès à la page, à sa
  session ni à ses clés — sous une politique de contenu posée dans le
  document : scripts et styles en ligne, bibliothèques chargées depuis
  cdnjs, jsdelivr ou unpkg ; aucune autre requête réseau (ni fetch, ni
  image distante, ni formulaire).
*/
import { Markdown } from "../lib/markdown.js";

const MAX_FILE = 2e6;          // caractères d'un fichier produit
const MAX_ARTIFACT = 5e5;      // caractères d'un artifact
export const TYPES = ["html", "markdown", "svg", "mermaid", "code"];

export const TOOLS = [
  { type: "function", function: {
    name: "save_file",
    description: "Propose à l'utilisateur un fichier à télécharger (CSV, JSON, script, "
      + "Markdown, configuration…), affiché sous ta réponse. Le contenu est du texte.",
    parameters: { type: "object", required: ["name", "content"], properties: {
      name: { type: "string", description: "Nom du fichier, avec son extension." },
      content: { type: "string", description: "Le contenu complet du fichier." },
      mime: { type: "string", description: "Type MIME (facultatif, déduit de l'extension)." },
    } } } },
  { type: "function", function: {
    name: "show_artifact",
    description: "Affiche dans le panneau latéral du chat une page HTML (scripts permis, "
      + "bibliothèques depuis cdnjs, jsdelivr ou unpkg ; aucune autre requête réseau), un "
      + "document Markdown, un SVG, un schéma Mermaid ou du code. Réutiliser le même `id` "
      + "pour en publier une nouvelle version. Pour les visualisations, maquettes, petits "
      + "outils interactifs, documents longs — pas pour une réponse courte.",
    parameters: { type: "object", required: ["title", "type", "content"], properties: {
      id: { type: "string", description: "Identifiant stable (ex. « tableau-de-bord ») ; même id = nouvelle version." },
      title: { type: "string", description: "Titre court." },
      type: { type: "string", enum: TYPES },
      content: { type: "string", description: "Le contenu complet (une page HTML entière pour `html`)." },
      language: { type: "string", description: "Pour `code` : le langage." },
    } } } },
];
export const TOOL_NAMES = new Set(TOOLS.map((t) => t.function.name));

const MIME = { csv: "text/csv", json: "application/json", md: "text/markdown", html: "text/html",
  svg: "image/svg+xml", xml: "application/xml", js: "text/javascript", py: "text/x-python",
  txt: "text/plain", yaml: "text/yaml", yml: "text/yaml", toml: "text/plain", sh: "text/x-shellscript" };
const ext = (name) => (String(name).split(".").pop() || "").toLowerCase();

// save_file : la puce rejoint le message (`files`).
export function saveFile(live, args) {
  const name = String(args.name || "").split(/[\\/]/).pop().trim();
  if (!name) return "Error: `name` est requis.";
  const content = String(args.content ?? "");
  if (content.length > MAX_FILE) return `Error: fichier trop long (${content.length} caractères, ${MAX_FILE} au plus).`;
  const type = args.mime || MIME[ext(name)] || "text/plain";
  (live.files || (live.files = [])).push({ name, type, content, size: new Blob([content]).size });
  return `Fichier « ${name} » proposé au téléchargement sous la réponse.`;
}

// show_artifact : la version rejoint conv.artifacts ; rend l'id montré.
export function showArtifact(conv, args) {
  const type = TYPES.includes(args.type) ? args.type : "";
  if (!type) return { result: `Error: \`type\` doit être l'un de ${TYPES.join(", ")}.` };
  const content = String(args.content ?? "");
  if (!content.trim()) return { result: "Error: `content` est vide." };
  if (content.length > MAX_ARTIFACT) return { result: `Error: artifact trop long (${MAX_ARTIFACT} caractères au plus).` };
  const title = String(args.title || "Sans titre").trim().slice(0, 120);
  const id = String(args.id || title).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "artifact";
  const list = conv.artifacts || (conv.artifacts = []);
  let art = list.find((a) => a.id === id);
  if (!art) { list.push({ id, title, versions: [] }); art = list[list.length - 1]; }
  art.title = title;
  art.versions.push({ type, content, language: args.language || "", at: Date.now() });
  const n = art.versions.length;
  return { id, result: `Affiché dans le panneau : « ${title} »` + (n > 1 ? ` (version ${n}).` : ".") };
}

// ── le document d'un artifact, pour le cadre isolé ────────────────────
const CDN = "https://cdnjs.cloudflare.com https://cdn.jsdelivr.net https://unpkg.com";
const CSP = `default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval' ${CDN}; `
  + `style-src 'unsafe-inline' ${CDN} https://fonts.googleapis.com; `
  + `font-src data: ${CDN} https://fonts.gstatic.com; img-src data: blob:; media-src data: blob:`;
const META = `<meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${CSP}">`;
const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
// Les documents générés par la page (Markdown, SVG, code) : thème clair,
// fixé — le cadre est blanc quel que soit celui de la page.
const BASE_CSS = `<style>:root{color-scheme:light}body{margin:0;padding:20px;background:#fff;
  color:#1d1c1a;font:15px/1.55 ui-sans-serif,system-ui,sans-serif}pre,code{font:13px ui-monospace,monospace}
  pre{white-space:pre-wrap}table{border-collapse:collapse}td,th{border:1px solid #8884;padding:4px 8px}
  img,svg{max-width:100%}</style>`;

export function artifactDoc(version) {
  const { type, content } = version;
  if (type === "html") {
    // La politique en tête de SA page : avant tout ce qu'elle charge.
    if (/<head[^>]*>/i.test(content)) return content.replace(/<head[^>]*>/i, (h) => h + META);
    if (/<html[^>]*>/i.test(content)) return content.replace(/<html[^>]*>/i, (h) => h + "<head>" + META + "</head>");
    return `<!doctype html><html><head>${META}</head><body>${content}</body></html>`;
  }
  const body = type === "markdown" ? Markdown.render(content)
    : type === "svg" ? content
    : type === "mermaid" ? `<pre class="mermaid">${esc(content)}</pre>
<script src="https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js"></script>
<script>mermaid.initialize({ startOnLoad: true,
  theme: "default" });</script>`
    : `<pre><code>${esc(content)}</code></pre>`;
  return `<!doctype html><html><head>${META}${BASE_CSS}</head><body>${body}</body></html>`;
}

// Le nom d'un artifact téléchargé.
export function artifactFile(art, version) {
  const extOf = { html: "html", markdown: "md", svg: "svg", mermaid: "mmd", code: "txt" };
  const base = art.id || "artifact";
  return { name: `${base}.${extOf[version.type] || "txt"}`, content: version.content };
}
