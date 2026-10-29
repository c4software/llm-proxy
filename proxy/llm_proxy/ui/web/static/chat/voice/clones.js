// @ts-check
/*
  Les VOIX CLONÉES du compte, gardées par le proxy (storage/voices.py,
  /ui/voices) : chacune sa référence normalisée (audio.js, un WAV de
  quelques centaines de Ko) et sa transcription mot pour mot. Le même
  compte les retrouve sur tout navigateur. La référence repart de la page
  avec CHAQUE requête de synthèse (docs/voix.md).

    clones          réactif : [{id, name, text, duration, created, source}]
    load()          les relit (une fois suffit, au premier besoin)
    add(v) / rename(id, nom) / remove(id)
    wav(id)         la référence (Blob), lue une fois puis gardée en mémoire
    reference(id)   {audio: base64, text} pour /v1/audio/speech, gardé en mémoire
*/
import { base64 } from "./audio.js";
import { call } from "../../lib/http.js";

const BASE = "/ui/voices/";

/** @typedef {{id: string, name: string, text: string, duration: number,
 *   created: number, source: string}} Clone */

/** @type {any} */
export const clones = Vue.ref([]);
/** @type {Promise<void> | null} */
let loading = null;
/** @type {Map<string, Promise<Blob>>} */
const blobs = new Map();
/** @type {Map<string, string>} */
const encoded = new Map();

export function load() {
  if (!loading) {
    loading = call("GET", "/ui/voices").then((r) => {
      clones.value = (r && r.data) || [];
    }, () => { clones.value = []; loading = null; });
  }
  return loading;
}

/** @param {Omit<Clone, "id" | "created"> & {wav: Blob}} v @returns {Promise<Clone>} */
export async function add(v) {
  const { wav: blob, ...meta } = v;
  const id = crypto.randomUUID();
  const clone = await call("PUT", BASE + id, { ...meta, data: "data:audio/wav;base64," + await base64(blob) });
  blobs.set(id, Promise.resolve(blob));
  clones.value = [...clones.value, clone];
  return clone;
}

/** @param {string} id @param {string} name */
export async function rename(id, name) {
  if (!clones.value.some((x) => x.id === id) || !name.trim()) return;
  const next = await call("PATCH", BASE + encodeURIComponent(id), { name: name.trim() });
  clones.value = clones.value.map((x) => (x.id === id ? next : x));
}

/** @param {string} id */
export async function remove(id) {
  await call("DELETE", BASE + encodeURIComponent(id));
  blobs.delete(id);
  encoded.delete(id);
  clones.value = clones.value.filter((x) => x.id !== id);
}

/** @param {string} id @returns {Promise<Blob>} */
export function wav(id) {
  if (!blobs.has(id)) {
    const got = fetch(BASE + encodeURIComponent(id) + "/audio").then((r) => {
      if (!r.ok) throw new Error(`référence de la voix : HTTP ${r.status}`);
      return r.blob();
    });
    got.catch(() => blobs.delete(id));
    blobs.set(id, got);
  }
  return /** @type {Promise<Blob>} */ (blobs.get(id));
}

/** @param {string} id @returns {Promise<{audio: string, text: string} | null>} */
export async function reference(id) {
  await load();
  const c = clones.value.find((x) => x.id === id);
  if (!c) return null;
  if (!encoded.has(id)) encoded.set(id, await base64(await wav(id)));
  return { audio: /** @type {string} */ (encoded.get(id)), text: c.text };
}
