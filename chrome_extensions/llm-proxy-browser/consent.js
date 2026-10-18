// Une demande d'accord, ouverte par background.js (une minute) : un SITE
// (kind=site, host) ou une SESSION (kind=session : qui appelle, dit par le proxy).
const q = new URLSearchParams(location.search);
const $ = (id) => document.getElementById(id);
const kind = q.get("kind") === "session" ? "session" : "site";
$(kind).hidden = false;
document.querySelectorAll(".proxy").forEach((e) => { e.textContent = q.get("proxy") || "?"; });

// Le logiciel client, reconnu à son User-Agent quand c'est possible.
function software(ua) {
  const known = [[/codex/i, "Codex"], [/\bomp\b|oh-my-pi/i, "omp"], [/\bpi[-/ ]|pi-coding-agent/i, "pi"],
    [/claude/i, "Claude Code"], [/curl\//i, "curl"], [/python|httpx|aiohttp|requests/i, "script Python"],
    [/node|undici|axios/i, "script Node"], [/Mozilla\//, "navigateur web"]];
  const hit = known.find(([re]) => re.test(ua));
  return hit ? hit[1] : "inconnu";
}
const ROUTES = { "/v1/chat/completions": "API Chat Completions", "/v1/responses": "API Responses",
  "/v1/messages": "API Anthropic (Messages)", "/mcp": "serveur MCP", "/v1/tools": "appel direct (/v1/tools)",
  "/ui/chat/completions": "chat de la page du proxy" };
if (kind === "site") {
  $("host").textContent = q.get("host") || "?";
  $("yes").textContent = "Autoriser ce site";
} else {
  const ua = q.get("agent") || "";
  $("agent").textContent = software(ua);
  if (ua) { const s = document.createElement("span"); s.className = "dim"; s.textContent = ua; $("agent").append(s); }
  $("key").textContent = q.get("key") || "?";
  const ep = q.get("endpoint") || "";
  $("endpoint").textContent = (ROUTES[ep] || ep || "?") + (q.get("conversation") ? ", une conversation" : "");
  $("model").textContent = q.get("model") || "—";
  $("yes").textContent = "Autoriser cette session";
}
const answer = (allow) => chrome.runtime.sendMessage({ type: "consent", id: q.get("id"), allow });
$("yes").addEventListener("click", () => answer(true));
$("no").addEventListener("click", () => answer(false));
let left = 60;
const tick = () => { $("left").textContent = `refus automatique dans ${left} s`; if (left-- > 0) setTimeout(tick, 1000); };
tick();
$("no").focus();
