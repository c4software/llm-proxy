// Le minimum du NAVIGATEUR pour charger les modules de la page hors de lui
// (les tests, tests/setup.mjs ; check-tools.mjs) : les modules y touchent en
// se chargeant (document, navigator, la fenêtre, le stockage), rien n'est
// affiché. Vue et VueUse sont les vrais, importés par les modules.
// `listeners` : ce que la page écoute sur le document (visibilitychange…).
export const listeners = {};
globalThis.window = globalThis;
// La fenêtre écoute (VueUse : visibilité, stockage, taille…) ; rien n'arrive.
globalThis.addEventListener ??= () => {};
globalThis.removeEventListener ??= () => {};
globalThis.document = { addEventListener: (t, f) => { listeners[t] = f; }, hidden: false,
  visibilityState: "visible", createElement: () => ({}) };
if (!globalThis.navigator) globalThis.navigator = {};
globalThis.location = { href: "http://localhost/ui" };
// Le stockage du navigateur (VueUse, useLocalStorage) : en mémoire. Le
// `localStorage` de Node, sans fichier, avertit à chaque lecture.
const memory = new Map();
Object.defineProperty(globalThis, "localStorage", { configurable: true, value: {
  getItem: (k) => (memory.has(k) ? memory.get(k) : null), setItem: (k, v) => memory.set(k, String(v)),
  removeItem: (k) => memory.delete(k), clear: () => memory.clear(),
  key: (i) => [...memory.keys()][i] ?? null, get length() { return memory.size; } } });
