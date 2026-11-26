// @ts-check
/*
  Le REGISTRE des outils de la page. Le chat ne connaît les outils que
  par lui, et lui ne les connaît que par leur contrat (contract.js).

  Un module par famille, inscrit UNE fois dans MODULES :

    page/ask.js           Questions           ask_user                feuille du bas
    page/save.js          Fichiers            save_file               un fichier sous la réponse
    page/artifacts.js     Artifacts           show_artifact,          le panneau latéral
                                              edit_artifact, read_artifact,
                                              grep_artifact
                          Déboguer            inspect_artifact,       la page affichée (actif
                                              run_in_artifact         par défaut) ; coupé :
                                              request_artifact_debug  une carte pour le rétablir
    page/folder.js        Dossier ouvert      folder_list, _read,     s'il est ouvert ; écrire
                                              _search, _write, _edit  attend l'accord (le diff)
    device/context.js     Date et heure       device_context
    device/location.js    Position            get_location            carte
    device/notifications.js Notifications     notify                  carte s'il faut les permettre
    device/clipboard.js   Presse-papiers      clipboard_write, _read  carte (lecture)
    device/calendar.js    Agenda              add_to_calendar         un .ics sous la réponse
    device/links.js       Liens               open_link               carte
    device/share.js       Partage             share                   carte
    device/screen.js      Capture d'écran     capture_screen          carte (aperçu)
    device/camera.js      Caméra              take_photo              carte (aperçu)
    device/codes.js       QR et codes-barres  scan_code               carte
    device/color.js       Pipette             pick_color              carte
    device/wake.js        Écran allumé        keep_screen_on          carte durable (basculer)
    device/speak.js       Lecture à voix      speak                   carte durable (relire)
                          haute
    sandbox/javascript.js JavaScript,         run_javascript,         bac isolé, sans réseau
                          TypeScript          check_typescript          (sandbox/bac.js)
    sandbox/php.js        PHP                 run_php
    sandbox/sql.js        SQL                 run_sql
    sandbox/postgres.js   PostgreSQL          run_postgres
    sandbox/linux.js      Linux               run_shell, shell_read,  la VM de linuxvm.js, partagée
                                              _grep, _edit, _write    avec le panneau Terminal
    sandbox/programs.js   Programmes          run_tools_program       le bac ; ses tools.x() passent
                          d'outils                                    par la page (/ui/tools)

  Ces modules donnent l'EXÉCUTION. La DÉCLARATION de chaque outil et de
  chaque famille (prompt, schéma, label, guidance…) vient du proxy, au
  contrat de tous les outils (tools/contract.py ; tools/page/ pour ceux-ci) :
  /ui/session la donne (`chat.page`), install() l'attache à l'exécution du
  même nom. Un outil d'une EXTENSION arrive avec l'adresse de son module
  (`module`), que installAll() importe : il rend son exécution, au même
  contrat (`export default (ext) => ({tools, families?})`).

  Au chargement, le registre VÉRIFIE l'exécution native : chaque outil
  hérite de PageTool, n'a que ses membres, a un mode cohérent ; chaque nom
  de TOOL et chaque clé de FAMILY est implémenté une fois et une seule. À
  l'installation, une déclaration sans exécution (ou l'inverse, pour une
  extension) est dite à la console et laissée hors du menu.
  `check-tools.mjs` (proxy/wasm, que pytest lance avec les déclarations du
  proxy) vérifie les deux côtés ensemble.
*/
import * as Vue from "vue";
import { TOOL, FAMILY, PageTool, MEMBERS, defineFamily, failure, refused, REFUSE, clip, oneLine,
         reasonOf } from "./contract.js";
import * as ask from "./page/ask.js";
import * as save from "./page/save.js";
import * as artifacts from "./page/artifacts.js";
import * as folder from "./page/folder.js";
import * as context from "./device/context.js";
import * as location from "./device/location.js";
import * as notifications from "./device/notifications.js";
import * as clipboard from "./device/clipboard.js";
import * as calendar from "./device/calendar.js";
import * as links from "./device/links.js";
import * as share from "./device/share.js";
import * as screen from "./device/screen.js";
import * as camera from "./device/camera.js";
import * as codes from "./device/codes.js";
import * as color from "./device/color.js";
import * as wake from "./device/wake.js";
import * as speak from "./device/speak.js";
import * as javascript from "./sandbox/javascript.js";
import * as php from "./sandbox/php.js";
import * as sql from "./sandbox/sql.js";
import * as postgres from "./sandbox/postgres.js";
import * as linux from "./sandbox/linux.js";
import * as programs from "./sandbox/programs.js";

