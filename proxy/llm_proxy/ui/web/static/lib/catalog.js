/*
  Le CATALOGUE des modèles : /ui/models?all=1, tout le catalogue avec le
  type de chacun (`chat` : de conversation, `vision`, `image` : d'images,
  `context`). Lu UNE fois pour toute la page — le chat, la voix et la
  configuration le partagent.

  Gardé aussi dans le NAVIGATEUR (localStorage ; c'est celui du proxy,
  le même pour tous) : à l'ouverture, la page montre le dernier connu
  tout de suite et le relit derrière.

    catalogState           réactif : {data, loading, error, at}
    catalog()              les entrées : celles gardées s'il y en a (la
                           première fois, relues derrière), sinon attend
                           la lecture
    refreshCatalog()       relit (le bouton de la liste)
*/
import { call } from "./http.js";

const { reactive } = Vue;
const KEY = "llm-proxy-catalog";

function stored() {
  try {
    const got = JSON.parse(localStorage.getItem(KEY) || "null");
    return got && Array.isArray(got.data) ? got : null;
  } catch { return null; }
}

const kept = stored();
export const catalogState = reactive({
  data: kept ? kept.data : [], at: kept ? kept.at : 0, loading: false, error: "" });
let reading = null;
let fresh = false;

export function refreshCatalog() {
  if (reading) return reading;
  fresh = true;
  catalogState.loading = true;
  reading = call("GET", "/ui/models?all=1").then((r) => {
    const data = (r.data || []).filter((m) => m.id);
    Object.assign(catalogState, { data, at: Date.now(), error: "" });
    try {
      localStorage.setItem(KEY, JSON.stringify({ at: catalogState.at, data }));
    } catch { /* sans stockage : relu à chaque ouverture */ }
    return data;
  }, (e) => {
    catalogState.error = e.message;
    throw e;
  }).finally(() => {
    catalogState.loading = false;
    reading = null;
  });
  return reading;
}

export function catalog() {
  if (!fresh) {
    const got = refreshCatalog();
    if (!catalogState.data.length) return got;
    got.catch(() => { /* dit par catalogState.error ; le gardé reste */ });
  }
  return catalogState.data.length ? Promise.resolve(catalogState.data) : refreshCatalog();
}
