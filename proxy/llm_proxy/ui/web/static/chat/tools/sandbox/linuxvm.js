// @ts-check
/*
  La VM LINUX de la page (docs/linux.md) : un PC x86 émulé en WASM (v86),
  avec une Alpine démarrée d'avance (vendor/linux/state.bin.zst, ≈ 25 Mo,
  téléchargé au premier usage). Rien ne passe par le proxy, qui ne fait
  que servir ces fichiers : le code, les fichiers et /work restent dans le
  navigateur.

  Une seule VM à la fois, VIVANTE tant que la page l'est : celle de la
  conversation qui s'en sert (`owner`). Elle tourne dans un cadre
  `sandbox` sans origine, sous une politique qui coupe tout réseau ; v86
  dans un Worker de ce cadre (la page reste fluide), le terminal xterm.js
  dans le cadre lui-même. Ce cadre est posé dans <body>, et placé par
  dessus le panneau Terminal quand il est ouvert (attach) : déplacé dans
  le DOM, un cadre se recharge — la VM serait perdue.

  Dès que le modèle se sert de la VM (run_shell, shell_read, shell_grep,
  shell_edit, shell_write), le panneau Terminal S'OUVRE s'il ne l'est pas
  (reveal) : l'utilisateur voit la machine, et les commandes du modèle
  s'y tapent.

  Deux accès à la même machine :
    le modèle      run_shell → exec() : l'agent de ttyS1, invisible ;
    l'utilisateur  le terminal du panneau : bash sur ttyS0.

  Le RÉSEAU est coupé, sauf si l'utilisateur l'ouvre pour la conversation
  (`conv.linuxNetwork`, bouton Réseau du panneau, après un avertissement) :
  la carte de la VM passe alors par le backend `fetch` de v86 — chaque
  requête HTTP de l'invité devient un fetch() du navigateur, sans relais,
  faite en https ; seuls les sites qui permettent CORS répondent (une
  origine `null`). Changer d'avis redémarre la VM, /work gardé.

  /WORK est gardé par conversation (IndexedDB, sqlbase.js sous
  « linux:<id> », en tar.gz) : après chaque appel du modèle, quand
  l'utilisateur a tapé (10 s après), au changement de conversation et à
  la fermeture du panneau. Le reste du système repart propre à chaque
  démarrage : c'est l'état du build, repris en ≈ 1,5 s.
*/
import * as sqlbase from "./sqlbase.js";
import { vendorFetch } from "./vendor.js";

const MAX_WORK = 100e6;              // octets de /work gardés (tar.gz)
const SAVE_AFTER = 10_000;           // ms sans frappe avant de garder /work
const key = (/** @type {string} */ id) => `linux:${id}`;

/** L'état montré par le panneau et l'outil.
 * status : off | loading (téléchargement) | starting | ready | failed */
export const vm = Vue.reactive({ status: "off", owner: "", error: "", busy: false, typing: false, saved: "", note: "",
                                  network: false });