export { TOOL, FAMILY, failure } from "./contract.js";

/** @typedef {import("./contract.js").Family} Family */
/** @typedef {import("./contract.js").FamilyBehavior} FamilyBehavior */
/** @typedef {import("./contract.js").Ctx} Ctx */
/** @typedef {import("./contract.js").Card} Card */

// L'ordre du menu Outils.
const MODULES = [ask, save, artifacts, folder, context, location, notifications, clipboard,
                 calendar, links, share, screen, camera, codes, color, wake, speak, javascript, php, sql,
                 postgres, linux, programs];

/** L'exécution native de chaque famille, dans l'ordre des modules.
 * @type {FamilyBehavior[]} */
export const BEHAVIORS = MODULES.flatMap((m) => m.FAMILIES);

/** L'exécution native de chaque outil. @type {Map<string, PageTool>} */
const NATIVE = new Map();

/** Les familles INSTALLÉES (déclaration + exécution), dans l'ordre du
 * proxy : ce que le menu montre et ce qui part au modèle.
 * @type {Family[]} */
export let FAMILIES = [];

/** Les outils installés : ceux que le modèle peut appeler. @type {Map<string, PageTool>} */
const TOOLS = new Map();

/** Toutes les exécutions connues, installées ou non (refusées, d'une
 * extension désactivée ou pas encore importée) : ce qui RENDRA une carte
 * déjà dans une conversation, et nettoiera ce qu'un outil garde.
 * @type {Map<string, PageTool>} */
const KNOWN = new Map();

/** Une carte dont l'outil n'est plus là : montrée, sans bouton. */
const GONE = { title: "Outil indisponible sur ce proxy", actions: [] };

// ── la vérification ─────────────────────────────────────────────────────
/** L'outil réécrit-il ce membre de PageTool ?
 * @param {any} tool @param {string} member */
function overrides(tool, member) {
  return tool[member] !== PageTool.prototype[member];
}

/** Ce qu'un outil a de trop ou de travers : la liste des fautes.
 * @param {PageTool} tool @returns {string[]} */
export function faults(tool) {
  if (!(tool instanceof PageTool)) return ["n'hérite pas de PageTool"];
  const out = [];
  const where = `outil « ${tool.name} »`;
  // Les membres : ceux de l'interface, rien d'autre (une faute de frappe).
  const own = new Set(Object.keys(tool));
  for (let p = Object.getPrototypeOf(tool); p && p !== PageTool.prototype; p = Object.getPrototypeOf(p)) {
    for (const k of Object.getOwnPropertyNames(p)) own.add(k);
  }
  for (const k of own) if (!MEMBERS.has(k)) out.push(`${where} : membre inconnu « ${k} » (pas dans PageTool)`);
  // Le mode : aussitôt (run), ou une carte (waits, view ou component, act).
  const card = overrides(tool, "waits") || tool.component;
  if (card) {
    if (!overrides(tool, "act")) out.push(`${where} : une carte sans act`);
    if (!overrides(tool, "view") && !tool.component) out.push(`${where} : une carte sans view ni component`);
  }
  // Une carte qui ne s'affiche que si la permission manque (notify) a aussi un run.
  if (!card && !overrides(tool, "run")) out.push(`${where} : ni run, ni carte`);
  if (!tool.keptComponent && overrides(tool, "keptView") !== overrides(tool, "keptAct")) {
    out.push(`${where} : keptView et keptAct vont ensemble`);
  }
  if (!["card", "sheet"].includes(tool.placement)) out.push(`${where} : placement inconnu`);
  if (tool.placement === "sheet" && !tool.component) out.push(`${where} : une feuille sans component`);
  return out;
}

