// @ts-check
/*
  Le REGISTRE des outils de la page. Le chat ne connaît les outils que
  par lui, et lui ne les connaît que par leur contrat (contract.js).

  Un module par famille, inscrit UNE fois dans MODULES :

    page/ask.js           Questions           ask_user                feuille du bas
    page/save.js          Fichiers            save_file               un fichier sous la réponse
    page/artifacts.js     Artifacts           show_artifact,          le panneau latéral
                                              edit_artifact, read_artifact
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

  Au chargement, le registre VÉRIFIE tout : chaque outil hérite de
  PageTool, n'a que ses membres, a un mode cohérent ; chaque nom de TOOL
  et chaque clé de FAMILY est implémenté une fois et une seule. Une
  dérive casse la page tout de suite — et `npm run check` (proxy/wasm)
  avant elle.
*/
import { TOOL, FAMILY, PageTool, MEMBERS, failure } from "./contract.js";
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

export { TOOL, FAMILY } from "./contract.js";

/** @typedef {import("./contract.js").Family} Family */
/** @typedef {import("./contract.js").Ctx} Ctx */
/** @typedef {import("./contract.js").Card} Card */

// L'ordre du menu Outils.
const MODULES = [ask, save, artifacts, folder, context, location, notifications, clipboard,
                 calendar, links, share, screen, camera, codes, color, wake, speak, javascript, php, sql];

/** @type {Family[]} */
export const FAMILIES = MODULES.flatMap((m) => m.FAMILIES);

/** @type {Map<string, PageTool>} */
const TOOLS = new Map();

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
  if (!Object.values(TOOL).includes(tool.name)) out.push(`${where} : nom hors de TOOL`);
  if (!tool.description || typeof tool.description !== "string") out.push(`${where} : description vide`);
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
  if (overrides(tool, "keptView") !== overrides(tool, "keptAct")) out.push(`${where} : keptView et keptAct vont ensemble`);
  if (!["card", "sheet"].includes(tool.placement)) out.push(`${where} : placement inconnu`);
  if (tool.agent && (card || !overrides(tool, "run"))) out.push(`${where} : un outil d'agent s'exécute aussitôt (run, sans carte)`);
  if (tool.placement === "sheet" && !tool.component) out.push(`${where} : une feuille sans component`);
  for (const r of tool.required) if (!(r in tool.params)) out.push(`${where} : « ${r} » requis mais pas dans params`);
  return out;
}

{
  const errors = [];
  const keys = new Set();
  for (const f of FAMILIES) {
    if (!Object.values(FAMILY).includes(f.key)) errors.push(`famille « ${f.key} » hors de FAMILY`);
    if (keys.has(f.key)) errors.push(`famille « ${f.key} » en double`);
    keys.add(f.key);
    if (!f.tools.length) errors.push(`famille « ${f.key} » sans outil`);
    for (const t of f.tools) {
      errors.push(...faults(t));
      if (TOOLS.has(t.name)) errors.push(`outil « ${t.name} » en double`);
      TOOLS.set(t.name, t);
    }
  }
  for (const k of Object.values(FAMILY)) if (!keys.has(k)) errors.push(`FAMILY « ${k} » sans famille`);
  for (const n of Object.values(TOOL)) if (!TOOLS.has(n)) errors.push(`TOOL « ${n} » sans outil`);
  if (errors.length) throw new Error("Outils de la page :\n  " + errors.join("\n  "));
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

/** Les consignes des familles actives, pour le prompt système.
 * @param {Ctx} ctx @param {string[]} keys */
export const guidance = (ctx, keys) => active(ctx, keys).map((f) => f.guidance(ctx)).filter(Boolean).join("\n\n");

// ── les appels ──────────────────────────────────────────────────────────

/** @param {string} name */
export const isTool = (name) => TOOLS.has(name);
/** @param {Card} w */
const of = (w) => TOOLS.get(w.name || w.tool);

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
  if (got && got.keep && ctx.live) {
    (ctx.live.kept || (ctx.live.kept = [])).push({ id: "kept_" + crypto.randomUUID(), name, ...got.keep });
  }
}

/** Un outil qui s'exécute aussitôt → {result, image?, artifact?}. Une
 * exception est une erreur pour le modèle.
 * @param {string} name @param {string} json @param {Ctx} ctx */
