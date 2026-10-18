/*
  Le RENDU d'un artifact (tools/page/artifacts.js), pour le panneau
  latéral (ArtifactPanel.js) : son document, et son nom de fichier.

  ISOLATION D'UN ARTIFACT : il est rendu dans un cadre `sandbox` sans
  `allow-same-origin` — une origine à lui, sans accès à la page, à sa
  session ni à ses clés — sous une politique de contenu posée dans le
  document : scripts et styles en ligne, bibliothèques chargées depuis
  cdnjs, jsdelivr ou unpkg ; aucune autre requête réseau (ni fetch, ni
  image distante, ni formulaire). L'utilisateur peut OUVRIR le réseau à
  un artifact (bouton « Réseau » du panneau, `art.network`) : il joint
  alors n'importe quel site — une carte, une API publique — mais reste
  isolé de la page.
*/
import { Markdown } from "../lib/markdown.js";

// ── le document d'un artifact, pour le cadre isolé ────────────────────
const CDN = "https://cdnjs.cloudflare.com https://cdn.jsdelivr.net https://unpkg.com";
const CSP = `default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval' ${CDN}; `
  + `style-src 'unsafe-inline' ${CDN} https://fonts.googleapis.com; `
  + `font-src data: ${CDN} https://fonts.gstatic.com; img-src data: blob:; media-src data: blob:`;
// Réseau ouvert par l'utilisateur : tout site, mais toujours pas de
// formulaire (le cadre reste isolé de la page).
const OPEN_CSP = "default-src * data: blob: 'unsafe-inline' 'unsafe-eval'; form-action 'none'";
const meta = (csp) => `<meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${csp}">`;
const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
// Les documents générés par la page (Markdown, SVG, code) : thème clair,
// fixé — le cadre est blanc quel que soit celui de la page.
const BASE_CSS = `<style>:root{color-scheme:light}body{margin:0;padding:20px;background:#fff;
  color:#1d1c1a;font:15px/1.55 ui-sans-serif,system-ui,sans-serif}pre,code{font:13px ui-monospace,monospace}
  pre{white-space:pre-wrap}table{border-collapse:collapse}td,th{border:1px solid #8884;padding:4px 8px}
  img,svg{max-width:100%}</style>`;

export function artifactDoc(version, { network = false } = {}) {
  const { type, content } = version;
  const META = meta(network ? OPEN_CSP : CSP);
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
