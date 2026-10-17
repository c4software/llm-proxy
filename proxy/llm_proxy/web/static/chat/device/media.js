/*
  Ce que partagent Capture d'écran, Caméra et Codes : les FLUX (caméra,
  écran) et les images qu'on en tire. Une image est prise sur la carte,
  montrée en aperçu avant d'être envoyée — le modèle la reçoit jointe au
  résultat de l'outil.

  Les flux vivent ici, pas dans la carte : ils ne survivent pas à un
  rechargement, et la carte revient alors au début.
*/
const { ref } = Vue;
const MAX_SIDE = 1920;               // pixels du plus grand côté d'une image envoyée
const QUALITY = 0.9;                 // JPEG

const streams = new Map();           // id de la carte → MediaStream
const scans = new Map();             // id de la carte → minuterie de lecture
const tick = ref(0);                 // un flux a changé : la carte se redessine

export const hasMedia = (what) => !!(navigator.mediaDevices && navigator.mediaDevices[what]);

// La carte a-t-elle son flux ? Lu dans view : une dépendance de Vue.
export function live(w) {
  tick.value;
  return streams.has(w.id);
}
export const stream = (w) => streams.get(w.id);

// La carte est répondue ou abandonnée : ses flux et sa lecture s'arrêtent.
export function stop(w) {
  clearInterval(scans.get(w.id));
  scans.delete(w.id);
  const s = streams.get(w.id);
  if (s) s.getTracks().forEach((t) => t.stop());
  if (streams.delete(w.id)) tick.value++;
}

// L'élément <video> de la carte reçoit son flux (ref de fonction de Vue).
export function attachVideo(el, w) {
  if (el && streams.has(w.id) && el.srcObject !== streams.get(w.id)) {
    el.srcObject = streams.get(w.id);
    el.play().catch(() => {});
  }
}

// La caméra arrière, attachée à la carte.
export async function camera(w) {
  stop(w);
  const s = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" }, audio: false });
  streams.set(w.id, s);
  tick.value++;
  return s;
}

// Une image prise une fois à l'écran (le sélecteur du navigateur : un
// onglet, une fenêtre ou l'écran) → un canevas ; le flux est coupé.
export async function screenshot() {
  const s = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: false });
  try { return await frame(s); } finally { s.getTracks().forEach((t) => t.stop()); }
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
