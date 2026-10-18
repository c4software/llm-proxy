// @ts-check
/*
  Ce que partagent Capture d'écran, Caméra et Codes : les FLUX (caméra,
  écran) et les images qu'on en tire. Une image est prise sur la carte,
  montrée en aperçu avant d'être envoyée — le modèle la reçoit jointe au
  résultat de l'outil.

  Les flux vivent ici, pas dans la carte : ils ne survivent pas à un
  rechargement, et la carte revient alors au début.
*/
const MAX_SIDE = 1920;               // pixels du plus grand côté d'une image envoyée
const QUALITY = 0.9;                 // JPEG

// id de la carte → {scope, media} : la caméra de la carte (useUserMedia),
// dans sa portée. Réactive : la carte se redessine quand le flux change.
const cams = Vue.shallowReactive(new Map());
const scans = new Map();             // id de la carte → minuterie de lecture

/** @param {string} what */
export const hasMedia = (what) => !!(navigator.mediaDevices && navigator.mediaDevices[what]);

/** La carte a-t-elle son flux ? (lu dans view : une dépendance de Vue) @param {any} w */
export const live = (w) => !!(cams.get(w.id) && cams.get(w.id).media.stream.value);
/** @param {any} w @returns {MediaStream | undefined} */
export const stream = (w) => (cams.get(w.id) ? cams.get(w.id).media.stream.value : undefined);

/** La carte est répondue ou abandonnée : son flux et sa lecture s'arrêtent. @param {any} w */
export function stop(w) {
  clearInterval(scans.get(w.id));
  scans.delete(w.id);
  const cam = cams.get(w.id);
  if (!cam) return;
  cam.media.stop();
  cam.scope.stop();
  cams.delete(w.id);
}

/** La caméra arrière, attachée à la carte → son flux. @param {any} w */
export async function camera(w) {
  stop(w);
  const scope = Vue.effectScope(true);
  const media = scope.run(() => VueUse.useUserMedia({
    constraints: { video: { facingMode: "environment" }, audio: false } }));
  cams.set(w.id, { scope, media });
  try {
    return await media.start();
  } catch (e) {
    stop(w);
    throw e;
  }
}

// Une image prise une fois à l'écran (le sélecteur du navigateur : un
// onglet, une fenêtre ou l'écran ; useDisplayMedia) → un canevas ; le
// flux est coupé.
export async function screenshot() {
  const scope = Vue.effectScope(true);
  const media = scope.run(() => VueUse.useDisplayMedia({ video: true, audio: false }));
  try {
    return await frame(await media.start());
  } finally {
    media.stop();
    scope.stop();
  }
}

// Une lecture répétée du flux de la carte, coupée par stop(w).
export function every(w, ms, fn) {
  clearInterval(scans.get(w.id));
  scans.set(w.id, setInterval(fn, ms));
}

// Un élément <video> qui joue `s`, hors de la page.
export async function player(s) {
  const video = document.createElement("video");
  video.muted = true;
  video.playsInline = true;
  video.srcObject = s;
  await video.play();
  if (!video.videoWidth) await new Promise((r) => video.addEventListener("loadedmetadata", r, { once: true }));
  return video;
}

// Une image du flux, réduite à MAX_SIDE → un canevas.
export async function frame(s) {
  const video = await player(s);
  const k = Math.min(1, MAX_SIDE / Math.max(video.videoWidth, video.videoHeight));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(video.videoWidth * k);
  canvas.height = Math.round(video.videoHeight * k);
  canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
  video.srcObject = null;
  return canvas;
}

export const jpeg = (canvas) => canvas.toDataURL("image/jpeg", QUALITY);
export const imageName = (what) => `${what}-${Date.now().toString(36)}.jpg`;

// Une erreur d'accès à la caméra ou à l'écran → la note de la carte.
export function denied(w, e) {
  stop(w);
  w.note = e.name === "NotAllowedError" || e.name === "AbortError" ? "accès refusé ou annulé." : e.message;
  return null;
}
