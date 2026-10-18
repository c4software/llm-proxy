/*
  Le SERVICE WORKER de l'extension : le lien avec le proxy, et l'exécution
  des actions de l'outil hébergé `browser` (proxy/llm_proxy/tools/browser.py).

  LIEN : une WebSocket vers <proxy>/v1/browser/link, présentée par une clé
  du proxy (options.html) — protocole décrit dans browser_link.py. Un ping
  toutes les 20 s la garde ouverte (et le service worker éveillé) ; une
  alarme la rouvre si Chrome a endormi le worker, ou si le proxy a
  redémarré. Une clé refusée, ou une connexion remplacée par une autre (un
  second navigateur du même compte), ne se rouvre pas d'elle-même.

  CE QUE L'IA PEUT FAIRE, et rien d'autre :
    - elle travaille dans SES onglets : ceux qu'elle a ouverts (une fenêtre
      à elle), et ceux que l'utilisateur lui confie (popup.html) ; les
      autres onglets lui sont invisibles ;
    - sur un site (un nom d'hôte) que l'utilisateur a AUTORISÉ : la première
      action sur un site ouvre une fenêtre de demande (consent.html), une
      minute au plus ; l'accord est gardé, et se retire dans le popup ;
    - tant que l'utilisateur ne l'a pas ARRÊTÉE (bouton du bandeau de ses
      onglets, ou du popup).

  Stockage : `chrome.storage.local` — settings {url, key}, allowed {hôte:
  date}, stopped ; `chrome.storage.session` (oublié à la fermeture du
  navigateur) — window, tabs, last, status.
*/
const VERSION = chrome.runtime.getManifest().version;
const PING_MS = 20_000;
const CONSENT_MS = 60_000;
const LOAD_MS = 15_000;

// ── état ─────────────────────────────────────────────────────────────────
const local = (k) => chrome.storage.local.get(k).then((o) => o[k]);
const session = (k) => chrome.storage.session.get(k).then((o) => o[k]);
const setSession = (o) => chrome.storage.session.set(o);

// `extra` : ce qui accompagne un état durable (refusé, remplacé) — les
// réglages d'alors : il ne se lève qu'avec d'autres réglages.
async function setStatus(state, detail = "", extra = {}) {
  await setSession({ status: { state, detail, at: Date.now(), ...extra } });
  const badge = { on: ["", "#2f9e44"], connecting: ["…", "#888"], off: ["!", "#888"],
                  denied: ["!", "#c92a2a"], replaced: ["!", "#e8590c"], unset: ["?", "#888"] }[state] || ["", "#888"];
  const stopped = await local("stopped");
  chrome.action.setBadgeText({ text: stopped && state === "on" ? "II" : badge[0] });
  chrome.action.setBadgeBackgroundColor({ color: stopped ? "#e8590c" : badge[1] });
}

// ── le lien ──────────────────────────────────────────────────────────────
let ws = null;
let pinger = null;
let retry = 1000;
let retryTimer = null;

function wsUrl(base) {
  const u = new URL(base);
  u.protocol = u.protocol === "https:" ? "wss:" : "ws:";
  u.pathname = u.pathname.replace(/\/(ui\/?)?$/, "") + "/v1/browser/link";
  u.search = u.hash = "";
  return u.href;
}

