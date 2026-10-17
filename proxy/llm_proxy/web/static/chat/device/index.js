/*
  Les outils du NAVIGATEUR : ce que le modèle peut demander à l'appareil.
  Rangés par FAMILLE, que chaque conversation active ou non (section
  « Navigateur » du menu Outils) ; un module par famille :

    context.js        Date et heure       device_context
    location.js       Position            get_location            carte
    notifications.js  Notifications       notify                  carte s'il faut les permettre
    clipboard.js      Presse-papiers      clipboard_write,
                                          clipboard_read          carte (lecture)
    calendar.js       Agenda              add_to_calendar         un .ics sous la réponse
    links.js          Liens, Partage      open_link, share        carte
    capture.js        Capture d'écran,    capture_screen,
                      Caméra, Codes       take_photo, scan_code   carte (aperçu avant envoi)
    color.js          Pipette             pick_color              carte
    wake.js           Écran allumé        keep_screen_on
    compute.js        Calcul              run_javascript          bac isolé, sans réseau

  Un module exporte FAMILIES ({key, label, hint, on (cochée d'office),
  available() ?, tools}), run(name, args, ctx) pour ce qui s'exécute
  aussitôt, et pour ce qui ATTEND l'utilisateur : waits(name),
  prepare(name, args) → l'état de la carte (`kind: "device"`), view(w)
  → ce qu'elle montre ({title, detail, actions, preview, video, note}),
  act(w, action) → {result, image?} quand elle est répondue, sinon
  null (elle change, et attend encore). Ce qui touche à l'appareil
  (position, caméra, écran, presse-papiers…) n'est jamais lu sans un clic
  sur la carte. Une image rendue (capture, photo) part au modèle jointe
  au résultat de l'outil.
*/
import * as context from "./context.js";
import * as location from "./location.js";
import * as notifications from "./notifications.js";
import * as clipboard from "./clipboard.js";
import * as calendar from "./calendar.js";
import * as links from "./links.js";
import * as capture from "./capture.js";
import * as color from "./color.js";
import * as wake from "./wake.js";
import * as compute from "./compute.js";

const MODULES = [context, location, notifications, clipboard, calendar, links, capture,
                 color, wake, compute];
export const FAMILIES = MODULES.flatMap((m) => m.FAMILIES);

const byTool = new Map();
for (const m of MODULES) {
  for (const f of m.FAMILIES) for (const t of f.tools) byTool.set(t.function.name, m);
}

// Celles que ce navigateur sait faire.
export const usableFamilies = () => FAMILIES.filter((f) => !f.available || f.available());
// Cochées d'office dans une conversation neuve.
export const defaultFamilies = () => FAMILIES.filter((f) => f.on).map((f) => f.key);

// Les déclarations des familles `keys` (actives et possibles ici).
export function toolsFor(keys) {
  const on = new Set(keys || []);
  return usableFamilies().filter((f) => on.has(f.key)).flatMap((f) => f.tools);
}

export const isTool = (name) => byTool.has(name);
export const waits = (name) => !!byTool.get(name)?.waits(name);
export async function run(name, args, ctx) {
  const m = byTool.get(name);
  return m ? m.run(name, args, ctx) : `Error: outil inconnu « ${name} ».`;
}
export function prepare(name, args) {
  const card = byTool.get(name).prepare(name, args);
  return card.error ? card : { kind: "device", tool: name, args, ...card };
}
export const view = (w) => byTool.get(w.tool).view(w);
export const act = (w, action) => byTool.get(w.tool).act(w, action);
// Une carte qui montre la caméra : son flux, sur l'élément <video>.
export const attachVideo = capture.attachVideo;
// Une carte abandonnée : ses flux (caméra, écran) sont coupés.
export const release = (w) => { if (w && w.kind === "device") capture.stop(w); };
