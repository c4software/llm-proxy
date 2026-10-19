// @ts-check
/*
  Le BAC commun des outils de code (JavaScript, PHP, SQL) : le code du
  modèle tourne dans un Worker, créé dans un cadre `sandbox` sans origine
  — ni la page, ni sa session, ni ses cookies — sous une politique qui
  coupe tout réseau (le Worker en hérite). Le Worker garde la page
  réactive même si le code boucle ; le cadre, et le Worker avec lui, est
  supprimé à la fin, ou au délai dépassé.

  Un runtime (vendor/<nom>.js, et ses binaires s'il en a ; construits
  par proxy/wasm) n'est téléchargé qu'au premier appel, par la PAGE — le bac n'a pas le
  droit de le faire —, gardé en mémoire, puis passé au bac à chaque appel.

  exec({driver, input, runtime, timeout, lib}) → {ok, value, error, logs}
    driver    une fonction async (input, {mod, wasm, bins, mods, lib}) →
              valeur, exécutée dans le Worker : passée en TEXTE, elle ne
              voit rien de ce module ; `mod` est le module du (premier)
              runtime, `bins` ses binaires par nom de fichier, `wasm` le
              premier d'entre eux, `mods` chaque module par son nom
    input     ce qu'elle reçoit (clonable)
    runtime   le nom d'un runtime de RUNTIMES, une liste de noms, ou rien
    lib       des fonctions d'aide {nom: fonction}, passées en texte comme
              `driver` (aucune ne voit ce module) : `lib.nom` dans le Worker
    logs      ce que le code a écrit sur la console
*/

import { vendorFetch } from "./vendor.js";

const WORKER = `
const fmt = (v) => { if (typeof v === "string") return v;
  try { return JSON.stringify(v, null, 2) ?? String(v); } catch { return String(v); } };
const logs = [];
for (const k of ["log", "info", "warn", "error", "debug"]) console[k] = (...a) => logs.push(a.map(fmt).join(" "));
self.onmessage = async ({ data: { driver, input, runtimes, lib } }) => {
  try {
    const mods = {};
    for (const r of runtimes) {
      mods[r.name] = await import(URL.createObjectURL(new Blob([r.js], { type: "text/javascript" })));
    }
    const first = runtimes[0];
    const bins = first ? first.bins : {};
    const helpers = {};
    for (const [k, src] of Object.entries(lib)) helpers[k] = (0, eval)("(" + src + ")");
    const drive = (0, eval)("(" + driver + ")");
    const value = await drive(input, { mod: first ? mods[first.name] : null, wasm: Object.values(bins)[0],
                                       bins, mods, lib: helpers, fmt });
    postMessage({ ok: true, value, logs });
  } catch (err) {
    // Une Error simple : son message seul (« Error: » est ajouté par l'outil).
    postMessage({ ok: false, error: err && err.name === "Error" ? err.message : String(err), logs });
  }
};`;

const FRAME = `<!doctype html><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval' 'wasm-unsafe-eval' blob:; worker-src blob:">
<script>
const SOURCE = ${JSON.stringify(WORKER)};
onmessage = (e) => {
  if (e.source !== parent) return;
  // Un Worker classique : un Worker module est refusé sans origine (le
  // runtime, lui, se charge par import()).
  const worker = new Worker(URL.createObjectURL(new Blob([SOURCE], { type: "text/javascript" })));
  worker.onmessage = (m) => parent.postMessage(m.data, "*");
  worker.onerror = (err) => { err.preventDefault(); parent.postMessage({ ok: false, error: String(err.message || "erreur du bac"), logs: [] }, "*"); };
  worker.postMessage(e.data, e.data.runtimes.flatMap((r) => Object.values(r.bins)));
};
parent.postMessage({ ready: true }, "*");
<\/script>`;

// Les runtimes de vendor/ : leurs binaires, à côté de <nom>.js.
const RUNTIMES = { php: ["php.wasm"], sql: ["sql.wasm"], pglite: ["pglite.wasm", "initdb.wasm", "pglite.data"],
                   chart: [], sucrase: [], typescript: [] };

// nom → promesse de {js, bins} ; un échec est oublié (réessayé au suivant).
const runtimes = new Map();

function load(name) {
  if (!runtimes.has(name)) {
    const get = vendorFetch;
    const files = RUNTIMES[name];
    const p = Promise.all([get(`${name}.js`, "text"), ...files.map((f) => get(f, "arrayBuffer"))])
      .then(([js, ...bins]) => ({ js, bins: Object.fromEntries(files.map((f, i) => [f, bins[i]])) }));
    p.catch(() => runtimes.delete(name));
    runtimes.set(name, p);
  }
  return runtimes.get(name);
}

export async function exec({ driver, input, runtime, timeout, lib = {} }) {
  const names = !runtime ? [] : Array.isArray(runtime) ? runtime : [runtime];
  const rts = [];
  for (const name of names) {
    try {
      rts.push({ name, ...(await load(name)) });
    } catch (e) {
      return { ok: false, logs: [], error: `runtime « ${name} » absent de ce serveur (${e.message}) : `
        + "il est construit par proxy/wasm (npm ci && npm run build), ce que fait l'image Docker." };
    }
  }
  return new Promise((resolve) => {
    const frame = document.createElement("iframe");
    frame.setAttribute("sandbox", "allow-scripts");
    frame.style.display = "none";
    let timer = null;
    const finish = (reply) => {
      clearTimeout(timer);
      window.removeEventListener("message", onMessage);
      frame.remove();
      resolve(reply);
    };
    function onMessage(e) {
      if (e.source !== frame.contentWindow) return;
      const d = e.data || {};
      if (!d.ready) { finish({ logs: [], ...d }); return; }
      // Une copie des binaires : celle du cache reste à la page.
      const sent = rts.map((r) => ({ name: r.name, js: r.js,
        bins: Object.fromEntries(Object.entries(r.bins).map(([f, b]) => [f, b.slice(0)])) }));
      const helpers = Object.fromEntries(Object.entries(lib).map(([k, f]) => [k, String(f)]));
      const msg = { driver: String(driver), input, runtimes: sent, lib: helpers };
      frame.contentWindow.postMessage(msg, "*", sent.flatMap((r) => Object.values(r.bins)));
    }
    window.addEventListener("message", onMessage);
    timer = setTimeout(() => finish({ ok: false, logs: [], error: `arrêté après ${timeout / 1000} s.` }), timeout);
    frame.srcdoc = FRAME;
    document.body.appendChild(frame);
  });
}