async function connect() {
  if (ws && ws.readyState <= 1) return;
  clearTimeout(retryTimer);
  const s = (await local("settings")) || {};
  if (!s.url) { await setStatus("unset", "adresse du proxy à régler (Options)"); return; }
  const st = (await session("status")) || {};
  if (["denied", "replaced"].includes(st.state) && st.url === s.url && st.key === s.key) return;
  let sock;
  try { sock = new WebSocket(wsUrl(s.url)); } catch (e) { await setStatus("off", e.message); return; }
  ws = sock;
  await setStatus("connecting");
  sock.onopen = () => sock.send(JSON.stringify({ type: "hello", key: s.key || "", version: VERSION, agent: navigator.userAgent }));
  sock.onmessage = (e) => onMessage(sock, e.data);
  sock.onclose = async (e) => {
    if (ws === sock) ws = null;
    clearInterval(pinger);
    if (e.code === 4401) {
      await setStatus("denied", "clé refusée par le proxy", { url: s.url, key: s.key });
    } else if (e.code === 4409) {
      await setStatus("replaced", "un autre navigateur s'est connecté avec ce compte", { url: s.url, key: s.key });
    } else {
      await setStatus("off", "proxy injoignable — nouvel essai dans " + Math.round(retry / 1000) + " s");
      retryTimer = setTimeout(connect, retry);
      retry = Math.min(retry * 2, 30_000);
    }
  };
}

function reconnect() {
  if (ws) { const old = ws; ws = null; old.onclose = null; try { old.close(); } catch { /* fermée */ } }
  clearInterval(pinger);
  retry = 1000;
  setSession({ status: { state: "off", at: Date.now() } }).then(connect);
}

async function onMessage(sock, data) {
  let msg;
  try { msg = JSON.parse(data); } catch { return; }
  if (msg.type === "welcome") {
    retry = 1000;
    clearInterval(pinger);
    pinger = setInterval(() => { if (sock.readyState === 1) sock.send('{"type":"ping"}'); }, PING_MS);
    await setStatus("on");
  } else if (msg.type === "call") {
    let reply;
    try { reply = await handle(msg.op, msg.args || {}); } catch (e) { reply = fail("failed", e.message || String(e)); }
    if (sock.readyState === 1) sock.send(JSON.stringify({ type: "result", id: msg.id, ...reply }));
  }
}

chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
chrome.alarms.create("link", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((a) => { if (a.name === "link") connect(); });
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.settings) reconnect();
  if (area === "local" && changes.stopped) session("status").then((s) => setStatus((s && s.state) || "off", (s && s.detail) || "", s || {}));
});
connect();

// ── les onglets de l'IA ──────────────────────────────────────────────────
const fail = (code, error) => ({ ok: false, code, error });
const hostOf = (url) => { try { return new URL(url).hostname; } catch { return ""; } };
const web = (url) => /^https?:\/\//i.test(url || "");

async function aiTabs() { return (await session("tabs")) || []; }
async function addTab(id) {
  const tabs = await aiTabs();
  if (!tabs.includes(id)) await setSession({ tabs: [...tabs, id] });
  await setSession({ last: id });
}
chrome.tabs.onRemoved.addListener(async (id) => {
  const tabs = await aiTabs();
  if (tabs.includes(id)) await setSession({ tabs: tabs.filter((t) => t !== id) });
});

// L'onglet visé : celui donné (s'il est à l'IA), sinon le dernier utilisé.
async function target(args) {
  const tabs = await aiTabs();
  const id = args.tab !== undefined && args.tab !== null ? Number(args.tab) : await session("last");
  if (!id || !tabs.includes(id)) {
    throw Object.assign(new Error(args.tab ? `tab ${args.tab} is not one of your tabs; use action "tabs"`
      : "you have no tab yet: open a URL first"), { code: "invalid_input" });
  }
  const tab = await chrome.tabs.get(id).catch(() => null);
  if (!tab) throw Object.assign(new Error(`tab ${id} was closed`), { code: "not_accessible" });
  await setSession({ last: id });
  return tab;
}

async function openTab(url) {
  const win = await session("window");
  const alive = win && await chrome.windows.get(win).catch(() => null);
  if (alive) {
    const tab = await chrome.tabs.create({ windowId: win, url, active: true });
    await addTab(tab.id);
    return tab;
  }
  const w = await chrome.windows.create({ url, focused: false, width: 1280, height: 900 });
  await setSession({ window: w.id });
  const tab = w.tabs[0];
  await addTab(tab.id);
  return tab;
}

