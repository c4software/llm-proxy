/*
  Le CONTRAT d'un outil du navigateur : ce qu'un outil doit offrir, ce
  qu'il reçoit, ce qu'il rend. Le registre (index.js) et le chat ne
  connaissent d'un outil que ce qui est écrit ici — jamais un outil par
  son nom en dur, toujours par TOOL.

  Ce module n'importe rien : un outil l'importe sans cycle.

  CE QUE LE MODÈLE LIT d'un outil : `description`, les `description` de
  ses paramètres, et le texte de son résultat — une erreur commence par
  «Error: » (failure), le modèle s'adapte.

  Deux façons de s'exécuter :
    aussitôt            run(args, ctx) → le texte du résultat (ou sa
                        promesse). `ctx` : {live, conv, ws} du tour.
    en attendant        waits(args) vrai : prepare(args) → l'état de la
    l'utilisateur       carte (`kind: "device"`, ou {error}), view(w) →
                        ce qu'elle montre ({title, detail, actions,
                        preview, video, quote, code, note}), act(w,
                        action) → {result, image?, name?} quand elle est
                        répondue, sinon null (elle change, et attend
                        encore). release(w) : la carte est abandonnée.
  Ce qui touche à l'appareil (position, caméra, écran, presse-papiers…)
  n'est jamais lu sans un clic sur la carte.
*/

// Les noms des outils, liste FERMÉE : celui de la fonction présentée au
// modèle. Un nom ne change pas (l'historique des conversations le garde).
export const TOOL = Object.freeze({
  DEVICE_CONTEXT: "device_context",
  GET_LOCATION: "get_location",
  NOTIFY: "notify",
  CLIPBOARD_WRITE: "clipboard_write",
  CLIPBOARD_READ: "clipboard_read",
  ADD_TO_CALENDAR: "add_to_calendar",
  OPEN_LINK: "open_link",
  SHARE: "share",
  CAPTURE_SCREEN: "capture_screen",
  TAKE_PHOTO: "take_photo",
  SCAN_CODE: "scan_code",
  PICK_COLOR: "pick_color",
  KEEP_SCREEN_ON: "keep_screen_on",
  RUN_JAVASCRIPT: "run_javascript",
  RUN_PHP: "run_php",
  RUN_SQL: "run_sql",
});

// Les clés des familles, liste FERMÉE : ce qu'une conversation active
// (gardé avec elle — une clé ne change pas non plus).
export const FAMILY = Object.freeze({
  CONTEXT: "context",
  LOCATION: "location",
  NOTIFY: "notify",
  CLIPBOARD: "clipboard",
  CALENDAR: "calendar",
  LINKS: "links",
  SHARE: "share",
  SCREEN: "screen",
  CAMERA: "camera",
  CODES: "codes",
  COLOR: "color",
  WAKE: "wake",
  JAVASCRIPT: "compute",
  PHP: "php",
  SQL: "sql",
});

const NAMES = new Set(Object.values(TOOL));
const KEYS = new Set(Object.values(FAMILY));

const unsupported = (what) => () => { throw new Error(`${what} : non pris en charge par cet outil`); };

/*
  Un outil. À écrire : `name` (de TOOL), `description`, et `run` — ou,
  pour un outil qui attend l'utilisateur, `waits`, `prepare`, `view` et
  `act`. Le reste a un défaut.

    params      les paramètres, {nom: schéma JSON}
    required    ceux qui sont obligatoires
    waits       true, ou (args) → booléen : attend-il l'utilisateur ?
    summary     (args) → ce que la puce de l'appel en montre, une ligne
*/
export function defineTool(def) {
  if (!NAMES.has(def.name)) throw new Error(`outil hors de TOOL : « ${def.name} »`);
  const waits = typeof def.waits === "function" ? def.waits : () => !!def.waits;
  const required = def.required || [];
  return Object.freeze({
    run: unsupported(`${def.name}.run`),
    prepare: () => ({}),
    view: unsupported(`${def.name}.view`),
    act: unsupported(`${def.name}.act`),
    release: () => {},
    summary: () => "",
    ...def,
    waits,
    // La fonction présentée au modèle, à la forme chat/completions.
    spec: { type: "function", function: { name: def.name, description: def.description,
      parameters: { type: "object", properties: def.params || {},
                    ...(required.length ? { required } : {}) } } },
  });
}

/*
  Une famille : ce que le menu Outils coche ou non, par conversation.
    key         de FAMILY
    label, hint ce qu'affiche le menu
    on          cochée d'office dans une conversation neuve
    available   () → booléen : ce navigateur sait-il la faire ? (défaut : oui)
    tools       ses outils (defineTool)
*/
export function defineFamily(def) {
  if (!KEYS.has(def.key)) throw new Error(`famille hors de FAMILY : « ${def.key} »`);
  return Object.freeze({ on: false, available: () => true, ...def });
}

// ── ce que partagent les outils ─────────────────────────────────────────

// Un échec : le texte que le modèle lit.
export const failure = (message) => `Error: ${message}`;
// Une carte refusée, ou un bouton pour la refuser.
export const refused = (what) => ({ result: `L'utilisateur a refusé ${what}.` });
export const REFUSE = { id: "refuse", label: "Refuser" };
// Un texte coupé à `n` caractères, en le disant.
export const clip = (s, n) => (s.length > n ? s.slice(0, n) + `\n… (${s.length - n} caractères coupés)` : s);
// Un code, sur une ligne (la puce d'un appel).
export const oneLine = (s) => String(s || "").replace(/\s+/g, " ").trim().slice(0, 120);
// Le paramètre `reason` d'une carte, et sa lecture.
export const REASON = { reason: { type: "string", description: "Pourquoi, en une phrase, montrée à l'utilisateur." } };
export const reasonOf = (args) => ({ reason: String(args.reason || "").trim().slice(0, 300) });