export async function run(name, json, ctx) {
  const t = TOOLS.get(name);
  if (!t) return { result: failure(`outil inconnu « ${name} ».`) };
  const args = parseArgs(json);
  if (args === null) return { result: failure("arguments illisibles (JSON attendu).") };
  try {
    const got = await t.run(args, ctx);
    if (typeof got === "string") return { result: got };
    keep(name, got, ctx);
    return got;
  } catch (e) {
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
export const view = (w) => of(w).view(w);
/** @param {Card} w */
export const component = (w) => of(w).component;
/** @param {Card} w */
export const placement = (w) => of(w).placement;
/** @param {Card} w */
export const notice = (w) => of(w).notice(w);
/** @param {Card} w */
export const pending = (w) => of(w).pending(w);
/** Une action de la carte → sa réponse, ou null ; une carte durable qu'elle
 * laisse rejoint le message.
 * @param {Card} w @param {string} action @param {Ctx} ctx @param {any} [payload] */
export async function act(w, action, ctx, payload) {
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
// ── les agents du navigateur (WebMCP) ───────────────────────────────────
// Un outil `agent` appelé par un agent du navigateur, hors de toute
// conversation : ni dossier, ni message, ni appareil ; une base SQL à lui
// (incognito : en mémoire). Seul le texte du résultat lui revient.
const AGENT_CTX = Object.freeze({ conv: Object.freeze({ id: "webmcp", incognito: true, messages: [] }),
                                  live: null, ws: null, vision: false, download: () => {} });

/** Les outils exposés aux agents, de familles possibles ici. */
export const agentTools = () => FAMILIES.filter((f) => f.available()).flatMap((f) => f.tools)
  .filter((t) => t.agent);

/** Un appel d'agent → le texte du résultat (une erreur commence par « Error: »).
 * @param {string} name @param {any} args */
export async function runForAgent(name, args) {
  const got = await run(name, JSON.stringify(args || {}), { ...AGENT_CTX });
  return got.result;
}

/** Une carte durable (Kept). @param {any} k */
export const keptView = (k) => TOOLS.get(k.name).keptView(k);
/** @param {any} k @param {string} action @param {Ctx} ctx */
export const keptAct = (k, action, ctx) => TOOLS.get(k.name).keptAct(k, action, ctx);
/** La ligne d'une puce d'appel. @param {string} name @param {any} args */
export const summary = (name, args) => TOOLS.get(name)?.summary(args || {}) || "";

/** Une conversation supprimée : chaque outil oublie ce qu'il gardait pour elle.
 * @param {string} convId */
export function forget(convId) {
  for (const t of TOOLS.values()) Promise.resolve(t.forget(convId)).catch(() => {});
}

/** Le contenu d'un fichier produit sous la réponse : son texte, ou ce
 * que l'outil qui l'a produit en relit (SourcedFile). L'ancienne forme
 * `{base}` est celle de run_sql.
 * @param {any} f @returns {Promise<string | Uint8Array | null>} */
export async function fileContent(f) {
  const source = f.source || (f.base ? { tool: TOOL.RUN_SQL, ref: f.base } : null);
  if (!source) return f.content;
  const t = TOOLS.get(source.tool);
  return t ? t.fileContent(source.ref) : null;
}

/** Ce qu'une réponse a fait avec les outils, pour l'historique renvoyé au
 * modèle : chaque étape, un message assistant à `tool_calls` puis un
 * message `tool` par appel ; une image rendue par un outil suit, dans un
 * message de l'utilisateur (tous les backends le lisent).
 * @param {any[]} steps */
export function stepMessages(steps) {
  const out = [];
  for (const step of steps || []) {
    out.push({ role: "assistant", content: step.content || null,
      tool_calls: step.calls.map((c) => ({ id: c.id, type: "function",
        function: { name: c.name, arguments: c.arguments || "{}" } })) });
    for (const c of step.calls) out.push({ role: "tool", tool_call_id: c.id, content: c.result });
    const images = step.calls.filter((c) => c.image);
    if (images.length) {
      out.push({ role: "user", content: [
        { type: "text", text: "Image(s) rendue(s) par " + images.map((c) => c.name).join(", ") + " :" },
        ...images.map((c) => ({ type: "image_url", image_url: { url: c.image } }))] });
    }
  }
  return out;
}
