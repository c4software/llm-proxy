// Agenda : un événement en fichier .ics sous la réponse, qui s'ouvre dans
// l'agenda de l'utilisateur (Google, Outlook, Apple…) — un rendez-vous
// qui survit à la page, contrairement à un rappel.
import { TOOL, FAMILY, defineTool, defineFamily, failure } from "./contract.js";
import { saveFile } from "../outputs.js";

const DAY = /^\d{4}-\d{2}-\d{2}$/;
const pad = (n) => String(n).padStart(2, "0");
const utc = (d) => `${d.getUTCFullYear()}${pad(d.getUTCMonth() + 1)}${pad(d.getUTCDate())}T`
  + `${pad(d.getUTCHours())}${pad(d.getUTCMinutes())}${pad(d.getUTCSeconds())}Z`;
const day = (s) => s.replaceAll("-", "");
// Le texte d'une propriété : échappé, replié à 75 octets environ.
const text = (s) => String(s).replace(/\\/g, "\\\\").replace(/;/g, "\\;").replace(/,/g, "\\,")
  .replace(/\r?\n/g, "\\n");
const fold = (line) => line.match(/.{1,72}/g).join("\r\n ");

function run(args, ctx) {
  const title = String(args.title || "").trim();
  const start = String(args.start || "").trim();
  if (!title || !start) return failure("`title` et `start` attendus.");
  const allDay = DAY.test(start);
  const lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//llm-proxy//chat//FR", "BEGIN:VEVENT",
    `UID:${crypto.randomUUID()}@llm-proxy`, `DTSTAMP:${utc(new Date())}`, `SUMMARY:${text(title)}`];
  if (allDay) {
    const next = new Date(start + "T12:00");
    const endDay = args.end && DAY.test(args.end) ? new Date(args.end + "T12:00") : next;
    endDay.setDate(endDay.getDate() + 1);
    lines.push(`DTSTART;VALUE=DATE:${day(start)}`,
               `DTEND;VALUE=DATE:${endDay.getFullYear()}${pad(endDay.getMonth() + 1)}${pad(endDay.getDate())}`);
  } else {
    const from = new Date(start);
    if (Number.isNaN(from.getTime())) return failure(`début illisible « ${start} ».`);
    const to = args.end ? new Date(args.end) : new Date(from.getTime() + 3600_000);
    if (Number.isNaN(to.getTime()) || to <= from) return failure(`fin illisible ou avant le début « ${args.end} ».`);
    lines.push(`DTSTART:${utc(from)}`, `DTEND:${utc(to)}`);
  }
  if (args.location) lines.push(`LOCATION:${text(args.location)}`);
  if (args.description) lines.push(`DESCRIPTION:${text(args.description)}`);
  const alarm = Math.round(Number(args.alarm_minutes) || 0);
  if (alarm > 0) {
    lines.push("BEGIN:VALARM", "ACTION:DISPLAY", `DESCRIPTION:${text(title)}`,
               `TRIGGER:-PT${alarm}M`, "END:VALARM");
  }
  lines.push("END:VEVENT", "END:VCALENDAR");
  const file = (title.replace(/[^\p{L}\p{N}]+/gu, "-").replace(/^-|-$/g, "").slice(0, 60) || "evenement") + ".ics";
  saveFile(ctx.live, { name: file, content: lines.map(fold).join("\r\n") + "\r\n", mime: "text/calendar" });
  return `Événement « ${title} » proposé sous la réponse (${file}) : l'utilisateur l'ouvre pour l'ajouter à son agenda.`;
}

const addToCalendar = defineTool({
  name: TOOL.ADD_TO_CALENDAR,
  description: "Propose à l'utilisateur un événement à ajouter à son agenda (un fichier .ics "
    + "sous la réponse, qu'il ouvre dans son agenda). Les heures sont locales à l'utilisateur "
    + "(appeler device_context si la date du jour est utile).",
  params: {
    title: { type: "string", description: "Le titre de l'événement." },
    start: { type: "string", description: "Début, heure locale : « 2026-10-18T09:30 », ou « 2026-10-18 » pour une journée entière." },
    end: { type: "string", description: "Fin, même format (facultatif : 1 h après le début, ou la journée)." },
    location: { type: "string", description: "Le lieu (facultatif)." },
    description: { type: "string", description: "Les détails (facultatif)." },
    alarm_minutes: { type: "number", description: "Une alerte N minutes avant (facultatif)." },
  },
  required: ["title", "start"],
  summary: (a) => [a.title, a.start].filter(Boolean).join(" · "),
  run,
});

export const FAMILIES = [defineFamily({
  key: FAMILY.CALENDAR, label: "Agenda", hint: "un événement à ajouter (.ics)", on: true,
  tools: [addToCalendar],
})];
