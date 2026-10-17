// Date et heure : la date et l'heure locales, le fuseau, les langues.
import { TOOL, FAMILY, defineTool, defineFamily } from "./contract.js";

const deviceContext = defineTool({
  name: TOOL.DEVICE_CONTEXT,
  description: "Donne la date et l'heure locales de l'utilisateur, son fuseau horaire et ses "
    + "langues. À utiliser avant de parler d'« aujourd'hui », de « demain » ou d'une heure.",
  run() {
    const now = new Date();
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const local = now.toLocaleString("fr-FR", { dateStyle: "full", timeStyle: "short" });
    const offset = -now.getTimezoneOffset();
    const hh = String(Math.floor(Math.abs(offset) / 60)).padStart(2, "0");
    const mm = String(Math.abs(offset) % 60).padStart(2, "0");
    return [`Date et heure locales : ${local} (UTC${offset >= 0 ? "+" : "-"}${hh}:${mm})`,
            `Fuseau horaire : ${zone}`,
            `Langues : ${(navigator.languages || [navigator.language]).join(", ")}`].join("\n");
  },
});

export const FAMILIES = [defineFamily({
  key: FAMILY.CONTEXT, label: "Date et heure", hint: "date, heure, fuseau, langues", on: true,
  tools: [deviceContext],
})];
