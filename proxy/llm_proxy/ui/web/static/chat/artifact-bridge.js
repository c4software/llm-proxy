// @ts-check
/*
  Le PONT vers l'artifact affiché : ce qui permet au modèle de tester la
  page qu'il a écrite, telle qu'elle tourne dans le panneau (celle que
  l'utilisateur voit), pour la déboguer — inspect_artifact,
  run_in_artifact (tools/page/artifacts.js).

  Le cadre de l'artifact est isolé (sandbox sans allow-same-origin) : la
  page n'y lit rien. Son document porte un petit script (BRIDGE de
  artifacts.js) qui, dès le chargement, garde la console et les erreurs,
  et répond aux demandes de la page par postMessage. Seule la page qui l'a
  rendu lui parle (source vérifiée des deux côtés).

    register(id, version, frame)  le panneau affiche cette version (au chargement du cadre)
    unregister(frame)             le cadre disparaît
    call(id, op, payload)         une demande (inspect, run, shot) → la réponse ;
                                  ouvre le panneau sur l'artifact s'il n'y est pas,
                                  et attend que sa dernière version soit chargée
    setOpener(fn)                 comment ouvrir le panneau (ChatPanel)
    busy                          l'artifact en cours d'inspection (le bandeau du panneau)
*/
const WAIT_LOAD = 8_000;        // ms pour que le panneau charge l'artifact
const WAIT_REPLY = 20_000;      // ms d'une demande (une capture, un script)

/** @type {{id: string, version: number, frame: HTMLIFrameElement} | null} */
let shown = null;
/** @type {Array<() => void>} */
let waiting = [];
/** @type {(id: string) => void} */
let opener = () => {};

export const busy = Vue.ref("");

// La bibliothèque de capture (vendor/html-to-image.js), lue une fois et
// passée au cadre : il n'a pas le droit de la charger lui-même.
/** @type {Promise<string> | null} */
let captureLib = null;
function loadCaptureLib() {
  if (!captureLib) {
    captureLib = fetch(new URL("/ui/static/vendor/html-to-image.js", location.href)).then((r) => {
      if (!r.ok) throw new Error(`html-to-image absent de ce serveur (HTTP ${r.status}) : proxy/wasm, npm run build`);
      return r.text();
    });
    captureLib.catch(() => { captureLib = null; });
  }
  return captureLib;
}

/** @param {(id: string) => void} fn */
export function setOpener(fn) { opener = fn; }

/** @param {string} id @param {number} version @param {HTMLIFrameElement} frame */
export function register(id, version, frame) {
  shown = { id, version, frame };
  const w = waiting;
  waiting = [];
  w.forEach((f) => f());
}

/** @param {HTMLIFrameElement} frame */
export function unregister(frame) {
  if (shown && shown.frame === frame) shown = null;
}

// Le cadre de l'artifact `id`, à sa version `version` (au moins) — le
// panneau est ouvert dessus s'il le faut.
/** @param {string} id @param {number} version @returns {Promise<{frame: HTMLIFrameElement, version: number}>} */
function ready(id, version) {
  const ok = () => shown && shown.id === id && shown.version >= version && shown.frame.contentWindow;
  if (ok()) return Promise.resolve({ frame: shown.frame, version: shown.version });
  opener(id);
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      waiting = waiting.filter((f) => f !== check);
      // Une autre version affichée (l'utilisateur en a choisi une) : celle-là.
      if (shown && shown.id === id && shown.frame.contentWindow) resolve({ frame: shown.frame, version: shown.version });
      else reject(new Error("l'artifact ne s'est pas affiché dans le panneau (écran trop étroit, ou panneau de la mémoire ouvert)"));
    }, WAIT_LOAD);
    function check() {
      if (ok()) { clearTimeout(timer); resolve({ frame: shown.frame, version: shown.version }); }
      else waiting.push(check);
    }
    waiting.push(check);
  });
}

/** Une demande au pont de l'artifact `id` (dernière version `version`).
 * @param {string} id @param {number} version @param {string} op @param {object} [payload]
 * @returns {Promise<any>} la réponse, et `version` : celle qui a répondu */
export async function call(id, version, op, payload = {}) {
  if (op === "shot") payload = { ...payload, lib: await loadCaptureLib() };
  const { frame, version: got } = await ready(id, version);
  const ask = crypto.randomUUID();
  busy.value = id;
  try {
    return await new Promise((resolve, reject) => {
      const timer = setTimeout(() => { removeEventListener("message", onReply); reject(new Error("l'artifact ne répond pas")); }, WAIT_REPLY);
      function onReply(e) {
        if (e.source !== frame.contentWindow || !e.data || e.data.llmBridge !== ask) return;
        clearTimeout(timer);
        removeEventListener("message", onReply);
        resolve({ ...e.data, version: got });
      }
      addEventListener("message", onReply);
      frame.contentWindow.postMessage({ llmBridge: ask, op, ...payload }, "*");
    });
  } finally {
    busy.value = "";
  }
}