/** Les fautes d'un ensemble de familles exécutées : outils hors contrat,
 * clés ou noms en double. `native` : clés et noms dans FAMILY et TOOL.
 * @param {FamilyBehavior[]} families @param {Map<string, PageTool>} into @param {boolean} native */
function check(families, into, native) {
  const nativeKeys = /** @type {string[]} */ (Object.values(FAMILY));
  const nativeNames = /** @type {string[]} */ (Object.values(TOOL));
  const errors = [];
  const keys = new Set();
  for (const f of families) {
    if (native && !nativeKeys.includes(f.key)) errors.push(`famille « ${f.key} » hors de FAMILY`);
    if (!native && nativeKeys.includes(f.key)) errors.push(`famille « ${f.key} » : clé native`);
    if (keys.has(f.key)) errors.push(`famille « ${f.key} » en double`);
    keys.add(f.key);
    if (!f.tools.length) errors.push(`famille « ${f.key} » sans outil`);
    for (const t of f.tools) {
      errors.push(...faults(t));
      if (native && !nativeNames.includes(t.name)) errors.push(`outil « ${t.name} » : nom hors de TOOL`);
      if (!native && nativeNames.includes(t.name)) errors.push(`outil « ${t.name} » : nom natif`);
      if (into.has(t.name)) errors.push(`outil « ${t.name} » en double`);
      into.set(t.name, t);
    }
  }
  return errors;
}

{
  const errors = check(BEHAVIORS, NATIVE, true);
  const keys = new Set(BEHAVIORS.map((f) => f.key));
  for (const k of Object.values(FAMILY)) if (!keys.has(k)) errors.push(`FAMILY « ${k} » sans famille`);
  for (const n of Object.values(TOOL)) if (!NATIVE.has(n)) errors.push(`TOOL « ${n} » sans outil`);
  if (errors.length) throw new Error("Outils de la page :\n  " + errors.join("\n  "));
  for (const [name, t] of NATIVE) KNOWN.set(name, t);
}

// ── l'installation : les déclarations du proxy sur l'exécution ──────────

/** Ce que reçoit le module d'une extension : de quoi écrire ses outils
 * sans chemin d'import (les fichiers de la page ont une empreinte dans
 * leur nom) ; `Vue`, le module, pour la réactivité et le composant d'une
 * carte (son `template` texte est compilé dans la page). */
export const EXT = Object.freeze({ PageTool, defineFamily, failure, refused, REFUSE, clip, oneLine,
                                   reasonOf, Vue });

/**
 * Les déclarations de /ui/session (`chat.page`).
 * @typedef {{families: {key: string, label: string, hint?: string, on?: boolean, fixed?: boolean,
 *   authored?: boolean, guidance?: string, tools: string[]}[],
 *   tools: {name: string, spec: object, settings?: object, module?: string}[]}} Declarations
 */

/** Attache les déclarations à l'exécution : native, ou `extra` (celle des
 * modules d'extension, installAll). Une déclaration sans exécution est
 * laissée hors du menu. → les écarts, aussi dits à la console.
 * @param {Declarations | null | undefined} page @param {FamilyBehavior[]} [extra]
 * @returns {string[]} */
export function install(page, extra = []) {
  const behaviors = new Map(NATIVE);
  const families = new Map(BEHAVIORS.map((f) => [f.key, f]));
  for (const f of extra) {
    families.set(f.key, f);
    for (const t of f.tools) { behaviors.set(t.name, t); KNOWN.set(t.name, t); }
  }
  const declared = new Map(((page && page.tools) || []).map((t) => [t.name, t]));
  const errors = [], installed = [];
  TOOLS.clear();
  // Les outils d'une extension attendent son module (installAll) : sans
  // lui, ils restent hors du menu, sans écart à dire ici.
  const awaited = new Set([...declared.values()].filter((t) => t.module).map((t) => t.name));
  for (const f of (page && page.families) || []) {
    const b = families.get(f.key);
    if (!b && f.tools.every((n) => awaited.has(n))) continue;
    if (!b) { errors.push(`famille « ${f.key} » : déclarée, sans exécution`); continue; }
    const members = [];
    for (const name of f.tools) {
      const t = behaviors.get(name), d = declared.get(name);
      if (!t || !d) { errors.push(`outil « ${name} » : déclaré, sans exécution`); continue; }
      t.declaration = d.spec;
      t.settings = d.settings || {};
      TOOLS.set(name, t);
      members.push(t);
    }
    if (!members.length) continue;
    installed.push(Object.freeze({ ...b, tools: members, label: f.label, hint: f.hint || "",
      on: !!f.on, fixed: !!f.fixed, authored: !!f.authored, guidance: () => f.guidance || "" }));
  }
  FAMILIES = installed;
  if (errors.length) console.error("Outils de la page :\n  " + errors.join("\n  "));
  return errors;
}

