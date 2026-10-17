/*
  Les outils de l'APPAREIL : ce que le modèle peut demander au navigateur.

    device_context  la date et l'heure locales, le fuseau, la langue —
                    aussitôt (rien de personnel)
    get_location    la position : ATTEND l'utilisateur (une carte dans le
                    fil : approximative, précise ou refus) ; le navigateur
                    demande en plus sa propre permission la première fois
    notify          une notification du navigateur, tout de suite ou
                    dans N minutes (un rappel, tant que la page reste
                    ouverte) ; pas encore permises, une carte propose de
                    les autoriser (le navigateur exige ce clic)

  La position n'est jamais lue sans un clic sur la carte : une permission
  du navigateur déjà accordée n'y suffit pas.
*/
import * as notifier from "./notify.js";

const MAX_DELAY = 24 * 60;          // minutes d'un rappel, au plus
const APPROX = 2;                   // décimales d'une position approximative (≈ 1 km)

export const TOOLS = [
  { type: "function", function: {
    name: "device_context",
    description: "Donne la date et l'heure locales de l'utilisateur, son fuseau horaire et "
      + "sa langue. À utiliser avant de parler d'« aujourd'hui », de « demain » ou d'une heure.",
    parameters: { type: "object", properties: {} } } },
  { type: "function", function: {
    name: "get_location",
    description: "Demande à l'utilisateur sa position (latitude, longitude), qu'il accepte ou "
      + "refuse sur une carte dans le chat. Seulement quand la réponse en dépend vraiment "
      + "(météo, itinéraire, lieux proches) ; dire pourquoi dans `reason`.",
    parameters: { type: "object", required: ["reason"], properties: {
      reason: { type: "string", description: "Pourquoi la position est utile, en une phrase, montrée à l'utilisateur." },
    } } } },
  { type: "function", function: {
    name: "notify",
    description: "Envoie une notification du navigateur à l'utilisateur, tout de suite ou "
      + "après un délai (un rappel : « rappelle-moi dans 20 minutes »). Le rappel n'arrive "
      + "que si la page reste ouverte.",
    parameters: { type: "object", required: ["title"], properties: {
      title: { type: "string", description: "Le titre, court." },
      body: { type: "string", description: "Le texte (facultatif)." },
      delay_minutes: { type: "number", description: `Dans combien de minutes (0 à ${MAX_DELAY}) ; 0 ou absent : tout de suite.` },
    } } } },
];
export const TOOL_NAMES = new Set(TOOLS.map((t) => t.function.name));
// Attend l'utilisateur : la position toujours, une notification tant que
// le navigateur ne les a ni permises ni refusées.
export const waits = (name) => name === "get_location"
  || (name === "notify" && notifier.supported() && Notification.permission === "default");

// ── aussitôt ────────────────────────────────────────────────────────────
export function run(name, args) {
  if (name === "device_context") return context();
  if (name === "notify") return notify(args);
  return `Error: outil inconnu « ${name} ».`;
}

function context() {
  const now = new Date();
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const local = now.toLocaleString("fr-FR", { dateStyle: "full", timeStyle: "short" });
  const offset = -now.getTimezoneOffset();
  const sign = offset >= 0 ? "+" : "-";
  const hh = String(Math.floor(Math.abs(offset) / 60)).padStart(2, "0");
  const mm = String(Math.abs(offset) % 60).padStart(2, "0");
  return [`Date et heure locales : ${local} (UTC${sign}${hh}:${mm})`,
          `Fuseau horaire : ${zone}`,
          `Langues : ${(navigator.languages || [navigator.language]).join(", ")}`].join("\n");
}

function notify(args) {
  const title = String(args.title || "").trim().slice(0, 120);
  if (!title) return "Error: `title` attendu.";
  if (!notifier.supported()) return "Error: ce navigateur n'a pas de notifications.";
  if (Notification.permission !== "granted") {
    return "Error: notifications refusées par le navigateur (l'utilisateur peut les "
      + "rétablir dans les paramètres du site).";
  }
  const body = String(args.body || "").slice(0, 300);
  const delay = Math.max(0, Math.min(MAX_DELAY, Number(args.delay_minutes) || 0));
  if (!delay) {
    notifier.show(title, body);
    return "Notification envoyée.";
  }
  setTimeout(() => notifier.show(title, body), delay * 60_000);
  const at = new Date(Date.now() + delay * 60_000)
    .toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
  return `Rappel programmé pour ${at} (dans ${delay} min), s'il garde la page ouverte.`;
}

// ── en attente de l'utilisateur ─────────────────────────────────────────
export function prepare(name, args) {
  if (name === "notify") {
    const title = String(args.title || "").trim().slice(0, 120);
    if (!title) return { error: "Error: `title` attendu." };
    return { kind: "notify", title, args };
  }
  if (name !== "get_location") return { error: `Error: outil inconnu « ${name} ».` };
  if (!("geolocation" in navigator)) return { error: "Error: ce navigateur ne donne pas de position." };
  return { kind: "location", reason: String(args.reason || "").trim().slice(0, 300) };
}

// La position, après le choix de l'utilisateur → le résultat de l'outil.
export function locate(precise) {
  return new Promise((resolve, reject) => {
    navigator.geolocation.getCurrentPosition((p) => {
      const { latitude, longitude, accuracy } = p.coords;
      if (!precise) {
        resolve(`Position approximative (≈ 1 km) : latitude ${latitude.toFixed(APPROX)}, `
          + `longitude ${longitude.toFixed(APPROX)}.`);
        return;
      }
      resolve(`Position : latitude ${latitude.toFixed(5)}, longitude ${longitude.toFixed(5)} `
        + `(précision ≈ ${Math.round(accuracy)} m).`);
    }, (e) => reject(new Error(e.code === 1 ? "permission refusée par le navigateur"
      : e.code === 3 ? "délai dépassé" : "position indisponible")),
    { enableHighAccuracy: precise, timeout: 20_000, maximumAge: 300_000 });
  });
}

export const REFUSED = "L'utilisateur a refusé de donner sa position.";

// La carte d'une notification : la permission demandée sur ce clic, puis
// la notification envoyée (ou programmée).
export async function allowAndNotify(args) {
  if ((await Notification.requestPermission()) !== "granted") return NOTIFY_REFUSED;
  return notify(args);
}
export const NOTIFY_REFUSED = "L'utilisateur n'a pas permis les notifications.";
