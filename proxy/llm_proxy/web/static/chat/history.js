/*
  L'HISTORIQUE du chat, gardé par le proxy et lié au compte connecté
  (conversations.py) : le même compte retrouve ses conversations sur tout
  navigateur, tout appareil.

    load()           les conversations du compte
    save(conv)       le document entier, après avoir envoyé ses images
    drop(id)         supprime (images comprises)

  Les IMAGES (jointes, générées) partent à part (/ui/assets) : le
  document n'en garde que l'adresse, /ui/assets/<id>. Ce qui part au
  modèle, ou à la retouche, les reprend en data URL (inline, dataUrl).
  Une conversation incognito n'est jamais envoyée.
*/
import { call } from "../lib/http.js";

const ASSET = "/ui/assets/";
export const isAsset = (url) => typeof url === "string" && url.startsWith(ASSET);
// Une image que la page tient elle-même (et peut donc retoucher).
export const ownImage = (url) => typeof url === "string" && (url.startsWith("data:") || isAsset(url));

export async function load() {
  const r = await call("GET", "/ui/conversations");
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
      cache.set(r.url, data);
      sent.set(data, r.url);
    }
    return sent.get(data);
  };
  const isData = (v) => typeof v === "string" && v.startsWith("data:");
  for (const m of conv.messages || []) {
    for (const a of m.attachments || []) {
      if (a.kind !== "image" || !isData(a.data)) continue;
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

export async function save(conv) {
  await upload(conv);
  await call("PUT", "/ui/conversations/" + encodeURIComponent(conv.id), conv);
}

export async function drop(id) {
  await call("DELETE", "/ui/conversations/" + encodeURIComponent(id));
}

// ── vers le modèle ──────────────────────────────────────────────────────
const cache = new Map();            // adresse → data URL
export async function dataUrl(url) {
  if (!isAsset(url)) return url;
  if (cache.has(url)) return cache.get(url);
  const r = await fetch(url);
  if (!r.ok) throw new Error(`Image de l'historique introuvable (HTTP ${r.status}).`);
  const blob = await r.blob();
  const out = await new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve(fr.result);
    fr.onerror = () => reject(fr.error);
    fr.readAsDataURL(blob);
  });
  cache.set(url, out);
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
