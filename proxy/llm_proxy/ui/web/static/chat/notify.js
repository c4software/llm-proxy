/*
  Prévenir quand une réponse se termine — ou qu'une question vous
  attend — pendant que l'onglet est en arrière-plan : le titre de l'onglet
  le dit (« ● »), et, si vous l'avez permis (réglages du chat), une
  notification du navigateur. Rien quand l'onglet est au premier plan.
*/
const KEY = "llm-proxy-chat-notify";
const MARK = "● ";

// Le choix, gardé dans ce navigateur (« 1 » ou « 0 », comme avant) ;
// l'onglet regardé ou non (useDocumentVisibility).
const wanted = VueUse.useLocalStorage(KEY, false,
  { serializer: { read: (v) => v === "1", write: (v) => (v ? "1" : "0") } });
const visibility = VueUse.useDocumentVisibility();
const hidden = () => visibility.value === "hidden";

export const supported = () => "Notification" in window;
export function enabled() {
  return !!wanted.value && supported() && Notification.permission === "granted";
}

// Activer : la permission du navigateur est demandée (geste de l'utilisateur).
export async function setEnabled(on) {
  if (on && supported() && Notification.permission !== "granted") {
    if ((await Notification.requestPermission()) !== "granted") on = false;
  }
  wanted.value = on;
  return on;
}

// Le titre revient à la normale dès que l'onglet est regardé.
Vue.watch(visibility, () => {
  if (!hidden() && document.title.startsWith(MARK)) document.title = document.title.slice(MARK.length);
});

// Une notification demandée (outil `notify`) : même onglet au premier
// plan, si le navigateur les permet.
export function show(title, body) {
  try {
    const n = new Notification(title, { body, icon: "/ui/static/icon-192.png" });
    n.onclick = () => { window.focus(); n.close(); };
  } catch { /* refusée ou indisponible */ }
}

export function notify(title, body) {
  if (!hidden()) return;
  if (!document.title.startsWith(MARK)) document.title = MARK + document.title;
  if (!enabled()) return;
  try {
    const n = new Notification(title, { body: body.slice(0, 180), tag: "llm-proxy-chat",
                                        icon: "/ui/static/icon-192.png" });
    n.onclick = () => { window.focus(); n.close(); };
  } catch { /* refusée ou indisponible : le titre suffit */ }
}
