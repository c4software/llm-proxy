/*
  Les formats d'affichage de la page, à la française.
*/

// Entier, espace comme séparateur de milliers.
export const num = (v) => String(Math.round(v) || 0).replace(/\B(?=(\d{3})+(?!\d))/g, " ");

// Un prix en dollars (`usage.cost`), approché (tarifs saisis, arrondi) :
// « ~0,0123 $ », deux décimales à partir d'un dollar ; « ~<0,0001 $ » pour
// un prix non nul plus petit ; « — » sans prix connu.
export function money(v) {
  if (v === null || v === undefined) return "—";
  if (v > 0 && v < 0.0001) return "~<0,0001 $";
  return "~" + v.toLocaleString("fr-FR", { minimumFractionDigits: v >= 1 ? 2 : 4,
                                           maximumFractionDigits: v >= 1 ? 2 : 4 }) + " $";
}

// Taille d'un fichier : Ko (au moins 1), ou Mo avec une décimale.
export const size = (n) => n >= 1e6 ? (n / 1e6).toFixed(1).replace(".", ",") + " Mo"
  : Math.max(1, Math.round(n / 1e3)) + " Ko";

// Durée en secondes : millisecondes sous la seconde.
export const ms = (s) => (s >= 1 ? s.toFixed(1) + " s" : Math.round(s * 1000) + " ms");

// Une durée longue, arrondie à l'unité utile (« 3 h 05 », « 12 j »).
export function dur(s) {
  s = Math.max(0, Math.round(s));
  if (s < 60) return s + " s";
  if (s < 3600) return Math.floor(s / 60) + " min";
  if (s < 86400) return Math.floor(s / 3600) + " h " + String(Math.floor((s % 3600) / 60)).padStart(2, "0");
  return Math.floor(s / 86400) + " j";
}

// « il y a 3 min » : `ts` et `now` en secondes.
export function ago(ts, now) {
  const s = Math.max(0, Math.round(now - ts));
  if (s < 5) return "à l'instant";
  if (s < 60) return "il y a " + s + " s";
  if (s < 3600) return "il y a " + Math.floor(s / 60) + " min";
  if (s < 86400) return "il y a " + Math.floor(s / 3600) + " h";
  return "il y a " + Math.floor(s / 86400) + " j";
}

// Date et heure courtes (« 17/10/2026 13:01 ») d'un instant en secondes ;
// « — » sans instant.
export const stamp = (ts) => ts ? new Date(ts * 1000).toLocaleString("fr-FR",
  { dateStyle: "short", timeStyle: "short" }) : "—";

// Jour et heure (« 17 oct., 13:01 ») d'un instant en secondes.
export const moment = (ts) => new Date(ts * 1000).toLocaleString("fr-FR",
  { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

// L'heure si c'est aujourd'hui, sinon le jour (« 13:01 », « 12 oct. ») —
// d'un instant en MILLISECONDES (les conversations du chat).
export function when(t) {
  const d = new Date(t), today = new Date();
  return d.toDateString() === today.toDateString()
    ? d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString("fr-FR", { day: "numeric", month: "short" });
}
