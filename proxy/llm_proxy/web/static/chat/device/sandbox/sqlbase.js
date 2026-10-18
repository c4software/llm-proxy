// @ts-check
/*
  La BASE SQLite d'une conversation (run_sql) : ses octets, gardés dans
  CE navigateur (IndexedDB), jamais envoyés au proxy. Une conversation
  incognito, ou un navigateur sans IndexedDB, la garde en mémoire seulement
  (perdue au rechargement).

    load(id) → Uint8Array, ou null      save(id, octets, durable)
    drop(id)                            la conversation est supprimée
*/
const DB = "llm-proxy-sql";
const STORE = "bases";

const memory = new Map();            // id → octets, sans IndexedDB
let opening = null;

function open() {
  if (!opening) {
    opening = new Promise((resolve, reject) => {
      const req = indexedDB.open(DB, 1);
      req.onupgradeneeded = () => req.result.createObjectStore(STORE);
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
    opening.catch(() => { opening = null; });
  }
  return opening;
}

async function tx(mode, fn) {
  const db = await open();
  return new Promise((resolve, reject) => {
    const t = db.transaction(STORE, mode);
    const req = fn(t.objectStore(STORE));
    t.oncomplete = () => resolve(req.result);
    t.onerror = () => reject(t.error);
  });
}

export async function load(id) {
  if (memory.has(id)) return memory.get(id);
  try {
    const v = await tx("readonly", (s) => s.get(id));
    return v ? new Uint8Array(v) : null;
  } catch { return null; }
}

// `durable` faux (incognito) : en mémoire seulement. Rend vrai si la base
// survivra à un rechargement.
export async function save(id, bytes, durable) {
  if (durable) {
    try {
      await tx("readwrite", (s) => s.put(bytes, id));
      memory.delete(id);
      return true;
    } catch { /* sans IndexedDB : en mémoire */ }
  }
  memory.set(id, bytes);
  return false;
}

export async function drop(id) {
  memory.delete(id);
  try { await tx("readwrite", (s) => s.delete(id)); } catch { /* rien à effacer */ }
}
