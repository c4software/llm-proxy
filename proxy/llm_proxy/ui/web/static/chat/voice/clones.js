// @ts-check
/*
  Les VOIX CLONÉES de ce navigateur (IndexedDB, « llm-proxy-voices ») :
  chacune sa référence normalisée (audio.js, un WAV de quelques centaines
  de Ko — trop pour localStorage) et sa transcription mot pour mot. Le
  proxy n'en garde rien : la référence part avec CHAQUE requête de
  synthèse (docs/voix.md).

    clones          réactif : [{id, name, text, wav, duration, created, source}]
    load()          les relit (une fois suffit, au premier besoin)
    add(v) / rename(id, nom) / remove(id)
    reference(id)   {audio: base64, text} pour /v1/audio/speech, gardé en mémoire
*/
import { base64 } from "./audio.js";

const DB = "llm-proxy-voices";
const STORE = "voices";

/** @typedef {{id: string, name: string, text: string, wav: Blob, duration: number,
 *   created: number, source: string}} Clone */

/** @type {any} */
export const clones = Vue.ref([]);
/** @type {Promise<IDBDatabase> | null} */
let opening = null;
/** @type {Promise<void> | null} */
let loading = null;
/** @type {Map<string, string>} */
const encoded = new Map();

function open() {
  if (!opening) {
    opening = new Promise((resolve, reject) => {
      const req = indexedDB.open(DB, 1);
      req.onupgradeneeded = () => req.result.createObjectStore(STORE, { keyPath: "id" });
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
    opening.catch(() => { opening = null; });
  }
  return opening;
}

/** @param {IDBTransactionMode} mode @param {(s: IDBObjectStore) => IDBRequest} fn */
async function tx(mode, fn) {
  const db = await open();
  return new Promise((resolve, reject) => {
    const t = db.transaction(STORE, mode);
    const req = fn(t.objectStore(STORE));
    t.oncomplete = () => resolve(req.result);
    t.onerror = () => reject(t.error);
  });
}

export function load() {
  if (!loading) {
    loading = tx("readonly", (s) => s.getAll()).then((all) => {
      clones.value = (/** @type {Clone[]} */ (all) || []).sort((a, b) => a.created - b.created);
    }, () => { clones.value = []; });
  }
  return loading;
}

/** @param {Omit<Clone, "id" | "created">} v @returns {Promise<Clone>} */
export async function add(v) {
  const clone = { ...v, id: crypto.randomUUID(), created: Date.now() };
  await tx("readwrite", (s) => s.put(clone));
  clones.value = [...clones.value, clone];
  return clone;
}

/** @param {string} id @param {string} name */
export async function rename(id, name) {
  const c = clones.value.find((x) => x.id === id);
  if (!c || !name.trim()) return;
  const next = { ...c, name: name.trim() };
  await tx("readwrite", (s) => s.put(next));
  clones.value = clones.value.map((x) => (x.id === id ? next : x));
}

/** @param {string} id */
export async function remove(id) {
  await tx("readwrite", (s) => s.delete(id));
  encoded.delete(id);
  clones.value = clones.value.filter((x) => x.id !== id);
}

/** @param {string} id @returns {Promise<{audio: string, text: string} | null>} */
export async function reference(id) {
  await load();
  const c = clones.value.find((x) => x.id === id);
  if (!c) return null;
  if (!encoded.has(id)) encoded.set(id, await base64(c.wav));
  return { audio: /** @type {string} */ (encoded.get(id)), text: c.text };
}
