// @ts-check
/*
  Les appels JSON de la page à son proxy (même cookie de session).

  call(méthode, url, corps?, options?) → le JSON de la réponse. Une
  réponse en erreur lève une Error dont le message est celui du proxy
  (error.message), sinon « HTTP <statut> », et qui porte `status`.
  options : `raw` (le corps déjà sérialisé, à la place de `corps`),
  `headers` (en plus), `keepalive` (la requête survit à la page quittée ;
  le navigateur la refuse au-delà de 64 Kio, elle part alors sans).
*/
const KEEPALIVE_MAX = 60_000;
/** @param {string} method @param {string} url @param {any} [body]
 * @param {{raw?: string, headers?: Record<string, string>, keepalive?: boolean}} [options] */
export async function call(method, url, body, { raw, headers = {}, keepalive = false } = {}) {
  const text = raw !== undefined ? raw : body !== undefined ? JSON.stringify(body) : undefined;
  const r = await fetch(url, {
    method,
    headers: { accept: "application/json",
               ...(text !== undefined ? { "content-type": "application/json" } : {}), ...headers },
    body: text,
    keepalive: keepalive && (text === undefined || text.length < KEEPALIVE_MAX),
  });
  let json = null;
  try { json = await r.json(); } catch { /* corps vide */ }
  if (!r.ok) {
    const err = /** @type {Error & {status?: number}} */ (new Error((json && json.error && json.error.message) || `HTTP ${r.status}`));
    err.status = r.status;
    throw err;
  }
  return json;
}

// GET d'une route de lecture, paramètres en query string (null ignorés).
// Lève le STATUT seul (un nombre) : la boucle des statistiques le compare.
export function ask(url, params = {}) {
  const q = Object.entries(params)
    .filter(([, v]) => v !== null && v !== undefined)
    .map(([k, v]) => k + "=" + encodeURIComponent(v)).join("&");
  return fetch(url + (q ? "?" + q : ""), { headers: { accept: "application/json" } })
    .then((r) => (r.ok ? r.json() : Promise.reject(r.status)));
}
