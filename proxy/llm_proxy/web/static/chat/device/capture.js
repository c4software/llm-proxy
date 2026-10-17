/*
  Capture d'écran, Caméra, Codes : une IMAGE prise sur la carte, montrée
  en aperçu avant d'être envoyée (Envoyer, Reprendre, Refuser) — le
  modèle la reçoit jointe au résultat de l'outil. La lecture d'un QR code
  ou d'un code-barres (BarcodeDetector) n'envoie que le texte lu.

  Les flux (caméra, écran) vivent ici, pas dans la carte : ils ne
  survivent pas à un rechargement, et la carte revient alors au début.
*/
import { fn, refused, REFUSE } from "./common.js";

const { ref } = Vue;
const MAX_SIDE = 1920;               // pixels du plus grand côté d'une image envoyée
const QUALITY = 0.9;                 // JPEG
const SCAN_EVERY = 400;              // ms entre deux lectures de code

const streams = new Map();           // id de la carte → MediaStream
const scans = new Map();             // id de la carte → minuterie de lecture
const tick = ref(0);                 // un flux a changé : la carte se redessine

const reason = { reason: { type: "string", description: "Pourquoi, en une phrase, montrée à l'utilisateur." } };

export const FAMILIES = [
  { key: "screen", label: "Capture d'écran", hint: "un onglet, une fenêtre ou l'écran, avec aperçu",
    available: () => !!(navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia),
    tools: [fn("capture_screen", "Demande à l'utilisateur une capture de son écran (un onglet, "
      + "une fenêtre ou l'écran entier, qu'il choisit et vérifie avant de l'envoyer) : pour "
      + "voir une erreur, une interface, un document. Il faut un modèle qui lit les images.",
    reason, ["reason"])] },
  { key: "camera", label: "Caméra", hint: "une photo, avec aperçu",
    available: () => !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia),
    tools: [fn("take_photo", "Demande à l'utilisateur une photo prise avec sa caméra (une "
      + "étiquette, un document, un objet), qu'il vérifie avant de l'envoyer. Il faut un modèle "
      + "qui lit les images.", reason, ["reason"])] },
  { key: "codes", label: "QR et codes-barres", hint: "lus à la caméra ou à l'écran",
    available: () => "BarcodeDetector" in window
      && !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia),
    tools: [fn("scan_code", "Demande à l'utilisateur de lire un QR code ou un code-barres, à la "
      + "caméra ou sur son écran ; rend le texte lu (pas d'image).", reason)] },
];

export const waits = () => true;
export const prepare = (name, args) => ({ reason: String(args.reason || "").trim().slice(0, 300) });

const live = (w) => streams.has(w.id);

export function view(w) {
  tick.value;                        // dépendance : les flux
  const detail = w.reason;
  if (w.tool === "capture_screen") {
    return { title: "Le modèle demande une capture d'écran", detail, preview: w.preview,
             note: w.note, actions: w.preview
      ? [{ id: "send", label: "Envoyer la capture", primary: true }, { id: "pick", label: "Reprendre" }, REFUSE]
      : [{ id: "pick", label: "Choisir quoi capturer", primary: true }, REFUSE] };
  }
  if (w.tool === "take_photo") {
    if (w.preview) {
      return { title: "Le modèle demande une photo", detail, preview: w.preview,
               actions: [{ id: "send", label: "Envoyer la photo", primary: true },
                         { id: "start", label: "Reprendre" }, REFUSE] };
    }
    return { title: "Le modèle demande une photo", detail, video: live(w), note: w.note,
             actions: live(w) ? [{ id: "shoot", label: "Prendre la photo", primary: true }, REFUSE]
               : [{ id: "start", label: "Activer la caméra", primary: true }, REFUSE] };
  }
  if (w.found) {
    return { title: "Code lu", detail, quote: w.found,
             actions: [{ id: "send", label: "Envoyer", primary: true }, { id: "start", label: "Relire" }, REFUSE] };
  }
  return { title: "Le modèle demande de lire un code", detail, video: live(w),
           note: w.note || (live(w) ? "présentez le code à la caméra…" : ""),
           actions: live(w) ? [REFUSE] : [{ id: "start", label: "Caméra", primary: true },
                                         { id: "screen", label: "Sur l'écran" }, REFUSE] };
}

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

// Une image du flux, réduite à MAX_SIDE → un canevas.
async function frame(stream) {
  const video = document.createElement("video");
  video.muted = true;
  video.playsInline = true;
  video.srcObject = stream;
  await video.play();
  if (!video.videoWidth) await new Promise((r) => video.addEventListener("loadedmetadata", r, { once: true }));
  const k = Math.min(1, MAX_SIDE / Math.max(video.videoWidth, video.videoHeight));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(video.videoWidth * k);
  canvas.height = Math.round(video.videoHeight * k);
  canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
  video.srcObject = null;
  return canvas;
}
const jpeg = (canvas) => canvas.toDataURL("image/jpeg", QUALITY);

async function detect(source) {
  const codes = await new window.BarcodeDetector().detect(source);
  return codes.length ? codes.map((c) => `${c.format} : ${c.rawValue}`).join("\n") : "";
}

async function camera(w) {
  stop(w);
  const s = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" }, audio: false });
  streams.set(w.id, s);
  tick.value++;
  return s;
}

export async function act(w, action) {
  if (action === "refuse") {
    stop(w);
    return refused(w.tool === "capture_screen" ? "la capture d'écran"
      : w.tool === "take_photo" ? "la photo" : "de lire un code");
  }
  if (action === "send") {
    stop(w);
    if (w.tool === "scan_code") return { result: "Code lu :\n" + w.found };
    const what = w.tool === "capture_screen" ? "Capture d'écran" : "Photo";
    return { result: `${what} envoyée par l'utilisateur : l'image suit.`, image: w.preview,
             name: (w.tool === "capture_screen" ? "capture" : "photo") + "-" + Date.now().toString(36) + ".jpg" };
  }
  w.note = "";
  try {
    if (action === "pick" || action === "screen") {
      // Le sélecteur du navigateur : un onglet, une fenêtre ou l'écran.
      const s = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: false });
      try {
        const canvas = await frame(s);
        if (action === "pick") { w.preview = jpeg(canvas); return null; }
        w.found = await detect(canvas);
        if (!w.found) w.note = "aucun code trouvé sur cette capture.";
      } finally {
        s.getTracks().forEach((t) => t.stop());
      }
      return null;
    }
    if (action === "start") {
      w.preview = "";
      w.found = "";
      const s = await camera(w);
      if (w.tool === "scan_code") {
        const detector = new window.BarcodeDetector();
        const video = document.createElement("video");
        video.muted = true;
        video.playsInline = true;
        video.srcObject = s;
        await video.play();
        scans.set(w.id, setInterval(async () => {
          if (!video.videoWidth) return;
          const codes = await detector.detect(video).catch(() => []);
          if (codes.length) {
            w.found = codes.map((c) => `${c.format} : ${c.rawValue}`).join("\n");
            stop(w);
          }
        }, SCAN_EVERY));
      }
      return null;
    }
    if (action === "shoot") {
      const s = streams.get(w.id);
      if (!s) return null;
      w.preview = jpeg(await frame(s));
      stop(w);
      return null;
    }
  } catch (e) {
    stop(w);
    if (e.name === "NotAllowedError" || e.name === "AbortError") w.note = "accès refusé ou annulé.";
    else w.note = e.message;
    return null;
  }
  return null;
}