// La page chargée (ou 15 s), puis un court répit pour ses scripts.
async function loaded(id) {
  const until = Date.now() + LOAD_MS;
  await new Promise((r) => setTimeout(r, 150));
  while (Date.now() < until) {
    const t = await chrome.tabs.get(id).catch(() => null);
    if (!t) throw Object.assign(new Error("the tab was closed"), { code: "not_accessible" });
    if (t.status === "complete") break;
    await new Promise((r) => setTimeout(r, 150));
  }
  await new Promise((r) => setTimeout(r, 300));
  return chrome.tabs.get(id);
}

// ── les sites autorisés ──────────────────────────────────────────────────
const asking = new Map();            // hôte → promesse de réponse
const refused = new Map();           // hôte → date du refus (cette session)

async function allowed(host) { return !!((await local("allowed")) || {})[host]; }

async function consent(url) {
  if (!web(url)) throw Object.assign(new Error("only http(s) pages can be used"), { code: "not_allowed" });
  const host = hostOf(url);
  if (await allowed(host)) return;
  if (refused.has(host) && Date.now() - refused.get(host) < 10 * 60_000) {
    throw Object.assign(new Error(`the user refused access to ${host}; do not retry, ask them`), { code: "not_allowed" });
  }
  if (!asking.has(host)) asking.set(host, ask(host).finally(() => asking.delete(host)));
  const yes = await asking.get(host);
  if (!yes) {
    refused.set(host, Date.now());
    throw Object.assign(new Error(`the user did not allow ${host}; do not retry, ask them`), { code: "not_allowed" });
  }
}

async function ask(host) {
  const id = crypto.randomUUID();
  const s = (await local("settings")) || {};
  const w = await chrome.windows.create({
    url: `consent.html?host=${encodeURIComponent(host)}&id=${id}&proxy=${encodeURIComponent(s.url || "")}`,
    type: "popup", width: 480, height: 340, focused: true });
  return new Promise((resolve) => {
    const done = (v) => {
      clearTimeout(timer);
      chrome.runtime.onMessage.removeListener(onMsg);
      chrome.windows.onRemoved.removeListener(onClosed);
      chrome.windows.remove(w.id).catch(() => {});
      resolve(v);
    };
    const timer = setTimeout(() => done(false), CONSENT_MS);
    function onMsg(m) {
      if (m && m.type === "consent" && m.id === id) {
        if (m.allow) {
          local("allowed").then((a) => chrome.storage.local.set({ allowed: { ...(a || {}), [host]: Date.now() } })).then(() => done(true));
        } else done(false);
      }
    }
    function onClosed(winId) { if (winId === w.id) done(false); }
    chrome.runtime.onMessage.addListener(onMsg);
    chrome.windows.onRemoved.addListener(onClosed);
  });
}

// ── dans la page ─────────────────────────────────────────────────────────
async function page(tabId, op, args = {}) {
  try {
    await chrome.scripting.executeScript({ target: { tabId }, files: ["page.js"] });
    const run = (o, a) => globalThis.__llmProxyAgent(o, a);
    if (op !== "banner") await chrome.scripting.executeScript({ target: { tabId }, func: run, args: ["banner", { on: true }] });
    const [r] = await chrome.scripting.executeScript({ target: { tabId }, func: run, args: [op, args] });
    const out = r && r.result;
    if (!out || out.error) throw Object.assign(new Error((out && out.error) || "the page did not answer"), { code: "failed" });
    return out;
  } catch (e) {
    if (e.code) throw e;
    throw Object.assign(new Error("cannot act in this page (" + e.message + ")"), { code: "not_accessible" });
  }
}

