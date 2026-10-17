// Calcul : du JavaScript exécuté dans un BAC isolé — un cadre `sandbox`
// sans origine (ni la page, ni sa session, ni ses cookies), sous une
// politique qui coupe tout réseau ; supprimé après 10 s au plus.
import { fn, clip } from "./common.js";

const TIMEOUT = 10_000;
const MAX_OUT = 20_000;

export const FAMILIES = [{
  key: "compute", label: "Calcul", hint: "JavaScript dans un bac isolé, sans réseau",
  tools: [fn("run_javascript", "Exécute du JavaScript dans un bac isolé du navigateur (aucun "
    + "réseau, aucun accès à la page), pour calculer exactement : arithmétique, dates, "
    + "statistiques, transformation de données. Le code est le corps d'une fonction async : "
    + "`return` la valeur voulue ; console.log est rendu aussi. 10 s au plus.",
  { code: { type: "string", description: "Le corps de la fonction (avec `return`)." } }, ["code"])],
}];

export const waits = () => false;

const RUNNER = `<!doctype html><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval'">
<script>
const fmt = (v) => { if (typeof v === "string") return v;
  try { return JSON.stringify(v, null, 2) ?? String(v); } catch { return String(v); } };
const logs = [];
for (const k of ["log", "info", "warn", "error"]) console[k] = (...a) => logs.push(a.map(fmt).join(" "));
window.onmessage = async (e) => {
  try {
    const run = new Function("return (async () => {\\n" + e.data + "\\n})()");
    const value = await run();
    parent.postMessage({ ok: true, value: value === undefined ? undefined : fmt(value), logs }, "*");
  } catch (err) {
    parent.postMessage({ ok: false, error: String(err && err.stack || err), logs }, "*");
  }
};
parent.postMessage({ ready: true }, "*");
<\/script>`;

export function run(name, args) {
  const code = String(args.code || "");
  if (!code.trim()) return "Error: `code` attendu.";
  return new Promise((resolve) => {
    const frame = document.createElement("iframe");
    frame.setAttribute("sandbox", "allow-scripts");
    frame.style.display = "none";
    let timer = null;
    const finish = (text) => {
      clearTimeout(timer);
      window.removeEventListener("message", onMessage);
      frame.remove();
      resolve(clip(text, MAX_OUT));
    };
    function onMessage(e) {
      if (e.source !== frame.contentWindow) return;
      const d = e.data || {};
      if (d.ready) { frame.contentWindow.postMessage(code, "*"); return; }
      const out = [];
      if (d.ok) out.push(d.value === undefined ? "(aucune valeur rendue)" : "Résultat : " + d.value);
      else out.push("Error: " + d.error);
      if (d.logs && d.logs.length) out.push("Console :\n" + d.logs.join("\n"));
      finish(out.join("\n"));
    }
    window.addEventListener("message", onMessage);
    timer = setTimeout(() => finish(`Error: arrêté après ${TIMEOUT / 1000} s.`), TIMEOUT);
    frame.srcdoc = RUNNER;
    document.body.appendChild(frame);
  });
}
