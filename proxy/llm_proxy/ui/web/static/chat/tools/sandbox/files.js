// @ts-check
/*
  Les FICHIERS des bacs, côté page.

  En entrée (`files` d'un outil) : des chemins du dossier ouvert, ou des
  noms de pièces jointes texte du fil — lus ici, passés au bac en octets.
  Leurs données ne partent pas au modèle.

  En sortie : ce que le code a produit, rendu sous la réponse.
    texte (csv, json, svg, md…)   un fichier à télécharger (save_file)
    image (png, jpeg, webp, gif)  montrée sous la réponse, et jointe au
                                  résultat pour le modèle s'il lit les
                                  images (la première seulement)
    graphique (chart() du bac)    une carte INTERACTIVE dans le corps de la
                                  réponse (Chart.js, chart-card.js), son
                                  PNG joint au résultat comme une image
*/
import { saveFile } from "../page/save.js";

const MAX_FILE = 100e6;              // octets d'un fichier lu
const MAX_FILES = 20;
const MAX_IMAGE = 5e6;               // octets d'une image produite
const MAX_OUTPUTS = 10;

const IMAGE = { png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", webp: "image/webp", gif: "image/gif" };
const TEXT = /^(txt|csv|tsv|json|jsonl|ndjson|md|markdown|html?|xml|svg|ya?ml|toml|ini|log|sql|js|mjs|css|php|py|sh|ics|tex)$/;

export const FILES_PARAM = { files: { type: "array", items: { type: "string" },
  description: "Fichiers à fournir au code : chemins dans le dossier ouvert, ou noms de pièces jointes." } };

const clean = (p) => String(p || "").trim().replace(/^\.?\/+/, "").replace(/\/+$/, "");
const ext = (name) => ((String(name).match(/\.([^./]+)$/) || [])[1] || "").toLowerCase();

// Les chemins de `args.files`, nettoyés.
export function paths(args) {
  const v = Array.isArray(args.files) ? args.files : args.files ? [args.files] : [];
  return v.map(clean).filter(Boolean);
}

// Les octets d'un fichier nommé par le modèle : le dossier ouvert d'abord,
// puis la pièce jointe texte la plus récente de ce nom.
async function bytesOf(ctx, path) {
  const ws = ctx.ws;
  if (ws && ws.files.has(path)) {
    const file = ws.files.get(path);
    if (file.size > MAX_FILE) throw new Error(`plus de ${MAX_FILE / 1e6} Mo`);
    return new Uint8Array(await file.arrayBuffer());
  }
  const base = path.split("/").pop();
  const msgs = (ctx.conv && ctx.conv.messages) || [];
  for (let i = msgs.length - 1; i >= 0; i--) {
    const a = (msgs[i].attachments || []).find((x) => x.kind === "text" && (x.name === path || x.name === base));
    if (a) return new TextEncoder().encode(a.data);
  }
  throw new Error(ws ? `introuvable dans le dossier « ${ws.name} » ni dans les pièces jointes`
    : "introuvable dans les pièces jointes (et aucun dossier n'est ouvert)");
}

// Les fichiers d'entrée → [{name, bytes}] ; lève une Error dont le message
// se lit tel quel (« fichier « x » : … »).
export async function inputs(ctx, list) {
  if (list.length > MAX_FILES) throw new Error(`${MAX_FILES} fichiers au plus par appel`);
  const out = [];
  for (const p of list) {
    try {
      out.push({ name: p, bytes: await bytesOf(ctx, p) });
    } catch (e) {
      throw new Error(`fichier « ${p} » : ${e.message}`);
    }
  }
  return out;
}

const dataUrl = (bytes, type) => new Promise((resolve, reject) => {
  const r = new FileReader();
  r.onload = () => resolve(r.result);
  r.onerror = () => reject(r.error);
  r.readAsDataURL(new Blob([bytes], { type }));
});

// Les fichiers produits ({name, bytes | text, chart?}) → dans la réponse.
// Rend {lines, image, keep} : ce que le résultat en dit, l'image pour le
// modèle, et les cartes durables (les graphiques interactifs).
export async function deliver(ctx, produced) {
  const lines = [];
  const keep = [];
  let image = "";
  const live = ctx.live || {};
  const charts = (produced || []).filter((f) => f.chart).length;
  for (const [i, f] of (produced || []).entries()) {
    const name = String(f.name || "").split("/").pop().trim();
    if (i >= MAX_OUTPUTS) { lines.push(`Error: « ${name} » non rendu (${MAX_OUTPUTS} fichiers au plus).`); continue; }
    if (!name) { lines.push("Error: un fichier produit sans nom."); continue; }
    const e = ext(name);
    if (IMAGE[e] && f.bytes) {
      if (f.bytes.length > MAX_IMAGE) { lines.push(`Error: image « ${name} » de plus de ${MAX_IMAGE / 1e6} Mo.`); continue; }
      const data = await dataUrl(f.bytes, IMAGE[e]);
      let line;
      if (f.chart) {
        // Un graphique : sa carte interactive, rejouée depuis sa configuration.
        const slot = name.replace(/\.[^.]+$/, "");
        keep.push({ slot, title: f.title || slot, chart: f.chart, width: f.width, height: f.height, file: name });
        line = `Graphique interactif « ${slot} » affiché à l'utilisateur dans le corps de la réponse`
          + (charts > 1 ? ` (${charts} graphiques en tout, tous visibles)` : "")
          + ` ; pour le placer dans ton texte, écrire sur une ligne seule : ![${f.title || slot}](chart:${slot})`;
      } else {
        (live.attachments || (live.attachments = [])).push({ kind: "image", name, data });
        line = `Image « ${name} » montrée à l'utilisateur sous la réponse`;
      }
      if (!ctx.vision) line += " (non jointe : le modèle de la conversation ne lit pas les images).";
      else if (image) line += " (non jointe : seule la première image l'est).";
      else { image = data; line += " ; jointe à ce résultat, à la suite."; }
      lines.push(line);
      continue;
    }
    if (typeof f.text === "string" || (f.bytes && TEXT.test(e))) {
      const content = typeof f.text === "string" ? f.text : new TextDecoder().decode(f.bytes);
      lines.push(saveFile(live, { name, content }));
      continue;
    }
    lines.push(`Error: « ${name} » : seuls le texte et les images png, jpeg, webp, gif sont rendus.`);
  }
  return { lines, image, keep };
}