const WORKER = `
let vm = null, ready = false, early = "";   // les frappes d'avant le démarrage
const blob = (text) => URL.createObjectURL(new Blob([text], { type: "text/javascript" }));
const resize = (cols, rows) => vm && vm.exec("stty -F /dev/ttyS0 rows " + (rows | 0) + " cols " + (cols | 0), { timeout: 10 }).catch(() => {});
onmessage = async ({ data: d }) => {
  if (d.op === "start") {
    try {
      const { V86 } = await import(blob(d.files.v86js));
      const { createVM } = await import(blob(d.files.vmjs));
      if (d.network) {
        // Les requêtes de l'invité (http://, port 80) partent en https : une
        // page https refuse le http, et la plupart des sites y redirigent.
        const f = self.fetch.bind(self);
        self.fetch = (u, o) => f(typeof u === "string" && u.startsWith("http://") ? "https://" + u.slice(7) : u, o);
      }
      vm = createVM(V86, d.files, { network: !!d.network });
      // Le terminal : les octets de ttyS0, regroupés par 16 ms.
      let pending = [], timer = 0;
      vm.onConsole((b) => {
        pending.push(b);
        if (!timer) timer = setTimeout(() => { timer = 0; postMessage({ op: "console", bytes: Uint8Array.from(pending) }); pending = []; }, 16);
      });
      await vm.ready();
      if (d.network && !(await vm.network())) postMessage({ op: "note", text: "réseau : la carte de la VM ne s'est pas configurée" });
      await resize(d.cols, d.rows);
      vm.redraw();                            // Ctrl-L : bash réaffiche son invite
      ready = true;
      if (early) { vm.type(new TextEncoder().encode(early)); early = ""; }
      postMessage({ op: "started" });
    } catch (e) {
      postMessage({ op: "fatal", error: String((e && e.message) || e) });
    }
  } else if (d.op === "type") {
    if (ready) vm.type(new TextEncoder().encode(d.data));
    else early += d.data;
  } else if (d.op === "resize") {
    resize(d.cols, d.rows);
  } else if (d.op === "redraw") {
    if (ready && vm.console.idle()) vm.redraw();
  } else if (d.op === "exec" || d.op === "typeExec" || d.op === "readFiles") {
    try {
      const r = d.op === "exec" ? await vm.exec(d.script, d.opts) : d.op === "typeExec" ? await vm.typeExec(d.script, d.opts)
        : { outputs: await vm.readFiles(d.script, d.opts) };
      postMessage({ op: "reply", id: d.id, ok: true, value: r },
                  [...(r.log ? [r.log.buffer] : []), ...(r.outputs || []).map((f) => f.bytes.buffer)]);
    } catch (e) {
      postMessage({ op: "reply", id: d.id, ok: false, error: String((e && e.message) || e) });
    }
  }
};`;

// Le cadre ; `connect-src` ouvert seulement avec le réseau (le Worker en hérite).
const frameDoc = (/** @type {boolean} */ network) => `<!doctype html><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval' 'wasm-unsafe-eval' blob:; worker-src blob:; style-src 'unsafe-inline'${network ? "; connect-src https: http:" : ""}">
<style>html, body { margin: 0; height: 100%; overflow: hidden; } #t { position: absolute; inset: 6px 2px 2px 8px; }</style>
<div id="t"></div>
<script>
const SOURCE = ${JSON.stringify(WORKER)};
let worker = null, term = null, fit = null;
const post = (m) => parent.postMessage(m, "*");
onmessage = async (e) => {
  if (e.source !== parent) return;
  const d = e.data || {};
  if (d.op === "start") {
    const style = document.createElement("style");
    style.textContent = d.xterm.css + "html, body { background: " + d.theme.background + "; }";
    document.head.append(style);
    const X = await import(URL.createObjectURL(new Blob([d.xterm.js], { type: "text/javascript" })));
    term = new X.Terminal({ fontFamily: d.font, fontSize: 13, theme: d.theme, cursorBlink: true, scrollback: 5000 });
    fit = new X.FitAddon();
    term.loadAddon(fit);
    term.open(document.getElementById("t"));
    fit.fit();
    new ResizeObserver(() => { try { fit.fit(); } catch { /* cadre caché */ } }).observe(document.body);
    // Un Worker classique : un Worker module est refusé sans origine.
    worker = new Worker(URL.createObjectURL(new Blob([SOURCE], { type: "text/javascript" })));
    worker.onmessage = (m) => { if (m.data.op === "console") term.write(m.data.bytes); else post(m.data); };
    worker.onerror = (err) => { err.preventDefault(); post({ op: "fatal", error: String(err.message || "erreur de la VM") }); };
    term.onData((s) => { worker.postMessage({ op: "type", data: s }); post({ op: "activity" }); });
    term.onResize(({ cols, rows }) => worker.postMessage({ op: "resize", cols, rows }));
    worker.postMessage({ op: "start", files: d.files, network: d.network, cols: term.cols, rows: term.rows }, Object.values(d.files).filter((v) => v instanceof ArrayBuffer));
  } else if (d.op === "theme") {
    if (term) { term.options.theme = d.theme; document.body.style.background = d.theme.background; }
  } else if (d.op === "focus") {
    if (term) term.focus();
  } else if (d.op === "shown") {
    // Visible à nouveau (le panneau s'ouvre) : réajusté, redessiné, et
    // l'invite réaffichée si le shell attend.
    if (term) { try { fit.fit(); } catch { /* caché */ } term.refresh(0, term.rows - 1); }
    if (worker) worker.postMessage({ op: "redraw" });
  } else if (worker) {
    worker.postMessage(d);
  }
};
post({ op: "loaded" });
<\/script>`;

