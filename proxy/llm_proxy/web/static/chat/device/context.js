// Date et heure : la date et l'heure locales, le fuseau, les langues.
import { fn } from "./common.js";

export const FAMILIES = [{
  key: "context", label: "Date et heure", hint: "date, heure, fuseau, langues", on: true,
  tools: [fn("device_context", "Donne la date et l'heure locales de l'utilisateur, son "
    + "fuseau horaire et ses langues. À utiliser avant de parler d'« aujourd'hui », de "
    + "« demain » ou d'une heure.")],
}];

export const waits = () => false;

export function run() {
  const now = new Date();
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const local = now.toLocaleString("fr-FR", { dateStyle: "full", timeStyle: "short" });
  const offset = -now.getTimezoneOffset();
  const hh = String(Math.floor(Math.abs(offset) / 60)).padStart(2, "0");
  const mm = String(Math.abs(offset) % 60).padStart(2, "0");
  return [`Date et heure locales : ${local} (UTC${offset >= 0 ? "+" : "-"}${hh}:${mm})`,
          `Fuseau horaire : ${zone}`,
          `Langues : ${(navigator.languages || [navigator.language]).join(", ")}`].join("\n");
}