async function readTab(tab, maxChars) {
  const r = await page(tab.id, "read", { maxChars: Math.min(Math.max(Number(maxChars) || 12000, 1000), 60000) });
  return { ok: true, url: r.url, title: r.title,
           text: `[tab ${tab.id}] ${r.title}\n${r.url}\n(${r.where}; ${r.refs} refs)\n\n${r.text}` };
}

// Après une action : la page a pu changer d'adresse (lien, formulaire) ;
// si c'est le cas, elle est relue (ses repères ont changé).
async function after(tab, done, before) {
  await new Promise((r) => setTimeout(r, 400));
  const now = await loaded(tab.id);
  if (now.url !== before) {
    if (!(await allowed(hostOf(now.url))) && web(now.url)) {
      return { ok: true, url: now.url, title: now.title,
               text: `${done}. The tab went to ${now.url}, on another site: reading it needs the user's permission (any next action asks).` };
    }
    const r = await readTab(now).catch(() => null);
    if (r) return { ...r, text: `${done}. The tab went to a new page:\n\n${r.text}` };
  }
  return { ok: true, url: now.url, title: now.title, text: `${done}. Tab ${tab.id}: ${now.title} — ${now.url}` };
}

// Un vrai clic, une vraie frappe : par le protocole des DevTools.
async function cdp(tabId, fn) {
  const t = { tabId };
  try { await chrome.debugger.attach(t, "1.3"); } catch (e) {
    throw Object.assign(new Error("real input events are unavailable here (" + e.message + "); try without trusted"), { code: "unavailable" });
  }
  try { return await fn((m, p) => chrome.debugger.sendCommand(t, m, p)); }
  finally { await chrome.debugger.detach(t).catch(() => {}); }
}
const ENTER = { key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13 };

// ── les actions ──────────────────────────────────────────────────────────
async function handle(op, args) {
  if (await local("stopped")) {
    return fail("not_allowed", "the user paused browser control in the extension; ask them to resume it");
  }
  try {
    return await act(op, args);
  } catch (e) {
    return fail(e.code || "failed", e.message || String(e));
  }
}