// ── les fichiers (vendor/linux, vendor/xterm.*), téléchargés une fois ──
/** @type {Promise<any> | null} */
let fetching = null;
function vendorFiles() {
  if (!fetching) {
    // vendor.js : chacun à son empreinte — un ancien pilote ne reprend pas un état neuf.
    const get = vendorFetch;
    fetching = Promise.all([get("linux/v86.js", "text"), get("linux/vm.js", "text"), get("linux/v86.wasm", "arrayBuffer"),
      get("linux/seabios.bin", "arrayBuffer"), get("linux/vgabios.bin", "arrayBuffer"), get("linux/state.bin.zst", "arrayBuffer"),
      get("xterm.js", "text"), get("xterm.css", "text")])
      .then(([v86js, vmjs, wasm, bios, vgaBios, state, xjs, xcss]) => ({
        files: { v86js, vmjs, wasm, bios, vgaBios, state }, xterm: { js: xjs, css: xcss } }));
    fetching.catch(() => { fetching = null; });
  }
  return fetching;
}

// Les couleurs du terminal : celles de la page (dashboard.css).
function theme() {
  const css = getComputedStyle(document.documentElement);
  const v = (/** @type {string} */ n, /** @type {string} */ d) => css.getPropertyValue(n).trim() || d;
  return { background: v("--panel", "#fffefb"), foreground: v("--ink", "#191919"), cursor: v("--accent", "#d97757"),
           selectionBackground: v("--accent-soft", "#f4e2da") };
}
const FONT = 'ui-monospace, SFMono-Regular, "JetBrains Mono", Menlo, monospace';

// ── le cadre ────────────────────────────────────────────────────────────
/** @type {HTMLIFrameElement | null} */
let frame = null;
/** @type {Promise<void> | null} */
let starting = null;
/** @type {Map<string, {resolve: Function, reject: Function}>} */
const calls = new Map();
let incognito = false;
/** Un démarrage en cours est abandonné (teardown) : sa promesse échoue. @type {Function | null} */
let abandon = null;
let generation = 0;                  // chaque teardown en ouvre une nouvelle
/** @type {any} */
let saveTimer = 0;

function onMessage(/** @type {MessageEvent} */ e) {
  if (!frame || e.source !== frame.contentWindow) return;
  const d = e.data || {};
  if (d.op === "reply") {
    const c = calls.get(d.id);
    calls.delete(d.id);
    if (c) { if (d.ok) c.resolve(d.value); else c.reject(new Error(d.error)); }
  } else if (d.op === "activity") {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => { save().catch(() => {}); }, SAVE_AFTER);
  } else if (d.op === "fatal") {
    fail(d.error);
  } else if (d.op === "note") {
    vm.note = d.text;
  }
}

function fail(/** @type {string} */ error) {
  vm.status = "failed";
  vm.error = error;
  for (const c of calls.values()) c.reject(new Error(error));
  calls.clear();
}

/** `op` : exec (le canal du modèle), typeExec (tapé dans le terminal), ou
 * readFiles (`script` : le chemin ; rend les fichiers en `outputs`).
 * @param {string} script @param {object} opts @param {"exec" | "typeExec" | "readFiles"} [op]
 * @returns {Promise<{log: Uint8Array, exitCode: number, outputs: {name: string, bytes: Uint8Array}[],
 *   busy?: boolean, running?: boolean}>} */
