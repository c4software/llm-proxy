// @ts-check
// Date et heure : la date et l'heure locales, le fuseau, les langues — et
// ce que le navigateur dit de l'appareil (VueUse) : en ligne ou non, la
// connexion, la batterie, le thème et le mouvement préférés, la fenêtre.
// Ce qu'il ne sait pas n'est pas dit.
import { TOOL, FAMILY, PageTool, defineFamily } from "../contract.js";
import { network, battery, prefersDark, reducedMotion, windowSize } from "./vueuse.js";

function device() {
  const out = [];
  const n = network;
  let line = `En ligne : ${n.isOnline.value ? "oui" : "non"}`;
  if (n.isSupported.value) {
    const parts = [n.effectiveType.value && `réseau ${n.effectiveType.value}`,
                   n.downlink.value && `≈ ${n.downlink.value} Mb/s`, n.saveData.value && "économie de données"];
    if (parts.some(Boolean)) line += ` (${parts.filter(Boolean).join(", ")})`;
  }
  out.push(line);
  if (battery.isSupported.value && battery.level.value !== undefined) {
    out.push(`Batterie : ${Math.round(battery.level.value * 100)} %${battery.charging.value ? ", en charge" : ""}`);
  }
  out.push(`Thème préféré : ${prefersDark.value ? "sombre" : "clair"}`
    + (reducedMotion.value === "reduce" ? " ; animations réduites" : ""));
  const w = windowSize.width.value, h = windowSize.height.value;
  if (w && h) out.push(`Fenêtre : ${w} × ${h} px${w <= 760 ? " (écran étroit, sans doute un téléphone)" : ""}`);
  return out;
}

class DeviceContext extends PageTool {
  constructor() {
    super({
      name: TOOL.DEVICE_CONTEXT,
      description: "Donne la date et l'heure locales de l'utilisateur, son fuseau horaire et ses "
        + "langues, et ce que son navigateur dit de l'appareil (en ligne, connexion, batterie, "
        + "thème, taille d'écran). À utiliser avant de parler d'« aujourd'hui », de « demain » "
        + "ou d'une heure, ou pour adapter une réponse à l'appareil.",
    });
  }

  /** @override */
  run() {
    const now = new Date();
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const local = now.toLocaleString("fr-FR", { dateStyle: "full", timeStyle: "short" });
    const offset = -now.getTimezoneOffset();
    const hh = String(Math.floor(Math.abs(offset) / 60)).padStart(2, "0");
    const mm = String(Math.abs(offset) % 60).padStart(2, "0");
    return [`Date et heure locales : ${local} (UTC${offset >= 0 ? "+" : "-"}${hh}:${mm})`,
            `Fuseau horaire : ${zone}`,
            `Langues : ${(navigator.languages || [navigator.language]).join(", ")}`,
            ...device()].join("\n");
  }
}

export const FAMILIES = [defineFamily({
  key: FAMILY.CONTEXT, label: "Date et heure", hint: "date, heure, fuseau, langues, appareil", on: true,
  tools: [new DeviceContext()],
})];
