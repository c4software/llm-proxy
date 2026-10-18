// @ts-check
// Notifications : tout de suite, ou un rappel dans N minutes (tant que la
// page reste ouverte). Pas encore permises : une carte propose de les
// autoriser — le navigateur n'en demande la permission que sur un clic.
// Chaque notification laisse une CARTE DURABLE : un rappel programmé
// s'annule ; perdu par un rechargement, il se reprogramme ; passé, il se
// renvoie.
import { TOOL, FAMILY, PageTool, defineFamily, refused, failure } from "../contract.js";
import { show } from "../../notify.js";

/** @typedef {import("../contract.js").Card} Card */
/** @typedef {import("../contract.js").Kept} Kept */

const MAX_DELAY = 24 * 60;          // minutes d'un rappel, au plus
const supported = () => "Notification" in window;
const granted = () => supported() && Notification.permission === "granted";
// Les rappels programmés dans CETTE page : référence → minuteur.
const timers = Vue.reactive(new Map());

/** @param {number} at */
const hour = (at) => new Date(at).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });

/** @param {string} ref @param {string} title @param {string} body @param {number} at */
function schedule(ref, title, body, at) {
  clearTimeout(timers.get(ref));
  timers.set(ref, setTimeout(() => { timers.delete(ref); show(title, body); }, Math.max(0, at - Date.now())));
}

/** @param {any} args @returns {import("../contract.js").RunResult} */
function send(args) {
  const title = String(args.title || "").trim().slice(0, 120);
  if (!title) return failure("`title` attendu.");
  if (!supported()) return failure("ce navigateur n'a pas de notifications.");
  if (!granted()) {
    return failure("notifications refusées par le navigateur (l'utilisateur peut les rétablir "
      + "dans les paramètres du site).");
  }
  const body = String(args.body || "").slice(0, 300);
  const delay = Math.max(0, Math.min(MAX_DELAY, Number(args.delay_minutes) || 0));
  const ref = crypto.randomUUID();
  if (!delay) {
    show(title, body);
    return { result: "Notification envoyée.", keep: { ref, title, body, at: 0 } };
  }
  const at = Date.now() + delay * 60_000;
  schedule(ref, title, body, at);
  return { result: `Rappel programmé pour ${hour(at)} (dans ${delay} min), s'il garde la page ouverte.`,
           keep: { ref, title, body, at } };
}

class Notify extends PageTool {
  constructor() {
    super({
      name: TOOL.NOTIFY,
      description: "Envoie une notification du navigateur à l'utilisateur, tout de suite ou après "
        + "un délai (un rappel : « rappelle-moi dans 20 minutes »). Le rappel n'arrive que si la "
        + "page reste ouverte ; pour un rendez-vous, préférer add_to_calendar.",
      params: {
        title: { type: "string", description: "Le titre, court." },
        body: { type: "string", description: "Le texte (facultatif)." },
        delay_minutes: { type: "number", description: `Dans combien de minutes (0 à ${MAX_DELAY}) ; 0 ou absent : tout de suite.` },
      },
      required: ["title"],
    });
  }

  // La permission manque : une carte la demande d'abord ; sinon, aussitôt.
  /** @override */
  waits() { return supported() && Notification.permission === "default"; }
  /** @override @param {any} args */
  run(args) { return send(args); }
  /** @override @param {any} args */
  prepare(args) { return String(args.title || "").trim() ? {} : { error: failure("`title` attendu.") }; }

  /** @override @param {any} a */
  summary(a) {
    return [a.title, a.delay_minutes ? "dans " + a.delay_minutes + " min" : ""].filter(Boolean).join(" · ");
  }

  /** @override @param {Card} w */
  view(w) {
    return {
      title: "Le modèle veut vous envoyer une notification",
      detail: `« ${String(w.args.title).trim()} »` + (w.args.delay_minutes ? ` — dans ${w.args.delay_minutes} min` : ""),
      actions: [{ id: "allow", label: "Autoriser les notifications", primary: true },
                { id: "refuse", label: "Non merci" }],
    };
  }

  /** @override @param {Card} w @param {string} action */
  async act(w, action) {
    if (action === "refuse") return refused("les notifications");
    if ((await Notification.requestPermission()) !== "granted") return refused("les notifications");
    const got = send(w.args);
    return typeof got === "string" ? { result: got } : got;
  }

  /** @override @param {Kept} k */
  keptView(k) {
    const quote = k.body ? `${k.title}\n${k.body}` : k.title;
    if (k.at && timers.has(k.ref)) {
      return { title: `Rappel programmé à ${hour(k.at)}`, quote,
               actions: [{ id: "cancel", label: "Annuler le rappel" }] };
    }
    if (k.at && k.at > Date.now()) {
      return { title: `Rappel prévu à ${hour(k.at)}`, quote, note: "perdu au rechargement de la page",
               actions: [{ id: "schedule", label: "Le reprogrammer", primary: true }] };
    }
    return { title: k.at ? `Rappel de ${hour(k.at)}` : "Notification", quote,
             actions: [{ id: "show", label: "Renvoyer maintenant" }] };
  }

  /** @override @param {Kept} k @param {string} action */
  async keptAct(k, action) {
    if (action === "cancel") {
      clearTimeout(timers.get(k.ref));
      timers.delete(k.ref);
      return "Rappel annulé.";
    }
    if (!granted() && (await Notification.requestPermission()) !== "granted") {
      return "Notifications refusées par le navigateur.";
    }
    if (action === "schedule") { schedule(k.ref, k.title, k.body, k.at); return null; }
    show(k.title, k.body);
    return "Envoyée.";
  }
}

export const FAMILIES = [defineFamily({
  key: FAMILY.NOTIFY, label: "Notifications", hint: "tout de suite, ou un rappel dans N minutes", on: true,
  available: supported,
  tools: [new Notify()],
})];
