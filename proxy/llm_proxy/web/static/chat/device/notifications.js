// @ts-check
// Notifications : tout de suite, ou un rappel dans N minutes (tant que la
// page reste ouverte). Pas encore permises : une carte propose de les
// autoriser — le navigateur n'en demande la permission que sur un clic.
import { TOOL, FAMILY, defineTool, defineFamily, refused, failure } from "./contract.js";
import { show } from "../notify.js";

const MAX_DELAY = 24 * 60;          // minutes d'un rappel, au plus
const supported = () => "Notification" in window;

function send(args) {
  const title = String(args.title || "").trim().slice(0, 120);
  if (!title) return failure("`title` attendu.");
  if (!supported()) return failure("ce navigateur n'a pas de notifications.");
  if (Notification.permission !== "granted") {
    return failure("notifications refusées par le navigateur (l'utilisateur peut les rétablir "
      + "dans les paramètres du site).");
  }
  const body = String(args.body || "").slice(0, 300);
  const delay = Math.max(0, Math.min(MAX_DELAY, Number(args.delay_minutes) || 0));
  if (!delay) { show(title, body); return "Notification envoyée."; }
  setTimeout(() => show(title, body), delay * 60_000);
  const at = new Date(Date.now() + delay * 60_000)
    .toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
  return `Rappel programmé pour ${at} (dans ${delay} min), s'il garde la page ouverte.`;
}

const notify = defineTool({
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
  summary: (a) => [a.title, a.delay_minutes ? "dans " + a.delay_minutes + " min" : ""]
    .filter(Boolean).join(" · "),
  // La permission manque : une carte la demande d'abord.
  waits: () => supported() && Notification.permission === "default",
  run: send,
  prepare: (args) => (String(args.title || "").trim() ? {} : { error: failure("`title` attendu.") }),
  view: (w) => ({
    title: "Le modèle veut vous envoyer une notification",
    detail: `« ${String(w.args.title).trim()} »` + (w.args.delay_minutes ? ` — dans ${w.args.delay_minutes} min` : ""),
    actions: [{ id: "allow", label: "Autoriser les notifications", primary: true },
              { id: "refuse", label: "Non merci" }],
  }),
  async act(w, action) {
    if (action === "refuse") return refused("les notifications");
    if ((await Notification.requestPermission()) !== "granted") return refused("les notifications");
    return { result: send(w.args) };
  },
});

export const FAMILIES = [defineFamily({
  key: FAMILY.NOTIFY, label: "Notifications", hint: "tout de suite, ou un rappel dans N minutes", on: true,
  available: supported,
  tools: [notify],
})];