/** install(), après avoir importé le module de chaque extension que les
 * déclarations nomment. Un module qui échoue, ou qui rend une exécution
 * hors contrat, est dit à la console : ses outils restent hors du menu.
 * Une installation que dépasse une plus récente (une nouvelle session)
 * n'installe rien. `load` : l'import (les tests le remplacent).
 * @param {Declarations | null | undefined} page
 * @param {(url: string) => Promise<any>} [load] @returns {Promise<string[]>} */
let generation = 0;
export async function installAll(page, load = (url) => import(/* @vite-ignore */ url)) {
  // Une installation plus récente gagne : celle-ci, si elle finit après,
  // n'installe rien (ses modules restent connus, KNOWN).
  const mine = ++generation;
  const errors = [], extra = [];
  const urls = new Set(((page && page.tools) || []).map((t) => t.module).filter(Boolean));
  for (const url of urls) {
    try {
      const mod = await load(url);
      const got = mod.default(EXT) || {};
      const tools = Array.isArray(got.tools) ? got.tools : [];
      const wanted = new Set(page.tools.filter((t) => t.module === url).map((t) => t.name));
      // Une famille sans exécution propre : celle de ses outils, sans plus.
      const given = (Array.isArray(got.families) ? got.families : []).map((f) => defineFamily(f));
      const covered = new Set(given.flatMap((f) => f.tools.map((t) => t.name)));
      const families = [...given, ...page.families
        .filter((f) => !given.some((g) => g.key === f.key) && f.tools.every((n) => wanted.has(n)))
        .map((f) => defineFamily({ key: f.key, tools: tools.filter((t) => f.tools.includes(t.name)
                                                                     && !covered.has(t.name)) }))];
      const found = new Map();
      const bad = check(families, found, false);
      const keys = new Set(page.families.filter((f) => f.tools.every((n) => wanted.has(n))).map((f) => f.key));
      for (const f of given) if (!keys.has(f.key)) bad.push(`famille « ${f.key} » : non déclarée par l'extension`);
      for (const n of wanted) if (!found.has(n)) bad.push(`outil « ${n} » : sans exécution dans ${url}`);
      for (const n of found.keys()) if (!wanted.has(n)) bad.push(`outil « ${n} » : non déclaré par l'extension`);
      if (bad.length) errors.push(...bad);
      else extra.push(...families);
    } catch (e) {
      errors.push(`module ${url} : ${e && e.message}`);
    }
  }
  if (errors.length) console.error("Extensions de la page :\n  " + errors.join("\n  "));
  if (mine !== generation) return [...errors, "installation dépassée par une plus récente"];
  return [...errors, ...install(page, extra)];
}

// ── les familles ────────────────────────────────────────────────────────

// Celles du menu que ce navigateur sait faire.
export const menuFamilies = () => FAMILIES.filter((f) => !f.fixed && f.available());
// Cochées d'office dans une conversation neuve.
export const defaultFamilies = () => FAMILIES.filter((f) => !f.fixed && f.on).map((f) => f.key);

/** Les familles actives pour ce tour : toujours actives ou cochées,
 * possibles ici, et `enabled` pour ce contexte.
 * @param {Ctx} ctx @param {string[]} keys */
function active(ctx, keys) {
  const on = new Set(keys || []);
  return FAMILIES.filter((f) => (f.fixed || on.has(f.key)) && f.available() && f.enabled(ctx));
}

