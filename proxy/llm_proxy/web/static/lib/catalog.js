/*
  Le CATALOGUE des modèles : /ui/models?all=1, tout le catalogue avec le
  type de chacun (`chat` : de conversation, `vision`, `image` : d'images,
  `context`). Lu UNE fois pour toute la page — le chat, la voix et la
  configuration le partagent. Un échec n'est pas gardé : l'appel suivant
  relit.
*/
import { call } from "./http.js";

let loading = null;
export function catalog() {
  return loading || (loading = call("GET", "/ui/models?all=1").then(
    (r) => (r.data || []).filter((m) => m.id),
    (e) => { loading = null; throw e; }));
}
