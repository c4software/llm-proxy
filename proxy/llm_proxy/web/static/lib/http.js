/*
  Les appels JSON de la page à son proxy (même cookie de session).

  call(méthode, url, corps?) → le JSON de la réponse. Une réponse en
  erreur lève une Error dont le message est celui du proxy
  (error.message), sinon « HTTP <statut> », et qui porte `status`.
*/
export async function call(method, url, body) {
  const r = await fetch(url, {
    method,
    headers: { accept: "application/json",
               ...(body !== undefined ? { "content-type": "application/json" } : {}) },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  let json = null;
  try { json = await r.json(); } catch { /* corps vide */ }
  if (!r.ok) {
    const err = new Error((json && json.error && json.error.message) || `HTTP ${r.status}`);
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
