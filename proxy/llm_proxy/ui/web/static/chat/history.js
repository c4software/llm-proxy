// @ts-check
/*
  L'HISTORIQUE du chat, gardé par le proxy et lié au compte connecté
  (conversations.py) : le même compte retrouve ses conversations sur tout
  navigateur, tout appareil.

    page(offset)     une page de la liste du menu : des résumés (id, titre,
                     dates, nombre de messages), sans les messages
    one(id)          une conversation entière (null : inconnue)
    search(q)        les résumés de celles qui contiennent chaque mot
    save(conv, o)    le document entier, après avoir envoyé ses images →
                     { text : le JSON envoyé, rev : sa révision } ;
                     o.rev : celle sur laquelle il s'appuie (409 sinon)
    drop(id)         supprime (images comprises)

  Les IMAGES (jointes, générées) et l'AUDIO (réponse d'un modèle de
  voix, voice/speech.js) partent à part (/ui/assets) : le
  document n'en garde que l'adresse, /ui/assets/<id>. Ce qui part au
  modèle, ou à la retouche, les reprend en data URL (inline, dataUrl).
  Une conversation incognito n'est jamais envoyée.
*/
import { call } from "../lib/http.js";

const ASSET = "/ui/assets/";
const isAsset = (url) => typeof url === "string" && url.startsWith(ASSET);
// Une image que la page tient elle-même (et peut donc retoucher).
export const ownImage = (url) => typeof url === "string" && (url.startsWith("data:") || isAsset(url));

export const PAGE = 40;

export async function page(offset = 0) {
  const r = await call("GET", `/ui/conversations?summary=1&limit=${PAGE}&offset=${offset}`);
  return { list: Array.isArray(r && r.data) ? r.data : [], next: r ? r.next : null,
           total: r && r.usage ? r.usage.conversations : 0 };
}

export async function one(id) {
  try {
    return await call("GET", "/ui/conversations/" + encodeURIComponent(id));
  } catch (e) {
    if (e.status === 404 || e.status === 400) return null;
    throw e;
  }
}

export async function search(q) {
  const r = await call("GET", "/ui/conversations?q=" + encodeURIComponent(q));
  return Array.isArray(r && r.data) ? r.data : [];
}

// Les images encore en data URL rejoignent le proxy ; leur adresse
// remplace la donnée DANS la conversation (elle s'affiche à l'identique).
// Une même image (jointe à la réponse ET rendue par l'outil qui l'a
// prise) ne part qu'une fois.
async function upload(conv) {
  const sent = new Map();            // data URL → adresse
  const put = async (data, name) => {
    if (!sent.has(data)) {
      const r = await call("POST", "/ui/assets", { conversation: conv.id, name, data });
      remember(r.url, data);
      sent.set(data, r.url);
    }
    return sent.get(data);
  };
  const isData = (v) => typeof v === "string" && v.startsWith("data:");
  for (const m of conv.messages || []) {
    for (const a of m.attachments || []) {
      if ((a.kind !== "image" && a.kind !== "audio") || !isData(a.data)) continue;
      const data = a.data;
      const url = await put(data, a.name);
      if (a.data === data) a.data = url;
    }
    // Les images rendues par un outil de la page (capture, photo).
    const steps = [...(m.steps || []), ...(m.step ? [m.step] : [])];
    for (const c of steps.flatMap((st) => st.calls || [])) {
      if (!isData(c.image)) continue;
      const data = c.image;
      const url = await put(data, c.name + ".jpg");
      if (c.image === data) c.image = url;
    }
  }
}

// Sérialisé UNE fois, après l'envoi des images (qui y remplacent leurs
// données) : ce qui est mémorisé comme enregistré est exactement ce qui
// est parti, pas ce que la conversation est devenue pendant l'envoi.
export async function save(conv, { rev = null, keepalive = false } = {}) {
  await upload(conv);
  const text = JSON.stringify(conv);
  const r = await call("PUT", "/ui/conversations/" + encodeURIComponent(conv.id), undefined, {
    raw: text, keepalive, headers: rev !== null ? { "if-match": String(rev) } : {} });
  return { text, rev: r && Number.isInteger(r.rev) ? r.rev : null };
}

export async function drop(id) {
  await call("DELETE", "/ui/conversations/" + encodeURIComponent(id));
}

// ── vers le modèle ──────────────────────────────────────────────────────
// adresse → data URL, borné : les plus anciennes sortent au-delà de
// CACHE_MAX caractères (une Map garde l'ordre d'insertion ; relue, une
// entrée repasse en dernier).
const cache = new Map();
const CACHE_MAX = 64 * 1024 * 1024;
let cached = 0;
function remember(url, data) {
  if (cache.has(url)) cached -= cache.get(url).length;
  cache.delete(url);
  cache.set(url, data);
  cached += data.length;
  for (const [k, v] of cache) {
    if (cached <= CACHE_MAX || k === url) break;
    cache.delete(k);
    cached -= v.length;
  }
}
// Un autre compte connecté : rien de l'ancien ne reste.
export function forgetCache() { cache.clear(); cached = 0; }
export async function dataUrl(url) {
  if (!isAsset(url)) return url;
  if (cache.has(url)) { const hit = cache.get(url); remember(url, hit); return hit; }
  const r = await fetch(url);
  if (!r.ok) throw new Error(`Image de l'historique introuvable (HTTP ${r.status}).`);
  const blob = await r.blob();
  const out = await new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve(fr.result);
    fr.onerror = () => reject(fr.error);
    fr.readAsDataURL(blob);
  });
  remember(url, out);
  return out;
}

// Les messages de l'API : chaque image /ui/assets/… → sa data URL.
export async function inline(messages) {
  for (const msg of messages) {
    if (!Array.isArray(msg.content)) continue;
    for (const part of msg.content) {
      if (part.type === "image_url" && part.image_url && isAsset(part.image_url.url)) {
        part.image_url = { ...part.image_url, url: await dataUrl(part.image_url.url) };
      }
    }
  }
  return messages;
}
