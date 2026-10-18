// @ts-check
/*
  Le CONTRAT des outils de la page : ceux que le proxy rend au navigateur
  en `tool_calls` (comme à tout client) et que la page exécute — question
  à choix, fichiers, artifacts, dossier ouvert, appareil, bacs de code.
  Le chat (ChatPanel) et le registre (index.js) ne connaissent d'un outil
  que ce qui est écrit ici : jamais un outil par son nom en dur.

  Ce module n'importe rien : un outil l'importe sans cycle.

  Trois garde-fous contre les dérives :
    les listes FERMÉES  TOOL et FAMILY : un nom, une clé ne s'inventent
                        pas, et chacun doit être implémenté (index.js le
                        vérifie dans les deux sens) ;
    l'INTERFACE         PageTool : un outil est une classe qui en hérite ;
                        le registre refuse un membre inconnu (une faute de
                        frappe) et un mode incohérent ;
    les TYPES           JSDoc, vérifiés par TypeScript (proxy/wasm : npm
                        run check) ; une méthode réécrite porte
                        `@override`, qui doit nommer une méthode de
                        PageTool.

  CE QUE LE MODÈLE LIT d'un outil : `description`, les `description` de
  ses paramètres, le `guidance` de sa famille quand elle est active (au
  prompt système), et le texte de ses résultats — une erreur commence
  par « Error: » (failure), le modèle s'adapte.
*/

// Les noms des outils : celui de la fonction présentée au modèle. Un nom
// ne change pas (l'historique des conversations le garde).
export const TOOL = Object.freeze(/** @type {const} */ ({
  // Questions, fichiers, artifacts, dossier (page/)
  ASK_USER: "ask_user",
  SAVE_FILE: "save_file",
  SHOW_ARTIFACT: "show_artifact",
  EDIT_ARTIFACT: "edit_artifact",
  READ_ARTIFACT: "read_artifact",
  FOLDER_LIST: "folder_list",
  FOLDER_READ: "folder_read",
  FOLDER_SEARCH: "folder_search",
  FOLDER_WRITE: "folder_write",
  FOLDER_EDIT: "folder_edit",
  // L'appareil (device/)
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
  // Les bacs de code (sandbox/)
  RUN_JAVASCRIPT: "run_javascript",
  CHECK_TYPESCRIPT: "check_typescript",
  RUN_PHP: "run_php",
  RUN_SQL: "run_sql",
}));

// Les clés des familles. Celles du menu Outils sont gardées avec chaque
// conversation : une clé ne change pas non plus.
export const FAMILY = Object.freeze(/** @type {const} */ ({
  // Toujours actives (pas dans le menu)
  ASK: "ask",
  SAVE: "save",
  ARTIFACTS: "artifacts",
  FOLDER: "folder",
  // Le menu Outils, section Navigateur
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
}));

// ── les types ───────────────────────────────────────────────────────────

/** @typedef {typeof TOOL[keyof typeof TOOL]} ToolName */
/** @typedef {typeof FAMILY[keyof typeof FAMILY]} FamilyKey */

/**
 * Ce que le tour donne à un outil.
 * @typedef {object} Ctx
 * @property {any} [conv]       la conversation
 * @property {any} [live]       le message de la réponse (ses fichiers, ses images)
 * @property {any} [ws]         le dossier ouvert, ou null
 * @property {boolean} [vision] le modèle de la conversation lit-il les images ?
 * @property {(file: {name: string, content: any}, type?: string) => void} [download]
 *                              faire télécharger un fichier à l'utilisateur
 */

/** Le résultat d'un `run` : le texte pour le modèle, ou ce texte et ses
 * effets — une image jointe (data URL), un artifact à ouvrir, une carte
 * DURABLE à laisser sous la réponse (`keep` : son état, voir Kept).
 * @typedef {string | {result: string, image?: string, name?: string, artifact?: string,
 *   keep?: object}} RunResult */

/** Une carte répondue : son résultat, et peut-être une image.
 * @typedef {{result: string, image?: string, name?: string}} Answer */

/** @typedef {{id: string, label: string, primary?: boolean, title?: string}} Action */

/** Ce qu'une carte montre, dans le gabarit commun.
 * @typedef {object} CardView
 * @property {string} title
 * @property {string} [detail]
 * @property {Action[]} actions
 * @property {string} [preview]       une image en aperçu (data URL)
 * @property {MediaStream} [video]    un flux montré en direct (la caméra)
 * @property {string} [quote]         un texte cité
 * @property {string} [code]          une adresse, en entier
 * @property {string} [note]
 */

