// @ts-check
/*
  Programmes d'outils : le modèle écrit un court programme JavaScript qui
  appelle les OUTILS HÉBERGÉS cochés dans la conversation (`await
  tools.web_search({...})`), combine et filtre leurs résultats, et ne
  rend que l'essentiel — en un seul appel, au lieu d'un tour par outil.

  Le programme tourne dans le bac (bac.js : Worker sans réseau ni accès à
  la page). Un `tools.x(args)` y est une demande à la PAGE (`call` du
  bac) : `bridge` la refuse, ou l'envoie au proxy (POST /ui/tools/<nom>,
  la session de la page : droits du compte, borne par minute du proxy),
  sous un budget et une concurrence par programme. Le programme ne joint
  que ces outils-là, et seulement par la page.
*/
import { TOOL, FAMILY, PageTool, defineFamily, clip, failure, oneLine } from "../contract.js";
import { exec } from "./bac.js";

const MAX_OUT = 20_000;
// Caractères des arguments d'un appel (du JSON).
const MAX_ARGS = 1_000_000;
// Refus au-delà desquels le programme est arrêté : une boucle qui ne fait
// plus que des appels refusés occuperait la page jusqu'au délai.
export const MAX_REFUSED = 100;

// Exécutée dans le Worker du bac : elle ne voit que ses arguments
// (exportée pour les tests, qui la font tourner sous Node).
export async function driver({ code, names }, { call, fmt }) {
  const tools = {};
  for (const name of names) {
    tools[name] = async (args) => {
      const r = await call(name, JSON.stringify(args === undefined ? {} : args));
      if (r.error) {
        const e = new Error(String(r.text).replace(/^Error: /, ""));
        Object.assign(e, { code: r.error, tool: name });
        throw e;
      }
      return r.text;
    };
  }
  const run = new Function("tools", "return (async () => {\n" + code + "\n})()");
  const value = await run(Object.freeze(tools));
  return value === undefined ? undefined : fmt(value);
}

/** Le pont d'un programme vers les outils : ce que la page répond à
 * chaque `call` du bac. `allowed` : les outils joignables ; `send(nom,
 * args)` : l'appel réel, rendu `{text, error}`. Hors liste, arguments
 * trop longs, budget épuisé ou pont fermé : un refus, sans appel ; au-delà
 * de MAX_REFUSED refus, `onFlood()`. `close()` : le programme est fini —
 * ce qui attend encore son tour ne part plus. `calls` : chaque appel
 * parti, avec son issue.
 * @param {string[]} allowed
 * @param {(name: string, args: string) => Promise<{text: string, error: string | null}>} send
 * @param {{max: number, concurrency: number, onFlood?: () => void}} o  max, concurrency :
 *   appels par programme, et à la fois (les `settings` déclarés de l'outil) */
export function bridge(allowed, send, { max, concurrency, onFlood = () => {} }) {
  /** @type {string[]} */
  const calls = [];
  /** @type {(() => void)[]} */
  const queue = [];
  let admitted = 0;
  let active = 0;
  let refused = 0;
  let closed = false;
  const next = () => {
    while (!closed && active < concurrency && queue.length) {
      active++;
      /** @type {() => void} */ (queue.shift())();
    }
  };
  /** @param {string} text @param {string} error */
  const refuse = (text, error) => {
    if (++refused === MAX_REFUSED + 1) onFlood();
    return { text: failure(text), error };
  };
  return {
    calls,
    refused: () => refused,
    close() { closed = true; queue.length = 0; },
    /** @param {string} name @param {any} args */
    call(name, args) {
      if (closed) return refuse("le programme est terminé.", "unavailable");
      if (!allowed.includes(name)) {
        return refuse(`l'outil « ${name} » n'est pas coché dans cette conversation.`, "invalid_input");
      }
      if (typeof args !== "string" || args.length > MAX_ARGS) {
        return refuse(`arguments de plus de ${MAX_ARGS} caractères.`, "invalid_input");
      }
      if (admitted >= max) return refuse(`la limite de ${max} appels par programme est atteinte.`, "limit");
      admitted++;
      return new Promise((resolve) => {
        queue.push(() => {
          send(name, args)
            .catch((/** @type {any} */ e) => ({ text: failure(e && e.message || String(e)), error: "unavailable" }))
            .then((r) => {
              calls.push(`${name} ${r.error || "ok"}`);
              resolve(r);
            })
            .finally(() => { active--; next(); });
        });
        next();
      });
    },
  };
}

