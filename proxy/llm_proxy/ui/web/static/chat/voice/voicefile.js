// @ts-check
/*
  Une VOIX CLONÉE en un fichier, pour la réutiliser : sa référence WAV,
  avec son nom et sa TRANSCRIPTION dans les métadonnées du WAV (bloc
  LIST/INFO : INAM le nom, ICMT la transcription, en UTF-8). Le fichier
  reste un WAV ordinaire — il s'écoute, et sert tel quel à curl
  (docs/voix.md) — et se réimporte dans un autre navigateur sans
  ressaisir la transcription.

    pack(wav, {name, text})   → le fichier (Blob)
    unpack(blob)              → {name, text} lus dans un WAV, «» sinon
*/
const enc = new TextEncoder();

/** Un sous-bloc INFO : identifiant, taille, texte terminé par un zéro,
 * aligné sur 2 octets. @param {string} id @param {string} text */
function info(id, text) {
  const body = enc.encode(text + "\0");
  const out = new Uint8Array(8 + body.length + (body.length % 2));
  out.set(enc.encode(id), 0);
  new DataView(out.buffer).setUint32(4, body.length, true);
  out.set(body, 8);
  return out;
}

/** @param {Blob} wav @param {{name: string, text: string}} tags @returns {Promise<Blob>} */
export async function pack(wav, tags) {
  const bytes = new Uint8Array(await wav.arrayBuffer());
  const items = [info("INAM", tags.name || ""), info("ICMT", tags.text || "")];
  const size = 4 + items.reduce((n, x) => n + x.length, 0);
  const list = new Uint8Array(8 + size);
  list.set(enc.encode("LIST"), 0);
  new DataView(list.buffer).setUint32(4, size, true);
  list.set(enc.encode("INFO"), 8);
  let at = 12;
  for (const x of items) { list.set(x, at); at += x.length; }
  const out = new Uint8Array(bytes.length + list.length);
  out.set(bytes, 0);
  out.set(list, bytes.length);
  new DataView(out.buffer).setUint32(4, out.length - 8, true);    // taille RIFF
  return new Blob([out], { type: "audio/wav" });
}

/** @param {Blob} blob @returns {Promise<{name: string, text: string}>} */
export async function unpack(blob) {
  const got = { name: "", text: "" };
  const bytes = new Uint8Array(await blob.arrayBuffer());
  const view = new DataView(bytes.buffer);
  const tag = (o) => String.fromCharCode(...bytes.subarray(o, o + 4));
  if (bytes.length < 12 || tag(0) !== "RIFF" || tag(8) !== "WAVE") return got;
  const dec = new TextDecoder();
  for (let o = 12; o + 8 <= bytes.length;) {
    const size = view.getUint32(o + 4, true);
    if (tag(o) === "LIST" && tag(o + 8) === "INFO") {
      for (let p = o + 12; p + 8 <= Math.min(o + 8 + size, bytes.length);) {
        const n = view.getUint32(p + 4, true);
        const text = dec.decode(bytes.subarray(p + 8, p + 8 + n)).replace(/\0+$/, "");
        if (tag(p) === "INAM") got.name = text;
        if (tag(p) === "ICMT") got.text = text;
        p += 8 + n + (n % 2);
      }
    }
    o += 8 + size + (size % 2);
  }
  return got;
}

/** Le nom de fichier d'une voix. @param {string} name */
export const fileName = (name) => (name || "voix").replace(/[\\/:*?"<>|]+/g, "_") + ".wav";
