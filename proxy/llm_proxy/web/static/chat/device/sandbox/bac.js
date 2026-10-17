/*
  Le BAC commun des outils de code (JavaScript, PHP, SQL) : le code du
  modèle tourne dans un Worker, créé dans un cadre `sandbox` sans origine
  — ni la page, ni sa session, ni ses cookies — sous une politique qui
  coupe tout réseau (le Worker en hérite). Le Worker garde la page
  réactive même si le code boucle ; le cadre, et le Worker avec lui, est
  supprimé à la fin, ou au délai dépassé.

  Un runtime WASM (vendor/<nom>.js et .wasm, construits par proxy/wasm)
  n'est téléchargé qu'au premier appel, par la PAGE — le bac n'a pas le
  droit de le faire —, gardé en mémoire, puis passé au bac à chaque appel.

  exec({driver, input, runtime, timeout}) → {ok, value, error, logs}
    driver    une fonction async (input, {mod, wasm}) → valeur, exécutée
              dans le Worker : passée en TEXTE, elle ne voit rien de ce
              module ; `mod` est le module du runtime, `wasm` son binaire
    input     ce qu'elle reçoit (clonable)
    runtime   le nom d'un runtime de vendor/, ou rien
    logs      ce que le code a écrit sur la console
*/

// Hors du préfixe versionné (ui_static.py) : l'empreinte change à chaque
// mise à jour de la page, un runtime presque jamais — revalidé, il rend
// 304 sans être retéléchargé.
const VENDOR = new URL("/ui/static/vendor/", location.href);

const WORKER = `
const fmt = (v) => { if (typeof v === "string") return v;
  try { return JSON.stringify(v, null, 2) ?? String(v); } catch { return String(v); } };
const logs = [];
for (const k of ["log", "info", "warn", "error", "debug"]) console[k] = (...a) => logs.push(a.map(fmt).join(" "));
self.onmessage = async ({ data: { driver, input, runtime } }) => {
  try {
    let mod = null;
    if (runtime) {
      const url = URL.createObjectURL(new Blob([runtime.js], { type: "text/javascript" }));
      mod = await import(url);
    }
    const drive = (0, eval)("(" + driver + ")");
    const value = await drive(input, { mod, wasm: runtime && runtime.wasm, fmt });
    postMessage({ ok: true, value, logs });
  } catch (err) {
    postMessage({ ok: false, error: String(err), logs });
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
  const runtime = e.data.runtime;
  worker.postMessage(e.data, runtime ? [runtime.wasm] : []);
};
parent.postMessage({ ready: true }, "*");
<\/script>`;

// nom → promesse de {js, wasm} ; un échec est oublié (réessayé au suivant).
const runtimes = new Map();

function load(name) {
  if (!runtimes.has(name)) {
    const get = async (file, as) => {
      const r = await fetch(new URL(file, VENDOR));
      if (!r.ok) throw new Error(`${file} : HTTP ${r.status}`);
      return r[as]();
    };
    const p = Promise.all([get(`${name}.js`, "text"), get(`${name}.wasm`, "arrayBuffer")])
      .then(([js, wasm]) => ({ js, wasm }));
    p.catch(() => runtimes.delete(name));
    runtimes.set(name, p);
  }
  return runtimes.get(name);
}

export async function exec({ driver, input, runtime, timeout }) {
  let rt = null;
  if (runtime) {
    try {
      rt = await load(runtime);
    } catch (e) {
      return { ok: false, logs: [], error: `runtime « ${runtime} » absent de ce serveur (${e.message}) : `
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
      // Une copie du binaire : celle du cache reste à la page.
      const msg = { driver: String(driver), input, runtime: rt && { js: rt.js, wasm: rt.wasm.slice(0) } };
      frame.contentWindow.postMessage(msg, "*", msg.runtime ? [msg.runtime.wasm] : []);
    }
    window.addEventListener("message", onMessage);
    timer = setTimeout(() => finish({ ok: false, logs: [], error: `arrêté après ${timeout / 1000} s.` }), timeout);
    frame.srcdoc = FRAME;
    document.body.appendChild(frame);
  });
}
