// Le menu de l'extension : l'état du lien, Arrêter / Reprendre, les
// onglets de l'IA, les sites autorisés (un clic retire l'accord).
const $ = (id) => document.getElementById(id);
const LABELS = { on: "Connecté au proxy", connecting: "Connexion…", off: "Déconnecté", denied: "Clé refusée",
                 replaced: "Un autre navigateur a pris la place", unset: "À régler (Options)" };
const item = (text, button, onClick) => {
  const li = document.createElement("li");
  const span = document.createElement("span");
  span.textContent = text;
  span.title = text;
  li.append(span);
  if (button) { const b = document.createElement("button"); b.textContent = button; b.addEventListener("click", onClick); li.append(b); }
  return li;
};
async function show() {
  const { status, tabs } = await chrome.storage.session.get(["status", "tabs"]);
  const { stopped, allowed } = await chrome.storage.local.get(["stopped", "allowed"]);
  const s = status || { state: "off" };
  $("state").className = "state " + s.state;
  $("state").lastElementChild.textContent = stopped ? "Arrêté — l'IA n'agit plus" : (LABELS[s.state] || s.state);
  $("detail").textContent = s.state === "on" ? "" : s.detail || "";
  $("toggle").textContent = stopped ? "Reprendre" : "Arrêter";
  $("toggle").className = stopped ? "primary" : "";
  const list = (await Promise.all((tabs || []).map((id) => chrome.tabs.get(id).catch(() => null)))).filter(Boolean);
  $("tabs").replaceChildren(...(list.length ? list.map((t) => item(t.title || t.url, "Voir",
    () => { chrome.tabs.update(t.id, { active: true }); chrome.windows.update(t.windowId, { focused: true }); }))
    : [item("aucun", "", null)]));
  const hosts = Object.keys(allowed || {}).sort();
  $("sites").replaceChildren(...(hosts.length ? hosts.map((h) => item(h, "Retirer", async () => {
    const { allowed: a } = await chrome.storage.local.get("allowed");
    delete a[h];
    await chrome.storage.local.set({ allowed: a });
  })) : [item("aucun", "", null)]));
}
$("toggle").addEventListener("click", async () => {
  const { stopped } = await chrome.storage.local.get("stopped");
  chrome.runtime.sendMessage({ type: stopped ? "resume" : "stop" });
});
$("share").addEventListener("click", async () => {
  const r = await chrome.runtime.sendMessage({ type: "share" });
  $("detail").textContent = r && r.error ? r.error : "Onglet confié à l'IA.";
  show();
});
$("options").addEventListener("click", () => chrome.runtime.openOptionsPage());
chrome.storage.onChanged.addListener(show);
show();