function call(script, opts, op = "exec") {
  return new Promise((resolve, reject) => {
    if (!frame || vm.status !== "ready") { reject(new Error("la VM n'est pas démarrée")); return; }
    const id = crypto.randomUUID();
    calls.set(id, { resolve, reject });
    frame.contentWindow.postMessage({ op, id, script, opts }, "*");
  });
}

// La VM de `conv`, démarrée, son /work rechargé.
async function start(/** @type {any} */ conv) {
  vm.status = "loading";
  vm.error = "";
  vm.owner = conv.id;
  vm.saved = "";
  vm.note = "";
  vm.network = !!conv.linuxNetwork;
  incognito = !!conv.incognito;
  const gen = generation;
  const { files, xterm } = await vendorFiles().catch((e) => {
    throw new Error(`VM Linux absente de ce serveur (${e.message}) : elle est construite par `
      + "proxy/linux/build.sh, ce que fait l'image Docker");
  });
  if (gen !== generation) throw new Error("VM arrêtée pendant son démarrage");
  vm.status = "starting";
  frame = document.createElement("iframe");
  frame.className = "linux-frame";
  frame.setAttribute("sandbox", "allow-scripts");
  frame.setAttribute("title", "Terminal Linux");
  frame.tabIndex = -1;
  park(frame);
  const f = frame;
  await new Promise((resolve, reject) => {
    abandon = (/** @type {Error} */ err) => { window.removeEventListener("message", onReady); reject(err); };
    const onReady = (/** @type {MessageEvent} */ e) => {
      if (e.source !== f.contentWindow) return;
      const op = e.data && e.data.op;
      if (op === "loaded") {
        // Des copies des binaires : celles du cache restent à la page.
        const sent = { ...files, wasm: files.wasm.slice(0), bios: files.bios.slice(0), vgaBios: files.vgaBios.slice(0),
                       state: files.state.slice(0) };
        f.contentWindow.postMessage({ op: "start", files: sent, xterm, theme: theme(), font: FONT, network: vm.network }, "*",
                                    [sent.wasm, sent.bios, sent.vgaBios, sent.state]);
      } else if (op === "started") { window.removeEventListener("message", onReady); resolve(null); }
      else if (op === "fatal") { window.removeEventListener("message", onReady); reject(new Error(e.data.error)); }
    };
    window.addEventListener("message", onReady);
    f.srcdoc = frameDoc(vm.network);
    document.body.appendChild(f);
  });
  abandon = null;
  vm.status = "ready";
  const work = conv.id ? await sqlbase.load(key(conv.id)) : null;
  if (work && work.length) {
    const r = await call("tar -xzf .work.tgz; rc=$?; rm -f .work.tgz; exit $rc",
                         { timeout: 120, inputs: [{ name: ".work.tgz", bytes: work }] });
    vm.saved = r.exitCode ? "/work n'a pas pu être rechargé" : `/work rechargé (${size(work.length)})`;
  }
}

// Arrêter la VM (sans garder /work).
function teardown() {
  clearTimeout(saveTimer);
  generation++;
  if (abandon) abandon(new Error("VM arrêtée pendant son démarrage"));
  abandon = null;
  if (frame) frame.remove();
  frame = null;
  starting = null;
  for (const c of calls.values()) c.reject(new Error("VM arrêtée"));
  calls.clear();
  vm.status = "off";
  vm.owner = "";
  vm.busy = false;
}