/** L'état d'une carte, gardé dans le message (m.waiting) : il survit à un
 * rechargement. `id` : celui de l'appel ; `name` : l'outil ; `args` : ses
 * arguments ; le reste vient de `prepare`.
 * @typedef {{id: string, name: ToolName, args: any, result?: string, busy?: boolean,
 *   [k: string]: any}} Card */

/** Une carte DURABLE, laissée sous la réponse par un `run` (`keep`) et
 * gardée dans le message (m.kept) : elle survit à un rechargement, et ses
 * boutons se rejouent à volonté (partager à nouveau). `id` : le sien ;
 * `name` : l'outil ; le reste vient de `keep`.
 * @typedef {{id: string, name: ToolName, [k: string]: any}} Kept */

/** Un fichier produit sous la réponse, dont le contenu est relu au clic.
 * @typedef {{name: string, type: string, size: number, source: {tool: ToolName, ref: string}}} SourcedFile */

// ── l'interface ─────────────────────────────────────────────────────────

const NO_ANSWER = "L'utilisateur n'a pas répondu : il a écrit un nouveau message à la place.";

/** Ce qu'un outil DÉCLARE, passé à son constructeur (super(spec)).
 * @typedef {object} ToolSpec
 * @property {ToolName} name           le nom de la fonction présentée au modèle (de TOOL)
 * @property {string} description      ce que le modèle lit pour savoir quand et comment l'appeler
 * @property {Record<string, object>} [params]  ses paramètres : {nom: schéma JSON}, leurs `description` comprises
 * @property {string[]} [required]     ceux qui sont obligatoires
 * @property {"card" | "sheet"} [placement]  où sa carte s'affiche : dans le fil, ou dans la feuille du bas
 * @property {object} [component]      un composant Vue de carte, à la place du gabarit commun (view) ;
 *                                     il reçoit `w` (la carte), `busy` et `ctx`, il émet
 *                                     `act(action, payload)`, et `minimize` dans la feuille
 */

/**
 * L'INTERFACE d'un outil de la page. Un outil est une classe qui en
 * hérite :
 *
 *   constructor()              super({name, description, params, required…}) :
 *                              ce qu'il déclare (ToolSpec)
 *   run                        s'il s'exécute aussitôt
 *   waits, view (ou component),
 *   act, et prepare            s'il attend l'utilisateur (une carte)
 *
 * Le reste a un défaut. Une méthode réécrite porte `@override` ; une
 * aide propre à l'outil est une fonction du module (le registre refuse
 * tout membre que PageTool ne déclare pas).
 */
export class PageTool {
  /** @param {ToolSpec} spec */
  constructor(spec) {
    /** @type {ToolName} */
    this.name = spec.name;
    /** @type {string} */
    this.description = spec.description;
    /** @type {Record<string, object>} */
    this.params = spec.params || {};
    /** @type {string[]} */
    this.required = spec.required || [];
    /** @type {"card" | "sheet"} */
    this.placement = spec.placement || "card";
    /** @type {object | null} */
    this.component = spec.component || null;
  }

  /** Attend-il l'utilisateur ? (une carte)
   * @param {any} args @param {Ctx} [ctx] @returns {boolean} */
  waits(args, ctx) { return false; }

  /** L'exécution, aussitôt. `args` : les arguments du modèle, rien de validé.
   * @param {any} args @param {Ctx} ctx @returns {RunResult | Promise<RunResult>} */
  run(args, ctx) { throw new Error(`${this.name} : pas d'exécution directe`); }

  /** Les arguments → l'état de la carte, ou {error} (« Error: … ») : rien
   * à demander à l'utilisateur.
   * @param {any} args @param {Ctx} [ctx] @returns {object | Promise<object>} */
  prepare(args, ctx) { return {}; }

  /** Ce que la carte montre (le gabarit commun).
   * @param {Card} w @returns {CardView} */
  view(w) { throw new Error(`${this.name} : pas de carte`); }

  /** Une action de la carte (un bouton) → {result, image?} quand elle est
   * répondue, null si elle change et attend encore. Une Error levée est
   * montrée à l'utilisateur, la carte reste.
   * @param {Card} w @param {string} action @param {Ctx} ctx @param {any} [payload]
   * @returns {Answer | null | Promise<Answer | null>} */
  act(w, action, ctx, payload) { throw new Error(`${this.name} : pas de carte`); }

  /** La carte est fermée (répondue ou abandonnée) : ce qu'elle tient
   * (caméra, écran) est relâché.
   * @param {Card} w @returns {void} */
  release(w) {}

  /** Le résultat d'une carte abandonnée (un nouveau message à la place).
   * @param {Card} w @returns {string} */
  abandoned(w) { return NO_ANSWER; }

