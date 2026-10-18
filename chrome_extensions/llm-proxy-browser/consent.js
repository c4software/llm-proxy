// La demande d'accord pour un site (ouverte par background.js, une minute).
const q = new URLSearchParams(location.search);
document.getElementById("host").textContent = q.get("host") || "?";
document.getElementById("proxy").textContent = q.get("proxy") || "?";
const answer = (allow) => chrome.runtime.sendMessage({ type: "consent", id: q.get("id"), allow });
document.getElementById("yes").addEventListener("click", () => answer(true));
document.getElementById("no").addEventListener("click", () => answer(false));
let left = 60;
const tick = () => { document.getElementById("left").textContent = `refus automatique dans ${left} s`; if (left-- > 0) setTimeout(tick, 1000); };
tick();
document.getElementById("yes").focus();
