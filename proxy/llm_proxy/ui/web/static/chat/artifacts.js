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

/** Le DÉBOGAGE d'un artifact : actif, sauf si l'utilisateur l'a coupé
 * (`art.debug === false`, bouton Débogage du panneau). @param {any} art */
export const debugOn = (art) => !!art && art.debug !== false;

// Le PONT de l'artifact (artifact-bridge.js), en tête de son document —
// seulement si son DÉBOGAGE est actif (debugOn, par défaut) : dès le
// chargement, il garde la console et les erreurs ; il répond aux demandes
// de la page qui l'a rendu (et d'elle seule) — inspecter la page, y
// exécuter un script avec ses aides (click, type, wait, text), la
// capturer (html-to-image, que la page lui passe : rien n'est chargé du
// réseau). Écrit comme une fonction : elle est copiée telle quelle.
function bridge() {
  const MAX_LOGS = 300, MAX_LINES = 400, MAX_CHARS = 14000;
  const logs = [];
  let seq = 0;
  const fmt = (v) => {
    if (v instanceof Error) return String(v.stack || v);
    if (typeof v === "string") return v;
    if (v instanceof Element) return "<" + v.tagName.toLowerCase() + (v.id ? "#" + v.id : "") + ">";
    try { const j = JSON.stringify(v); return j === undefined ? String(v) : j; } catch { return String(v); }
  };
  const note = (level, text) => {
    logs.push({ n: ++seq, level, text: String(text).slice(0, 2000) });
    if (logs.length > MAX_LOGS) logs.shift();
  };
  for (const level of ["log", "info", "warn", "error", "debug"]) {
    const orig = console[level].bind(console);
    console[level] = (...a) => { note(level, a.map(fmt).join(" ")); orig(...a); };
  }
  addEventListener("error", (e) => {
    const t = e.target;
    if (t && t !== window && (t.src || t.href)) note("error", "Ressource non chargée : " + (t.src || t.href));
    else note("error", (e.message || "Erreur") + (e.lineno ? " (ligne " + e.lineno + ", colonne " + e.colno + ")" : ""));
  }, true);
  addEventListener("unhandledrejection", (e) => note("error", "Promesse rejetée : " + fmt(e.reason)));
  addEventListener("securitypolicyviolation", (e) =>
    note("error", "Bloqué par la politique de contenu : " + (e.blockedURI || "?") + " (" + e.violatedDirective + ")"));

  // Les aides de run_in_artifact.
  const find = (sel) => {
    const el = typeof sel === "string" ? document.querySelector(sel) : sel;
    if (!el) throw new Error("aucun élément « " + sel + " »");
    return el;
  };
  const helpers = {
    click(sel) {
      const el = find(sel);
      for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup"]) {
        el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
      }
      el.click();
      return true;
    },
    type(sel, text) {
      const el = find(sel);
      el.focus();
      el.value = String(text);
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    },
    key(sel, key) {
      const el = find(sel);
      for (const type of ["keydown", "keyup"]) el.dispatchEvent(new KeyboardEvent(type, { key, bubbles: true }));
      return true;
    },
    wait: (ms) => new Promise((r) => setTimeout(r, Math.min(Number(ms) || 0, 10000))),
    text: (sel) => find(sel).innerText,
    all: (sel) => [...document.querySelectorAll(sel)],
  };

  // La page en texte : un élément par ligne, indenté, avec son id, ses
  // classes, son texte propre et ses valeurs.
  function outline() {
    const out = [];
    let chars = 0;
    function walk(el, depth) {
      if (out.length >= MAX_LINES || chars > MAX_CHARS) return;
      const tag = el.tagName.toLowerCase();
      if (["script", "style", "noscript", "template", "meta", "link"].includes(tag)) return;
      const css = getComputedStyle(el);
      const hidden = css.display === "none" || css.visibility === "hidden";
      let line = "  ".repeat(depth) + tag + (el.id ? "#" + el.id : "")
        + [...el.classList].slice(0, 3).map((c) => "." + c).join("");
      const own = [...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent.trim()).join(" ").trim();
      if (own) line += " « " + own.slice(0, 80) + (own.length > 80 ? "…" : "") + " »";
      if ("value" in el && el.value !== undefined && el.value !== "" && tag !== "button") line += " [valeur : " + String(el.value).slice(0, 60) + "]";
      if (el.type && tag === "input") line += " [" + el.type + (el.checked ? ", coché" : "") + "]";
      if (el.disabled) line += " [désactivé]";
      if (tag === "img") line += " [" + (el.alt || el.src.slice(0, 60)) + (el.naturalWidth ? "" : ", non chargée") + "]";
      if (tag === "canvas" || tag === "svg") line += " [" + Math.round(el.getBoundingClientRect().width) + "×" + Math.round(el.getBoundingClientRect().height) + "]";
      if (hidden) line += " (masqué)";
      out.push(line);
      chars += line.length;
      if (hidden || tag === "svg") return;
      for (const c of el.children) walk(c, depth + 1);
    }
    if (document.body) walk(document.body, 0);
    if (out.length >= MAX_LINES || chars > MAX_CHARS) out.push("… (page coupée)");
    return out.join("\n");
  }

  addEventListener("message", async (e) => {
    const d = e.data;
    if (e.source !== parent || !d || typeof d.llmBridge !== "string") return;
    const reply = (r) => parent.postMessage({ llmBridge: d.llmBridge, ...r }, "*");
    const since = seq;
    try {
      if (d.op === "inspect") {
        reply({ logs: logs.slice(), outline: outline(), title: document.title,
                size: [innerWidth, innerHeight], scroll: [scrollX, scrollY, document.documentElement.scrollHeight] });
      } else if (d.op === "run") {
        const run = new Function(...Object.keys(helpers), "return (async () => {\n" + d.code + "\n})()");
        const value = await run(...Object.values(helpers));
        await new Promise((r) => setTimeout(r, 60));           // les effets d'un clic
        reply({ value: value === undefined ? undefined : fmt(value), logs: logs.filter((l) => l.n > since) });
      } else if (d.op === "shot") {
        if (!window.htmlToImage) (0, eval)(d.lib);
        const bg = getComputedStyle(document.body).backgroundColor;
        const canvas = await window.htmlToImage.toCanvas(document.documentElement, {
          backgroundColor: bg === "rgba(0, 0, 0, 0)" ? "#ffffff" : bg, pixelRatio: 1,
          width: innerWidth, height: Math.min(document.documentElement.scrollHeight, 4000) });
        reply({ image: canvas.toDataURL("image/jpeg", 0.85), size: [canvas.width, canvas.height] });
      }
    } catch (err) {
      reply({ error: fmt(err), logs: logs.filter((l) => l.n > since) });
    }
  });
}
const BRIDGE = `<script>(${bridge})();</script>`;

export function artifactDoc(version, { network = false, debug = false } = {}) {
  const { type, content } = version;
  const META = meta(network ? OPEN_CSP : CSP) + (debug ? BRIDGE : "");
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