const size = (/** @type {number} */ n) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)} Mo` : `${Math.ceil(n / 1e3)} Ko`);

/** Garder /work de la conversation de la VM. Rend une ligne pour le résultat. */
export async function save() {
  clearTimeout(saveTimer);
  if (!frame || vm.status !== "ready" || !vm.owner) return "";
  const owner = vm.owner;
  const r = await call("tar -czf - . 2>/dev/null", { timeout: 120 });
  if (vm.owner !== owner) return "";
  if (r.exitCode) return (vm.saved = "/work n'a pas pu être archivé");
  if (r.log.length > MAX_WORK) return (vm.saved = `/work non gardé : ${size(r.log.length)} compressé, plus de ${size(MAX_WORK)}`);
  const kept = await sqlbase.save(key(owner), r.log, !incognito);
  return (vm.saved = `/work gardé avec la conversation (${size(r.log.length)})`
    + (kept ? "" : " en mémoire seulement : perdu au rechargement"));
}

/** La VM de cette conversation, démarrée : celle d'une autre est
 * arrêtée, son /work gardé d'abord. @param {any} conv */
export async function use(conv) {
  if (vm.status === "failed") teardown();
  // Le réseau a changé pour cette conversation : une VM neuve, /work gardé.
  if (frame && vm.owner === conv.id && vm.status === "ready" && vm.network !== !!conv.linuxNetwork) {
    await save().catch(() => {});
    teardown();
  }
  if (frame && vm.owner === conv.id && vm.status === "ready") return;
  if (starting && vm.owner === conv.id) return starting;
  if (vm.owner && vm.owner !== conv.id) { await save().catch(() => {}); teardown(); }
  window.removeEventListener("message", onMessage);
  window.addEventListener("message", onMessage);
  const gen = generation;
  starting = start(conv).catch((e) => { if (gen === generation) fail(e.message); throw e; });
  return starting;
}

// Comment ouvrir le panneau Terminal (ChatPanel) : rend vrai s'il l'a
// ouvert (la conversation est celle affichée).
let opener = (/** @type {any} */ conv) => false;
/** @param {(conv: any) => boolean} fn */
export function setOpener(fn) { opener = fn; }

/** Le modèle se sert de la VM de `conv` : le panneau Terminal s'ouvre
 * s'il ne l'est pas (la conversation affichée) — il démarre la VM et se
 * pose le temps d'une image ou deux. @param {any} conv */
async function reveal(conv) {
  if (slot || !opener(conv)) return;
  for (let i = 0; i < 20 && !slot; i++) await new Promise((r) => setTimeout(r, 25));
}

/** Le terminal de `conv` est-il sous les yeux de l'utilisateur (panneau
 * ouvert sur sa VM, démarrée) ? @param {any} conv */
export const visible = (conv) => !!slot && !!frame && vm.owner === conv.id && vm.status === "ready";

const MAX_TYPED = 6000;              // caractères d'un script tapé dans le terminal

/**
 * Un script du modèle (run_shell), dans la VM de `conv`. Panneau ouvert
 * (visible) : il est TAPÉ dans le terminal de l'utilisateur (typeExec) —
 * une ligne telle quelle, un script de plusieurs lignes en un bloc
 * `( … )` (bash l'affiche ligne à ligne, l'exécute d'un coup ; un sous-
 * shell : son `exit` ou son `cd` ne touchent pas celui de l'utilisateur).
 * Le canal invisible sinon : `terminal: false`, un terminal pas libre
 * (une commande en cours, une ligne entamée), un script de plus de
 * MAX_TYPED caractères ou avec des tabulations (tapées, elles complètent).
 * Rend aussi `terminal` (tapé), `running` (toujours en cours dans le
 * terminal), `busy` (terminal pas libre), `long` (trop long pour lui).
 * @param {any} conv @param {string} script @param {any} opts
 * @returns {Promise<{log: Uint8Array, exitCode: number, outputs: {name: string, bytes: Uint8Array}[],
 *   terminal?: boolean, running?: boolean, busy?: boolean, long?: boolean}>}
 */
export async function exec(conv, script, opts) {
  await reveal(conv);
  await use(conv);
  vm.busy = true;
  try {
    const line = script.trim();
    const typed = line.includes("\n") ? `(\n${line}\n)` : line;
    if (opts.terminal === false || !visible(conv) || !line) return await call(script, opts);
    if (typed.length > MAX_TYPED || typed.includes("\t")) return { ...(await call(script, opts)), long: true };
    // Les fichiers fournis et le repère de /out d'abord, par le canal ; puis
    // la commande, tapée ; puis ce qu'elle a écrit sous /out depuis le repère.
    const mark = crypto.randomUUID().replace(/-/g, "").slice(0, 12);
    await call(":", { timeout: 60, inputs: opts.inputs || [], mark });
    vm.typing = true;
    let r;
    try { r = await call(typed, { timeout: opts.timeout }, "typeExec"); } finally { vm.typing = false; }
    if (r.busy) return { ...(await call(script, { ...opts, inputs: [] })), busy: true };
    const o = opts.out ? await call(":", { timeout: 60, out: true, mark }) : { outputs: [] };
    return { ...r, outputs: o.outputs, terminal: true };
  } finally {
    vm.busy = false;
  }
}

// ── les fichiers de la VM (shell_read, shell_grep, shell_edit, shell_write) ──
const MAX_TEXT = 2e6;                // octets d'un fichier lu ou écrit
/** Un chemin du modèle : absolu, ou relatif à /work. @param {string} path */
export const absolute = (path) => {
  const p = String(path || "").trim() || ".";
  return (p.startsWith("/") ? p : `/work/${p}`).replace(/\/\.?(?=\/|$)/g, "").replace(/\/+/g, "/") || "/";
};
const shq = (/** @type {string} */ s) => "'" + s.replace(/'/g, "'\\''") + "'";
const isText = (/** @type {Uint8Array} */ b) => !b.subarray(0, 8192).includes(0);

/** Le texte d'un fichier de la VM de `conv` ; lève une Error lisible
 * (absent, dossier, binaire, trop gros). @param {any} conv @param {string} path */
export async function readFile(conv, path) {
  await reveal(conv);
  await use(conv);
  const f = absolute(path);
  const files = await call(f, { maxFiles: 1, maxSize: MAX_TEXT + 1 }, "readFiles").then((r) => r.outputs);
  const file = files.find((x) => x.name === f);
  if (!file) throw new Error(`« ${f} » est un dossier`);
  if (file.bytes.length > MAX_TEXT) throw new Error(`« ${f} » : plus de ${MAX_TEXT / 1e6} Mo`);
  if (!isText(file.bytes)) throw new Error(`« ${f} » est un fichier binaire`);
  return new TextDecoder().decode(file.bytes);
}

/** Les fichiers texte sous `path` (un fichier, ou un dossier : récursif,
 * cachés exclus) → [{name, text}]. @param {any} conv @param {string} path */
export async function readTexts(conv, path) {
  await reveal(conv);
  await use(conv);
  const r = await call(absolute(path), { maxFiles: 500, maxSize: 1_000_000 }, "readFiles");
  return r.outputs.filter((f) => isText(f.bytes)).map((f) => ({ name: f.name, text: new TextDecoder().decode(f.bytes) }));
}

/** Écrire `text` dans un fichier de la VM de `conv` (ses dossiers créés ;
 * un fichier existant garde ses droits). @param {any} conv @param {string} path @param {string} text */
export async function writeFile(conv, path, text) {
  await reveal(conv);
  await use(conv);
  const f = absolute(path);
  const bytes = new TextEncoder().encode(text);
  if (bytes.length > MAX_TEXT) throw new Error(`plus de ${MAX_TEXT / 1e6} Mo`);
  const tmp = `.vm-write-${crypto.randomUUID().slice(0, 8)}`;
  const r = await call(`f=${shq(f)}; mkdir -p "$(dirname "$f")" && cat /work/${tmp} > "$f"; rc=$?; rm -f /work/${tmp}; exit $rc`,
                       { timeout: 60, inputs: [{ name: tmp, bytes }] });
  if (r.exitCode) throw new Error(new TextDecoder().decode(r.log).trim() || `écriture de « ${f} » impossible`);
  return { path: f, bytes: bytes.length, lines: text.split("\n").length };
}

/** Redémarrer la VM de `conv` : /work gardé, ou vidé (`wipe`). @param {any} conv @param {{wipe?: boolean}} [o] */
export async function restart(conv, { wipe = false } = {}) {
  if (wipe) { teardown(); await sqlbase.drop(key(conv.id)); }
  else if (frame) { await save().catch(() => {}); teardown(); }
  return use(conv);
}

/** Ouvrir ou couper le réseau de la VM de `conv` (gardé avec la
 * conversation) ; démarrée, elle redémarre, /work gardé.
 * @param {any} conv @param {boolean} on */
export async function setNetwork(conv, on) {
  conv.linuxNetwork = !!on;
  if (frame && vm.owner === conv.id) return use(conv);
}

/** Arrêter la VM, /work gardé. */
export async function stop() {
  await save().catch(() => {});
  teardown();
}

/** La conversation est supprimée : son /work aussi. @param {string} convId */
export async function forget(convId) {
  if (vm.owner === convId) teardown();
  await sqlbase.drop(key(convId));
}

/** /work de la conversation de la VM, en tar.gz (le bouton Télécharger). */
export async function archive() {
  const r = await call("tar -czf - . 2>/dev/null", { timeout: 120 });
  if (r.exitCode) throw new Error("archive impossible");
  return r.log;
}

// ── la place du cadre ───────────────────────────────────────────────────
// Le cadre reste dans <body> ; attaché, il suit la boîte du panneau à
// chaque image ; détaché, il est hors de l'écran (sa taille gardée :
// le terminal ne se recalcule pas à zéro colonne).
/** @type {HTMLElement | null} */
let slot = null;
let raf = 0;
let last = "";
const GRIP = 5;

function park(/** @type {HTMLIFrameElement} */ f) {
  Object.assign(f.style, { position: "fixed", left: "-10000px", top: "0", width: "640px", height: "400px",
                           border: "0", zIndex: "7", visibility: "hidden" });
  f.setAttribute("aria-hidden", "true");
}

function follow() {
  raf = 0;
  if (!slot) return;
  raf = requestAnimationFrame(follow);
  if (!frame) return;
  if (frame.tabIndex !== 0) { frame.tabIndex = 0; frame.removeAttribute("aria-hidden"); last = ""; }
  const r = slot.getBoundingClientRect();
  const k = `${r.left},${r.top},${r.width},${r.height}`;
  if (k !== last) {
    const shown = !!(r.width && r.height) && frame.style.visibility !== "visible";
    last = k;
    // Décalé de GRIP px : la poignée de largeur du panneau (son bord gauche) reste à la souris.
    Object.assign(frame.style, { left: `${r.left + GRIP}px`, top: `${r.top}px`, width: `${Math.max(0, r.width - GRIP)}px`, height: `${r.height}px`,
                                 visibility: r.width && r.height ? "visible" : "hidden" });
    if (shown && frame.contentWindow) frame.contentWindow.postMessage({ op: "shown" }, "*");
  }
}

/** Le cadre par-dessus `el` (le corps du panneau Terminal) — seulement
 * quand la VM est celle de la conversation du panneau (LinuxPanel).
 * @param {HTMLElement} el */
export function attach(el) {
  slot = el;
  last = "";
  if (!raf) raf = requestAnimationFrame(follow);
}

export function detach() {
  slot = null;
  cancelAnimationFrame(raf);
  raf = 0;
  if (frame) { park(frame); frame.tabIndex = -1; }
}

/** Le focus au terminal. */
export function focus() {
  if (frame && frame.contentWindow) { frame.focus(); frame.contentWindow.postMessage({ op: "focus" }, "*"); }
}

// Le thème de la page change : celui du terminal suit.
if (typeof window !== "undefined" && window.matchMedia) {
  const retheme = () => { if (frame && frame.contentWindow) frame.contentWindow.postMessage({ op: "theme", theme: theme() }, "*"); };
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", retheme);
  if (typeof MutationObserver !== "undefined" && document.documentElement) {
    new MutationObserver(retheme).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  }
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") save().catch(() => {}); });
}
