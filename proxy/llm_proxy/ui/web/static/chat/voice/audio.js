// @ts-check
/*
  La RÉFÉRENCE d'une voix clonée, préparée dans la page (docs/voix.md,
  « La normaliser ») : le clonage copie le niveau, la pièce et les pauses
  de son enregistrement. Tout se fait ici, sans serveur :

    décodage       ce que le navigateur sait lire (WAV, MP3, Ogg, WebM
                   du micro…), decodeAudioData
    mono 24 kHz    la fréquence des modèles, mélange des canaux
    silences       ceux du début et de la fin coupés (sous -45 dBFS), 0,15 s
                   gardée de chaque côté ; jamais ceux du milieu
    niveau         -20 LUFS (intégré, pondéré K, à portes, BS.1770), crêtes
                   à -1 dBFS au plus : le gain s'arrête avant (pas de
                   limiteur), la voix peut rester un peu sous -20
    WAV            PCM 16 bits, ce qu'attend `reference_audio`
*/
export const RATE = 24000;
const SILENCE_DB = -45;
const KEEP = 0.15;            // secondes de silence gardées à chaque bout
const TARGET_LUFS = -20;
const PEAK_DB = -1;

const db = (x) => 20 * Math.log10(Math.max(x, 1e-9));

/** Un fichier ou un enregistrement → ses échantillons, mono à RATE.
 * @param {Blob} blob @returns {Promise<Float32Array>} */
export async function decode(blob) {
  const bytes = await blob.arrayBuffer();
  const ctx = new OfflineAudioContext(1, 1, RATE);
  let buf;
  try { buf = await ctx.decodeAudioData(bytes); } catch {
    throw new Error("format audio illisible par ce navigateur");
  }
  if (!buf.duration) throw new Error("enregistrement vide");
  // Rééchantillonné et mélangé en mono par le rendu (canaux moyennés).
  const out = new OfflineAudioContext(1, Math.ceil(buf.duration * RATE), RATE);
  const src = out.createBufferSource();
  src.buffer = buf;
  src.connect(out.destination);
  src.start();
  return (await out.startRendering()).getChannelData(0);
}

/** Les silences du début et de la fin retirés, KEEP gardée.
 * @param {Float32Array} s */
export function trim(s) {
  const win = Math.round(RATE * 0.01);
  const loud = (i) => {
    let sum = 0;
    const end = Math.min(s.length, i + win);
    for (let k = i; k < end; k++) sum += s[k] * s[k];
    return db(Math.sqrt(sum / Math.max(1, end - i))) > SILENCE_DB;
  };
  let first = -1, last = -1;
  for (let i = 0; i < s.length; i += win) if (loud(i)) { first = i; break; }
  if (first < 0) throw new Error("aucune voix entendue (tout est sous " + SILENCE_DB + " dB)");
  for (let i = Math.floor((s.length - 1) / win) * win; i >= 0; i -= win) if (loud(i)) { last = i + win; break; }
  const keep = Math.round(RATE * KEEP);
  return s.slice(Math.max(0, first - keep), Math.min(s.length, last + keep));
}

/** La sonie intégrée (LUFS) : pondération K (filtres de la Web Audio),
 * blocs de 400 ms à 75 % de recouvrement, portes absolue et relative.
 * @param {Float32Array} s */
export async function loudness(s) {
  const ctx = new OfflineAudioContext(1, s.length, RATE);
  const buf = ctx.createBuffer(1, s.length, RATE);
  buf.getChannelData(0).set(s);
  const src = ctx.createBufferSource();
  src.buffer = buf;
  const shelf = ctx.createBiquadFilter();
  shelf.type = "highshelf"; shelf.frequency.value = 1500; shelf.gain.value = 4;
  const high = ctx.createBiquadFilter();
  high.type = "highpass"; high.frequency.value = 38; high.Q.value = 0.5;
  src.connect(shelf).connect(high).connect(ctx.destination);
  src.start();
  const k = (await ctx.startRendering()).getChannelData(0);
  const block = Math.round(RATE * 0.4), step = Math.round(block / 4);
  const z = [];
  for (let i = 0; i + block <= k.length; i += step) {
    let sum = 0;
    for (let j = i; j < i + block; j++) sum += k[j] * k[j];
    z.push(sum / block);
  }
  if (!z.length) {   // moins de 400 ms : un seul bloc, tout
    let sum = 0;
    for (const v of k) sum += v * v;
    z.push(sum / Math.max(1, k.length));
  }
  const lufs = (xs) => -0.691 + 10 * Math.log10(Math.max(xs.reduce((a, b) => a + b, 0) / xs.length, 1e-12));
  const abs = z.filter((v) => -0.691 + 10 * Math.log10(Math.max(v, 1e-12)) > -70);
  if (!abs.length) return -70;
  const rel = lufs(abs) - 10;
  const gated = abs.filter((v) => -0.691 + 10 * Math.log10(Math.max(v, 1e-12)) > rel);
  return lufs(gated.length ? gated : abs);
}

/** Au niveau : TARGET_LUFS, sans dépasser PEAK_DB.
 * @param {Float32Array} s @returns {Promise<{samples: Float32Array, lufs: number, gain: number}>} */
export async function level(s) {
  const before = await loudness(s);
  let peak = 0;
  for (const v of s) peak = Math.max(peak, Math.abs(v));
  const gain = Math.min(10 ** ((TARGET_LUFS - before) / 20), 10 ** (PEAK_DB / 20) / Math.max(peak, 1e-9));
  const out = new Float32Array(s.length);
  for (let i = 0; i < s.length; i++) out[i] = s[i] * gain;
  return { samples: out, lufs: before + db(gain), gain: db(gain) };
}

/** Échantillons mono → fichier WAV PCM 16 bits.
 * @param {Float32Array} s */
export function wav(s) {
  const out = new DataView(new ArrayBuffer(44 + s.length * 2));
  const str = (o, t) => { for (let i = 0; i < t.length; i++) out.setUint8(o + i, t.charCodeAt(i)); };
  str(0, "RIFF"); out.setUint32(4, 36 + s.length * 2, true); str(8, "WAVE");
  str(12, "fmt "); out.setUint32(16, 16, true); out.setUint16(20, 1, true); out.setUint16(22, 1, true);
  out.setUint32(24, RATE, true); out.setUint32(28, RATE * 2, true); out.setUint16(32, 2, true);
  out.setUint16(34, 16, true); str(36, "data"); out.setUint32(40, s.length * 2, true);
  for (let i = 0; i < s.length; i++) {
    const v = Math.max(-1, Math.min(1, s[i]));
    out.setInt16(44 + i * 2, v < 0 ? v * 0x8000 : v * 0x7fff, true);
  }
  return new Blob([out.buffer], { type: "audio/wav" });
}

/** Toute la chaîne : un enregistrement ou un fichier → la référence.
 * @param {Blob} blob
 * @returns {Promise<{wav: Blob, duration: number, lufs: number}>} */
export async function normalize(blob) {
  const { samples, lufs } = await level(trim(await decode(blob)));
  return { wav: wav(samples), duration: samples.length / RATE, lufs };
}

/** Le contenu d'un Blob en base64 (sans l'en-tête de data URL).
 * @param {Blob} blob @returns {Promise<string>} */
export function base64(blob) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(",", 2)[1] || "");
    r.onerror = () => reject(r.error);
    r.readAsDataURL(blob);
  });
}