/** L'appel réel d'un outil hébergé, par la route de la page, avec les
 * sites connus (`known`, provenance des URL) : ceux que le proxy a donnés
 * en fin de réponse, et ceux que les appels du programme y ajoutent.
 * @param {string} name @param {string} args @param {Set<string>} known
 * @param {any} ctx @param {AbortSignal} signal */
export async function send(name, args, known, ctx, signal) {
  // Les arguments, relus en objet : le corps est refait ici, pas collé —
  // le programme ne glisse rien d'autre dans la requête.
  let parsed;
  try { parsed = JSON.parse(args); } catch { parsed = null; }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    return { text: failure("les arguments doivent être un objet JSON."), error: "invalid_input" };
  }
  const headers = { "content-type": "application/json",
                    ...(ctx.conv && ctx.conv.incognito ? { "x-incognito": "1" } : {}) };
  const r = await fetch("/ui/tools/" + encodeURIComponent(name), { method: "POST", headers, signal,
    body: JSON.stringify({ arguments: parsed, hosts: [...known] }) });
  const d = await r.json().catch(() => ({}));
  if (Array.isArray(d.hosts)) for (const h of d.hosts) known.add(String(h));
  if (r.ok) return { text: String(d.result ?? ""), error: d.error ?? null };
  const message = (d.error && d.error.message) || `HTTP ${r.status}`;
  return { text: failure(message), error: statusCode(r.status) };
}

/** Le code d'erreur d'un refus de la route (docs/outils.md, codes
 * d'erreur) : 429 est la borne par minute du proxy, une limite d'appels.
 * @param {number} status */
export const statusCode = (status) => ({ 400: "invalid_input", 403: "not_allowed", 404: "invalid_input",
                                         429: "limit" })[status] || "unavailable";

class RunToolsProgram extends PageTool {
  constructor() {
    super({ name: TOOL.RUN_TOOLS_PROGRAM });
  }

  /** @override @param {any} a */
  summary(a) { return oneLine(a.code); }

  /** @override @param {any} args @param {import("../contract.js").Ctx} ctx */
  async run(args, ctx) {
    const code = String(args.code || "");
    if (!code.trim()) return failure("`code` attendu.");
    const names = ctx.hosted || [];
    if (!names.length) return failure("aucun outil hébergé n'est coché dans cette conversation (menu Outils).");
    // Arrêté par l'utilisateur, une inondation de refus, ou la fin du
    // programme : le bac est supprimé, les appels en cours sont coupés.
    const stop = new AbortController();
    const abort = () => stop.abort();
    if (ctx.signal) ctx.signal.addEventListener("abort", abort, { once: true });
    let flooded = false;
    const known = new Set((ctx.live && ctx.live.known) || []);
    const { max_calls: max, concurrency, timeout } = this.settings;
    const pont = bridge(names, (name, a) => send(name, a, known, ctx, stop.signal),
                        { max, concurrency, onFlood: () => { flooded = true; abort(); } });
    let r;
    try {
      r = await exec({ driver, input: { code, names }, timeout: timeout * 1000, signal: stop.signal,
                       onCall: (name, a) => pont.call(name, a) });
    } finally {
      pont.close();
      abort();
      if (ctx.signal) ctx.signal.removeEventListener("abort", abort);
    }
    const out = [];
    if (r.ok) out.push(r.value === undefined ? "(aucune valeur rendue)" : "Résultat : " + r.value);
    else if (flooded) out.push(failure(`plus de ${MAX_REFUSED} appels refusés : programme arrêté.`));
    else out.push(failure(r.error));
    if (r.logs.length) out.push("Console :\n" + r.logs.join("\n"));
    if (pont.calls.length || pont.refused()) {
      out.push(`Appels d'outils (${pont.calls.length}) : ${pont.calls.join(", ") || "aucun"}`
               + (pont.refused() ? ` ; ${pont.refused()} refusé(s)` : ""));
    }
    return clip(out.join("\n"), MAX_OUT);
  }
}

export const FAMILIES = [defineFamily({ key: FAMILY.PROGRAMS, tools: [new RunToolsProgram()] })];