async function act(op, args) {
  if (op === "tabs") {
    const tabs = (await Promise.all((await aiTabs()).map((id) => chrome.tabs.get(id).catch(() => null)))).filter(Boolean);
    const last = await session("last");
    if (!tabs.length) return { ok: true, text: "You have no tab yet: open a URL." };
    return { ok: true, text: tabs.map((t) => `[tab ${t.id}] ${t.title} — ${t.url}${t.id === last ? " (current)" : ""}`).join("\n") };
  }
  if (op === "open") {
    const url = String(args.url || "");
    await consent(url);
    const tab = await openTab(url);
    return readTab(await loaded(tab.id), args.max_chars);
  }
  const tab = await target(args);
  if (op === "close") {
    await chrome.tabs.remove(tab.id);
    return { ok: true, text: `Tab ${tab.id} closed.` };
  }
  if (op === "navigate") {
    const url = String(args.url || "");
    await consent(url);
    await chrome.tabs.update(tab.id, { url });
    return readTab(await loaded(tab.id), args.max_chars);
  }
  await consent(tab.url);
  switch (op) {
    case "back":
      await chrome.tabs.goBack(tab.id).catch(() => { throw Object.assign(new Error("no previous page"), { code: "invalid_input" }); });
      return readTab(await loaded(tab.id), args.max_chars);
    case "read":
      return readTab(tab, args.max_chars);
    case "screenshot": {
      await chrome.tabs.update(tab.id, { active: true });
      await new Promise((r) => setTimeout(r, 250));
      const image = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "jpeg", quality: 70 });
      return { ok: true, url: tab.url, title: tab.title, image,
               text: `Screenshot of tab ${tab.id} (${tab.title}) taken; it is shown to the user as an image file. To understand the page yourself, use read.` };
    }
    case "click": {
      need(args, "ref");
      if (args.trusted) {
        const p = await page(tab.id, "point", { ref: args.ref });
        await cdp(tab.id, async (send) => {
          for (const type of ["mouseMoved", "mousePressed", "mouseReleased"]) {
            await send("Input.dispatchMouseEvent", { type, x: p.x, y: p.y, button: "left", clickCount: 1 });
          }
        });
        return after(tab, `Clicked [${args.ref}] ${p.done} (real click)`, tab.url);
      }
      const r = await page(tab.id, "click", { ref: args.ref });
      return after(tab, `Clicked [${args.ref}] ${r.done}`, tab.url);
    }
    case "type": {
      need(args, "ref");
      const clear = args.clear !== false;
      if (args.trusted) {
        const r = await page(tab.id, "focus", { ref: args.ref });
        await cdp(tab.id, async (send) => {
          if (!clear) await send("Input.dispatchKeyEvent", { type: "keyDown", key: "End", code: "End", windowsVirtualKeyCode: 35 });
          await send("Input.insertText", { text: String(args.text ?? "") });
          if (args.submit) {
            await send("Input.dispatchKeyEvent", { type: "keyDown", ...ENTER, text: "\r" });
            await send("Input.dispatchKeyEvent", { type: "keyUp", ...ENTER });
          }
        });
        return after(tab, `Typed into [${args.ref}] ${r.done} (real keystrokes)`, tab.url);
      }
      const r = await page(tab.id, "type", { ref: args.ref, text: args.text, clear, submit: !!args.submit });
      return after(tab, `Typed into [${args.ref}] ${r.done}`, tab.url);
    }
    case "select": {
      need(args, "ref");
      const r = await page(tab.id, "select", { ref: args.ref, value: String(args.value ?? "") });
      return after(tab, r.done, tab.url);
    }
    case "key": {
      const r = await page(tab.id, "key", { key: String(args.key || "") });
      return after(tab, r.done, tab.url);
    }
    case "scroll": {
      const r = await page(tab.id, "scroll", { direction: args.direction || "down", ref: args.ref });
      return { ok: true, url: tab.url, title: tab.title, text: `${r.done}. Read again to see what is now in view.` };
    }
    case "wait": {
      const r = await page(tab.id, "wait", { text: args.text, ms: args.ms });
      return after(tab, r.done, tab.url);
    }
    default:
      return fail("invalid_input", `unknown action ${op}`);
  }
}

function need(args, k) {
  if (args[k] === undefined || args[k] === null || args[k] === "") {
    throw Object.assign(new Error(`\`${k}\` is required for this action`), { code: "invalid_input" });
  }
}

// ── le popup, le bandeau, les options ────────────────────────────────────
chrome.runtime.onMessage.addListener((m, sender, reply) => {
  if (!m || typeof m !== "object") return;
  if (m.type === "stop") {
    chrome.storage.local.set({ stopped: true });
    // Le bandeau de chaque onglet de l'IA disparaît : plus rien n'y est piloté.
    aiTabs().then((tabs) => tabs.forEach((id) => chrome.scripting.executeScript({ target: { tabId: id },
      func: () => globalThis.__llmProxyAgent && globalThis.__llmProxyAgent("banner", { on: false }) }).catch(() => {})));
  } else if (m.type === "resume") {
    chrome.storage.local.set({ stopped: false });
  } else if (m.type === "reconnect") {
    reconnect();
  } else if (m.type === "share") {
    // L'utilisateur confie l'onglet actif à l'IA (son geste vaut accord pour le site).
    chrome.tabs.query({ active: true, lastFocusedWindow: true }).then(async ([tab]) => {
      if (!tab || !web(tab.url)) { reply({ error: "seule une page web (http, https) peut être confiée" }); return; }
      await addTab(tab.id);
      const a = (await local("allowed")) || {};
      await chrome.storage.local.set({ allowed: { ...a, [hostOf(tab.url)]: Date.now() } });
      reply({ ok: true, tab: tab.id });
    });
    return true;
  }
});