  /** Le texte de la notification quand la carte paraît.
   * @param {Card} w @returns {string} */
  notice(w) { return this.component ? "Une carte attend votre réponse" : this.view(w).title; }

  /** Dans le fil, pour une carte de la feuille du bas : la ligne qui la rouvre.
   * @param {Card} w @returns {string} */
  pending(w) { return "Une carte attend votre réponse"; }

  /** Ce que montre une carte durable (Kept), dans le gabarit commun.
   * @param {Kept} k @returns {CardView} */
  keptView(k) { throw new Error(`${this.name} : pas de carte durable`); }

  /** Un bouton d'une carte durable → une note montrée sur la carte (« Partagé. »),
   * ou null. Rien ne part au modèle : la réponse est finie.
   * @param {Kept} k @param {string} action @param {Ctx} ctx
   * @returns {string | null | Promise<string | null>} */
  keptAct(k, action, ctx) { throw new Error(`${this.name} : pas de carte durable`); }

  /** La ligne que la puce de l'appel montre.
   * @param {any} args @returns {string} */
  summary(args) { return ""; }

  /** Le contenu, relu au clic, d'un fichier produit par l'outil (SourcedFile).
   * @param {string} ref @returns {Promise<string | Uint8Array | null>} */
  async fileContent(ref) { return null; }

  /** La conversation est supprimée : ce que l'outil garde pour elle
   * (une base, des fichiers) est effacé.
   * @param {string} convId @returns {void | Promise<void>} */
  forget(convId) {}

  /** La fonction présentée au modèle, à la forme chat/completions.
   * Assemblée : un outil ne la réécrit pas. */
  get spec() {
    return { type: "function", function: { name: this.name, description: this.description,
      parameters: { type: "object", properties: this.params,
                    ...(this.required.length ? { required: this.required } : {}) } } };
  }
}

// Les membres de l'interface : un outil n'en a pas d'autres.
export const MEMBERS = Object.freeze(new Set([
  ...Object.getOwnPropertyNames(PageTool.prototype),
  "name", "description", "params", "required", "placement", "component",
]));

/*
  Une FAMILLE : ce que le menu Outils coche ou non, par conversation —
  ou, `fixed`, toujours active et hors du menu.
*/
/**
 * @typedef {object} FamilyDef
 * @property {FamilyKey} key
 * @property {string} label            ce qu'affiche le menu
 * @property {string} hint
 * @property {PageTool[]} tools
 * @property {boolean} [on]            cochée d'office dans une conversation neuve
 * @property {boolean} [fixed]         toujours active, hors du menu
 * @property {() => boolean} [available]   ce navigateur sait-il la faire ?
 * @property {(ctx: Ctx) => boolean} [enabled] active pour CE tour ? (le dossier : s'il est ouvert)
 * @property {(ctx: Ctx) => string} [guidance] une consigne ajoutée au prompt système quand elle est active
 */
/** @typedef {Readonly<Required<FamilyDef>>} Family */

const FAMILY_KEYS = ["key", "label", "hint", "tools", "on", "fixed", "available", "enabled", "guidance"];

/** @param {FamilyDef} def @returns {Family} */
export function defineFamily(def) {
  for (const k of Object.keys(def)) {
    if (!FAMILY_KEYS.includes(k)) throw new Error(`famille « ${def.key} » : membre inconnu « ${k} »`);
  }
  return Object.freeze({ on: false, fixed: false, available: () => true, enabled: () => true,
                         guidance: () => "", ...def });
}

// ── ce que partagent les outils ─────────────────────────────────────────

/** Un échec : le texte que le modèle lit. @param {string} message */
export const failure = (message) => `Error: ${message}`;
/** Une carte refusée. @param {string} what @returns {Answer} */
export const refused = (what) => ({ result: `L'utilisateur a refusé ${what}.` });
/** @type {Action} */
export const REFUSE = { id: "refuse", label: "Refuser" };
/** Un texte coupé à `n` caractères, en le disant. @param {string} s @param {number} n */
export const clip = (s, n) => (s.length > n ? s.slice(0, n) + `\n… (${s.length - n} caractères coupés)` : s);
/** Un code, sur une ligne (la puce d'un appel). @param {any} s */
export const oneLine = (s) => String(s || "").replace(/\s+/g, " ").trim().slice(0, 120);
/** Le paramètre `reason` d'une carte, et sa lecture. */
export const REASON = { reason: { type: "string", description: "Pourquoi, en une phrase, montrée à l'utilisateur." } };
/** @param {any} args */
export const reasonOf = (args) => ({ reason: String(args.reason || "").trim().slice(0, 300) });