/** Les déclarations des outils actifs (la forme chat/completions).
 * @param {Ctx} ctx @param {string[]} keys */
export const specs = (ctx, keys) => active(ctx, keys).flatMap((f) => f.tools.map((t) => t.spec));

/** Les consignes des familles actives, pour le prompt système (figées).
 * @param {Ctx} ctx @param {string[]} keys */
export const guidance = (ctx, keys) => active(ctx, keys).map((f) => f.guidance(ctx)).filter(Boolean).join("\n\n");

/** L'état du moment des familles actives (les artifacts, le dossier
 * ouvert…), joint au message de l'utilisateur du tour.
 * @param {Ctx} ctx @param {string[]} keys */
export const state = (ctx, keys) => active(ctx, keys).map((f) => f.state(ctx)).filter(Boolean).join("\n");

// ── les appels ──────────────────────────────────────────────────────────

/** @param {string} name */
export const isTool = (name) => TOOLS.has(name);
/** @param {Card} w */
const of = (w) => KNOWN.get(w.name || w.tool);

/** Les arguments d'un appel (du JSON), ou null s'ils sont illisibles.
 * @param {string} json */
export function parseArgs(json) {
  try {
    const v = json ? JSON.parse(json) : {};
    return v && typeof v === "object" && !Array.isArray(v) ? v : null;
  } catch { return null; }
}

/** @param {string} name @param {any} args @param {Ctx} [ctx] */
export const waits = (name, args, ctx) => !!TOOLS.get(name)?.waits(args || {}, ctx);

/** Une carte durable (`keep` d'un résultat) : gardée sous la réponse, dans
 * le message (m.kept).
 * @param {string} name @param {any} got @param {Ctx} ctx */
function keep(name, got, ctx) {
  if (!got || !got.keep || !ctx.live) return;
  const list = ctx.live.kept || (ctx.live.kept = []);
  for (const k of Array.isArray(got.keep) ? got.keep : [got.keep]) {
    list.push({ id: "kept_" + crypto.randomUUID(), name, ...k });
  }
}

// La promesse `p`, ou une AbortError dès que `signal` est levé — l'outil
// qui ne l'écoute pas est abandonné, pas attendu.
/** @template T @param {Promise<T>} p @param {AbortSignal} signal @returns {Promise<T>} */
function abortable(p, signal) {
  const stop = () => new DOMException("arrêté par l'utilisateur", "AbortError");
  if (signal.aborted) return Promise.reject(stop());
  return new Promise((resolve, reject) => {
    const onAbort = () => reject(stop());
    signal.addEventListener("abort", onAbort, { once: true });
    p.then(resolve, reject).finally(() => signal.removeEventListener("abort", onAbort));
  });
}

/** Un outil qui s'exécute aussitôt → {result, image?, artifact?}. Une
 * exception est une erreur pour le modèle — sauf l'arrêt de la réponse
 * (ctx.signal), une AbortError qui remonte au tour.
 * @param {string} name @param {string} json @param {Ctx} ctx */
export async function run(name, json, ctx) {
  const t = TOOLS.get(name);
  if (!t) return { result: failure(`outil inconnu ou désactivé sur ce proxy « ${name} ».`) };
  const args = parseArgs(json);
  if (args === null) return { result: failure("arguments illisibles (JSON attendu).") };
  const signal = ctx && ctx.signal;
  try {
    const got = await (signal ? abortable(Promise.resolve(t.run(args, ctx)), signal) : t.run(args, ctx));
    if (typeof got === "string") return { result: got };
    keep(name, got, ctx);
    return got;
  } catch (e) {
    if (signal && signal.aborted && e && e.name === "AbortError") throw e;
    return { result: failure(e.message) };
  }
}

/** Un outil qui attend l'utilisateur → l'état de sa carte (sans id), ou {error}.
 * @param {string} name @param {string} json @param {Ctx} ctx */
export async function prepare(name, json, ctx) {
  const args = parseArgs(json);
  if (args === null) return { error: failure("arguments illisibles (JSON attendu).") };
  const card = await TOOLS.get(name).prepare(args, ctx);
  return card.error ? card : { name, args, ...card };
}

