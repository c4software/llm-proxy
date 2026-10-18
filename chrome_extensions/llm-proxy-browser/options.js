// Les réglages : l'adresse du proxy et la clé (chrome.storage.local).
const $ = (id) => document.getElementById(id);
const LABELS = { on: "connectée", connecting: "connexion…", off: "déconnectée", denied: "clé refusée",
                 replaced: "remplacée par un autre navigateur", unset: "à régler" };
async function show() {
  const { status } = await chrome.storage.session.get("status");
  const s = status || { state: "off" };
  $("state").className = "state " + s.state;
  $("state").lastElementChild.textContent = (LABELS[s.state] || s.state) + (s.detail && s.state !== "on" ? " — " + s.detail : "");
}
chrome.storage.local.get("settings").then(({ settings }) => {
  $("url").value = (settings && settings.url) || "";
  $("key").value = (settings && settings.key) || "";
});
$("form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = $("url").value.trim().replace(/\/+$/, "");
  // HTTPS, sauf sur cette machine : la clé et les ordres ne passent pas en clair.
  let u = null;
  try { u = new URL(url); } catch { /* invalide */ }
  const local = u && ["localhost", "127.0.0.1", "[::1]"].includes(u.hostname);
  if (!u || !(u.protocol === "https:" || (u.protocol === "http:" && local))) {
    $("error").textContent = "Adresse en https:// attendue (http:// seulement pour localhost) : la clé et les ordres ne doivent pas passer en clair.";
    $("error").hidden = false;
    return;
  }
  $("error").hidden = true;
  await chrome.storage.local.set({ settings: { url, key: $("key").value.trim() } });
  chrome.runtime.sendMessage({ type: "reconnect" });
});
chrome.storage.onChanged.addListener((c, area) => { if (area === "session" && c.status) show(); });
show();
