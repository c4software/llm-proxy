// @ts-check
/*
  Le RENDU d'un artifact (tools/page/artifacts.js), pour le panneau
  latéral (ArtifactPanel.vue) : son document, et son nom de fichier.

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
import { vendorUrl } from "./tools/sandbox/vendor.js";

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
    const t = /** @type {any} */ (e.target);
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
      // Du code colorié (highlight.js) : son texte, pas un span par jeton.
      const own = el.classList.contains("hljs") ? el.textContent.trim() : [...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent.trim()).join(" ").trim();
      if (own) line += " « " + own.slice(0, 80) + (own.length > 80 ? "…" : "") + " »";
      if ("value" in el && el.value !== undefined && el.value !== "" && tag !== "button") line += " [valeur : " + String(el.value).slice(0, 60) + "]";
      if (el.type && tag === "input") line += " [" + el.type + (el.checked ? ", coché" : "") + "]";
      if (el.disabled) line += " [désactivé]";
      if (tag === "img") line += " [" + (el.alt || el.src.slice(0, 60)) + (el.naturalWidth ? "" : ", non chargée") + "]";
      if (tag === "canvas" || tag === "svg") line += " [" + Math.round(el.getBoundingClientRect().width) + "×" + Math.round(el.getBoundingClientRect().height) + "]";
      if (hidden) line += " (masqué)";
      out.push(line);
      chars += line.length;
      if (hidden || tag === "svg" || el.classList.contains("hljs")) return;
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
        const w = /** @type {any} */ (window);
        if (!w.htmlToImage) (0, eval)(d.lib);
        const bg = getComputedStyle(document.body).backgroundColor;
        const canvas = await w.htmlToImage.toCanvas(document.documentElement, {
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

// Le CODE, colorié par la page avec highlight.js (vendor/highlight.js,
// `hl` : { hljs, css }, chargé par loadHighlighter) : le cadre reçoit du
// HTML déjà colorié et son thème, sans script ni requête. Le `language`
// de show_artifact s'il est dans le paquet courant (alias compris : py,
// js, sh, c#, c++…), sinon détecté. Sans `hl` (pas encore chargé, absent
// du serveur), le code brut. La coloration se fait sur le fil de la
// page : au-delà de MAX_HIGHLIGHT caractères, le code brut ; la détection
// ne lit que le début (essayer chaque langage sur tout figerait la page).
export const MAX_HIGHLIGHT = 100000;
const DETECT_CHARS = 5000;
function codeBody(content, language, hl) {
  const asked = String(language || "").toLowerCase();
  const lang = !hl || content.length > MAX_HIGHLIGHT ? "" : hl.hljs.getLanguage(asked) ? asked
    : hl.hljs.highlightAuto(content.slice(0, DETECT_CHARS)).language;
  if (!lang) return `<pre><code>${esc(content)}</code></pre>`;
  const { value } = hl.hljs.highlight(content, { language: lang, ignoreIllegals: true });
  return `<style>${hl.css}</style><pre><code class="hljs">${value}</code></pre>`;
}

/** highlight.js et son thème (vendor/highlight.js), chargés une fois ;
 * un échec est oublié. @type {Promise<{ hljs: any, css: string }> | null} */
let highlighter = null;
export function loadHighlighter() {
  if (!highlighter) {
    highlighter = vendorUrl("highlight.js").then((u) => import(/* @vite-ignore */ u));
    highlighter.catch(() => { highlighter = null; });
  }
  return highlighter;
}

export function artifactDoc(version, { network = false, debug = false, hl = null } = {}) {
  const { type, content } = version;
  const META = meta(network ? OPEN_CSP : CSP) + (debug ? BRIDGE : "");
  if (type === "html") {
    // La politique TOUJOURS en premier, avant le moindre octet de
    // l'artifact : chercher son <head> ne suffit pas (`<header>`, un
    // commentaire, un script placé avant). Ce que l'artifact a ensuite
    // en tête est rangé par l'analyseur HTML dans le même <head> (son
    // doctype et son <head> sont ignorés, les attributs de son <html>
    // fusionnés) : rendu inchangé, toujours en mode standard.
    return `<!doctype html>${META}${content}`;
  }
  const body = type === "markdown" ? Markdown.render(content)
    : type === "svg" ? content
    : type === "mermaid" ? `<pre class="mermaid">${esc(content)}</pre>
<script src="https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js"></script>
<script>mermaid.initialize({ startOnLoad: true,
  theme: "default" });</script>`
    : codeBody(content, version.language, hl);
  return `<!doctype html><html><head>${META}${BASE_CSS}</head><body>${body}</body></html>`;
}

// ── un PROJET : plusieurs fichiers ({path, content}) ──────────────────

/** Le fichier HTML d'entrée d'un projet : `index.html` à la racine, sinon
 * le premier `.html` ; null sans HTML. @param {any} version */
export function entryOf(version) {
  const html = version.files.filter((f) => /\.html?$/i.test(f.path));
  return html.find((f) => f.path === "index.html") || html[0] || null;
}

// Le chemin d'un lien relatif d'un fichier du projet (« style.css »,
// « ./app.js », « ../css/a.css », « /static/a.css » depuis la racine) ;
// null s'il sort du projet ou désigne une URL.
function resolveIn(from, href) {
  if (/^([a-z][a-z0-9+.-]*:|\/\/)/i.test(href)) return null;
  const parts = href.startsWith("/") ? [] : from.split("/").slice(0, -1);
  for (const seg of href.split(/[?#]/)[0].split("/")) {
    if (seg === "..") { if (!parts.length) return null; parts.pop(); } else if (seg && seg !== ".") parts.push(seg);
  }
  return parts.join("/");
}

/** L'APERÇU d'un fichier HTML du projet : ses feuilles de style et scripts
 * qui sont des fichiers du projet, mis en ligne ; le reste tel quel (une
 * URL, un `import` de module, une image du projet ne sont pas résolus).
 * Un script garde ses attributs sauf `src` ; un script classique `defer`
 * ou `async` passe en fin de document (en ligne, il n'attendrait plus).
 * Même document qu'un artifact HTML (politique en premier, pont).
 * @param {any} version @param {string} path @param {object} [opts] */
export function projectDoc(version, path, opts = {}) {
  const files = new Map(version.files.map((f) => [f.path, f.content]));
  const fileAt = (href) => { const p = resolveIn(path, href); return p === null ? undefined : files.get(p); };
  const ATTR = /\s([^\s=>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
  const attrs = (tag) => [...tag.matchAll(ATTR)].map((m) => ({ name: m[1].toLowerCase(), raw: m[0], value: m[2] ?? m[3] ?? m[4] ?? "" }));
  // Ce qui fermerait la balise (`</style`, `</script`, casse gardée) ou
  // ouvrirait un commentaire dans un script, échappé.
  const inStyle = (css) => css.replace(/<\/(style)/gi, "<\\/$1");
  const inScript = (js) => js.replace(/<\/(script)/gi, "<\\/$1").replace(/<!--/g, "<\\!--");
  const deferred = [];
  let html = (files.get(path) || "")
    .replace(/<link\b[^>]*>/gi, (tag) => {
      const a = attrs(tag.slice(5));
      const css = a.some((x) => x.name === "rel" && /\bstylesheet\b/i.test(x.value))
        ? fileAt((a.find((x) => x.name === "href") || {}).value || "") : undefined;
      return css === undefined ? tag : `<style>${inStyle(css)}</style>`;
    })
    .replace(/<script\b([^>]*)>\s*<\/script>/gi, (tag, inner) => {
      const a = attrs(inner);
      const js = fileAt((a.find((x) => x.name === "src") || {}).value || "");
      if (js === undefined) return tag;
      const module = a.some((x) => x.name === "type" && /^module$/i.test(x.value));
      const later = !module && a.some((x) => x.name === "defer" || x.name === "async");
      const kept = a.filter((x) => x.name !== "src" && (!later || (x.name !== "defer" && x.name !== "async")))
        .map((x) => x.raw).join("");
      const inline = `<script${kept}>${inScript(js)}</script>`;
      if (!later) return inline;
      deferred.push(inline);
      return "";
    });
  if (deferred.length) html += deferred.join("");
  return artifactDoc({ type: "html", content: html }, opts);
}

/** L'ARBORESCENCE d'un projet, pour l'explorateur : une ligne par dossier
 * et par fichier ({key: chemin, name, depth, dir}).
 * @param {{path: string}[]} files */
export function fileTree(files) {
  const rows = new Map();
  for (const f of files) {
    const parts = f.path.split("/");
    parts.forEach((name, i) => {
      const key = parts.slice(0, i + 1).join("/");
      if (!rows.has(key)) rows.set(key, { key, name, depth: i, dir: i < parts.length - 1 });
    });
  }
  // Segment par segment (comparer les chemins entiers mettrait « src-old »
  // entre « src » et son contenu) ; dans un même dossier, les dossiers
  // d'abord.
  const segs = (r) => r.key.split("/").map((s, i, all) => [i < all.length - 1 || r.dir ? 0 : 1, s]);
  const cmp = (a, b) => {
    const x = segs(a), y = segs(b);
    for (let i = 0; i < Math.min(x.length, y.length); i++) {
      if (x[i][0] !== y[i][0]) return x[i][0] - y[i][0];
      if (x[i][1] !== y[i][1]) return x[i][1] < y[i][1] ? -1 : 1;
    }
    return x.length - y.length;
  };
  return [...rows.values()].sort(cmp);
}

/** La SOURCE d'un fichier, colorée selon son extension (codeBody).
 * @param {{path: string, content: string}} file @param {any} hl */
export function fileDoc(file, hl) {
  const ext = (/\.([A-Za-z0-9]+)$/.exec(file.path) || [])[1] || "";
  return artifactDoc({ type: "code", content: file.content, language: ext }, { hl });
}

// Une archive ZIP sans compression (« stored »), noms en UTF-8 : de quoi
// télécharger un projet d'un coup.
const CRC = Array.from({ length: 256 }, (_, n) => {
  let c = n;
  for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  return c >>> 0;
});
const crc32 = (bytes) => { let c = 0xffffffff; for (const b of bytes) c = CRC[(c ^ b) & 0xff] ^ (c >>> 8); return (c ^ 0xffffffff) >>> 0; };
/** @param {{path: string, content: string}[]} files @param {number} at @returns {Uint8Array} */
export function zip(files, at = Date.now()) {
  const d = new Date(at);
  const time = (d.getHours() << 11) | (d.getMinutes() << 5) | (d.getSeconds() >> 1);
  const date = ((d.getFullYear() - 1980) << 9) | ((d.getMonth() + 1) << 5) | d.getDate();
  const enc = new TextEncoder(), local = [], central = [];
  let offset = 0;
  /** @param {number} size @returns {[Uint8Array, DataView]} */
  const header = (size) => { const b = new Uint8Array(size); return [b, new DataView(b.buffer)]; };
  for (const f of files) {
    const name = enc.encode(f.path), data = enc.encode(f.content), crc = crc32(data);
    const [lh, l] = header(30);
    l.setUint32(0, 0x04034b50, true); l.setUint16(4, 20, true); l.setUint16(6, 0x0800, true);
    l.setUint16(10, time, true); l.setUint16(12, date, true); l.setUint32(14, crc, true);
    l.setUint32(18, data.length, true); l.setUint32(22, data.length, true); l.setUint16(26, name.length, true);
    const [ch, c] = header(46);
    c.setUint32(0, 0x02014b50, true); c.setUint16(4, 20, true); c.setUint16(6, 20, true); c.setUint16(8, 0x0800, true);
    c.setUint16(12, time, true); c.setUint16(14, date, true); c.setUint32(16, crc, true);
    c.setUint32(20, data.length, true); c.setUint32(24, data.length, true); c.setUint16(28, name.length, true);
    c.setUint32(42, offset, true);
    local.push(lh, name, data);
    central.push(ch, name);
    offset += 30 + name.length + data.length;
  }
  const size = central.reduce((n, b) => n + b.length, 0);
  const [eh, e] = header(22);
  e.setUint32(0, 0x06054b50, true); e.setUint16(8, files.length, true); e.setUint16(10, files.length, true);
  e.setUint32(12, size, true); e.setUint32(16, offset, true);
  const out = new Uint8Array(offset + size + 22);
  let at2 = 0;
  for (const b of [...local, ...central, eh]) { out.set(b, at2); at2 += b.length; }
  return out;
}

// Le fichier d'un artifact téléchargé ; un projet : son .zip.
export function artifactFile(art, version) {
  const base = art.id || "artifact";
  if (version.type === "project") return { name: `${base}.zip`, content: zip(version.files, version.at) };
  const extOf = { html: "html", markdown: "md", svg: "svg", mermaid: "mmd", code: "txt" };
  return { name: `${base}.${extOf[version.type] || "txt"}`, content: version.content };
}

// La CAPTURE rendue par le pont d'un artifact : elle vient du cadre, donc
// de code non fiable — l'artifact peut répondre à la place du pont. Elle
// ne part au modèle et à l'affichage que si c'est bien une image JPEG ou
// PNG en base64, de taille bornée ; sinon null.
export const MAX_SHOT_CHARS = 8 * 1024 * 1024;
const SHOT = /^data:image\/(?:jpeg|png);base64,[A-Za-z0-9+/]+={0,2}$/;
/** @param {any} shot @returns {{ image: string, size: [number, number] } | null} */
export function shotImage(shot) {
  if (!shot || typeof shot.image !== "string" || shot.image.length > MAX_SHOT_CHARS
      || !SHOT.test(shot.image)) return null;
  const [w, h] = Array.isArray(shot.size) ? shot.size.map(Number) : [];
  const dim = (n) => Number.isFinite(n) && n > 0 && n < 100000 ? Math.round(n) : 0;
  return { image: shot.image, size: [dim(w), dim(h)] };
}
