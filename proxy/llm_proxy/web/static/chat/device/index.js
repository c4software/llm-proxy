// @ts-check
/*
  Les outils du NAVIGATEUR : ce que le modèle peut demander à l'appareil.
  Le registre : il ne connaît d'un outil que son contrat (contract.js).
  Rangés par FAMILLE, que chaque conversation active ou non (section
  « Navigateur » du menu Outils) ; un module par famille :

    context.js            Date et heure       device_context
    location.js           Position            get_location            carte
    notifications.js      Notifications       notify                  carte s'il faut les permettre
    clipboard.js          Presse-papiers      clipboard_write,
                                              clipboard_read          carte (lecture)
    calendar.js           Agenda              add_to_calendar         un .ics sous la réponse
    links.js, share.js    Liens, Partage      open_link, share        carte
    screen.js, camera.js, Capture d'écran,    capture_screen,         carte (aperçu avant envoi)
      codes.js            Caméra, Codes       take_photo, scan_code     — leurs flux : media.js
    color.js              Pipette             pick_color              carte
    wake.js               Écran allumé        keep_screen_on
    sandbox/              JavaScript,         run_javascript,         bac isolé, sans réseau
                          TypeScript, PHP,    check_typescript,         (bac.js) ; PHP, SQLite
                          SQL                 run_php, run_sql          et TypeScript chargés
                                                                        à la demande

  Une image rendue par une carte (capture, photo) part au modèle jointe
  au résultat de l'outil.
*/
import * as context from "./context.js";
import * as location from "./location.js";
import * as notifications from "./notifications.js";
import * as clipboard from "./clipboard.js";
import * as calendar from "./calendar.js";
import * as links from "./links.js";
import * as share from "./share.js";
import * as screen from "./screen.js";
import * as camera from "./camera.js";
import * as codes from "./codes.js";
import * as color from "./color.js";
import * as wake from "./wake.js";
import * as javascript from "./sandbox/javascript.js";
import * as php from "./sandbox/php.js";
import * as sql from "./sandbox/sql.js";
import { attachVideo as attach } from "./media.js";
import { failure } from "./contract.js";

export { TOOL, FAMILY } from "./contract.js";
// La base SQL d'une conversation (run_sql), pour la télécharger.
export { load as sqlBase } from "./sandbox/sqlbase.js";

// L'ordre du menu Outils.
export const FAMILIES = [context, location, notifications, clipboard, calendar, links, share,
                         screen, camera, codes, color, wake, javascript, php, sql]
  .flatMap((m) => m.FAMILIES);

// nom → outil ; un nom ou une clé en double est une erreur du registre.
const TOOLS = new Map();
{
  const keys = new Set();
  for (const f of FAMILIES) {
    if (keys.has(f.key)) throw new Error(`famille en double : « ${f.key} »`);
    keys.add(f.key);
    for (const t of f.tools) {
      if (TOOLS.has(t.name)) throw new Error(`outil en double : « ${t.name} »`);
      TOOLS.set(t.name, t);
    }
  }
}

// Celles que ce navigateur sait faire.
export const usableFamilies = () => FAMILIES.filter((f) => f.available());
// Cochées d'office dans une conversation neuve.
export const defaultFamilies = () => FAMILIES.filter((f) => f.on).map((f) => f.key);

// Les déclarations des familles `keys` (actives et possibles ici).
export function toolsFor(keys) {
  const on = new Set(keys || []);
  return usableFamilies().filter((f) => on.has(f.key)).flatMap((f) => f.tools.map((t) => t.spec));
}

export const isTool = (name) => TOOLS.has(name);
export const waits = (name, args = {}) => !!TOOLS.get(name)?.waits(args);
export async function run(name, args, ctx) {
  const t = TOOLS.get(name);
  return t ? t.run(args, ctx) : failure(`outil inconnu « ${name} ».`);
}
export function prepare(name, args) {
  const card = TOOLS.get(name).prepare(args);
  return card.error ? card : { kind: "device", tool: name, args, ...card };
}
export const summary = (name, args) => TOOLS.get(name).summary(args);
export const view = (w) => TOOLS.get(w.tool).view(w);
export const act = (w, action) => TOOLS.get(w.tool).act(w, action);
// Une carte qui montre la caméra : son flux, sur l'élément <video>.
export const attachVideo = attach;
// Une carte abandonnée : ce qu'elle tient (caméra, écran) est relâché.
export const release = (w) => { if (w && w.kind === "device") TOOLS.get(w.tool)?.release(w); };