/** @param {Card} w */
export const view = (w) => (of(w) ? of(w).view(w) : GONE);
/** @param {Card} w */
export const component = (w) => of(w)?.component || null;
/** @param {Card} w */
export const placement = (w) => of(w)?.placement || "card";
/** @param {Card} w */
export const notice = (w) => (of(w) ? of(w).notice(w) : GONE.title);
/** @param {Card} w */
export const pending = (w) => (of(w) ? of(w).pending(w) : GONE.title);
/** Une action de la carte → sa réponse, ou null ; une carte durable qu'elle
 * laisse rejoint le message.
 * @param {Card} w @param {string} action @param {Ctx} ctx @param {any} [payload] */
export async function act(w, action, ctx, payload) {
  if (!TOOLS.has(w.name)) throw new Error("outil indisponible sur ce proxy");
  const out = await of(w).act(w, action, ctx, payload);
  keep(w.name, out, ctx);
  return out;
}
/** Une carte répondue : ce qu'elle tient est relâché. @param {Card} w */
export const release = (w) => { if (w && of(w)) of(w).release(w); };
/** Une carte abandonnée : relâchée, et le résultat pour le modèle. @param {Card} w */
export function abandon(w) {
  release(w);
  return of(w) ? of(w).abandoned(w) : "Non exécuté.";
}
/** Une carte durable (Kept). @param {any} k */
export const keptView = (k) => (KNOWN.get(k.name) ? KNOWN.get(k.name).keptView(k) : GONE);
/** Le composant d'une carte durable, ou null (le gabarit commun). @param {any} k */
export const keptComponent = (k) => KNOWN.get(k.name)?.keptComponent || null;
/** @param {any} k @param {string} action @param {Ctx} ctx */
export const keptAct = (k, action, ctx) => (TOOLS.has(k.name) ? TOOLS.get(k.name).keptAct(k, action, ctx)
  : "Outil indisponible sur ce proxy.");
/** La ligne d'une puce d'appel. @param {string} name @param {any} args */
export const summary = (name, args) => KNOWN.get(name)?.summary(args || {}) || "";

/** Une conversation supprimée : chaque outil oublie ce qu'il gardait pour elle.
 * @param {string} convId */
export function forget(convId) {
  for (const t of KNOWN.values()) Promise.resolve(t.forget(convId)).catch(() => {});
}

/** Le contenu d'un fichier produit sous la réponse : son texte, ou ce
 * que l'outil qui l'a produit en relit (SourcedFile). L'ancienne forme
 * `{base}` est celle de run_sql.
 * @param {any} f @returns {Promise<string | Uint8Array | null>} */
export async function fileContent(f) {
  const source = f.source || (f.base ? { tool: TOOL.RUN_SQL, ref: f.base } : null);
  if (!source) return f.content;
  const t = KNOWN.get(source.tool);
  return t ? t.fileContent(source.ref) : null;
}

/** Ce qu'une réponse a fait avec les outils, pour l'historique renvoyé au
 * modèle : chaque étape, un message assistant à `tool_calls` puis un
 * message `tool` par appel ; une image rendue par un outil suit, dans un
 * message de l'utilisateur (tous les backends le lisent). Rejoué tel
 * qu'il a été vu, réponse en cours ou passée : l'historique est en ajout
 * seul (contract.js) ; un contenu périmé depuis, la version actuelle le
 * dit (`state`, read_artifact, shell_read).
 * @param {any[]} steps */
export function stepMessages(steps) {
  const out = [];
  for (const step of steps || []) {
    const calls = step.calls;
    out.push({ role: "assistant", content: step.content || null,
      tool_calls: calls.map((c) => ({ id: c.id, type: "function",
        function: { name: c.name, arguments: c.arguments || "{}" } })) });
    for (const c of calls) out.push({ role: "tool", tool_call_id: c.id, content: c.result });
    const images = step.calls.filter((c) => c.image);
    if (images.length) {
      out.push({ role: "user", content: [
        { type: "text", text: "Image(s) rendue(s) par " + images.map((c) => c.name).join(", ") + " :" },
        ...images.map((c) => ({ type: "image_url", image_url: { url: c.image } }))] });
    }
  }
  return out;
}
