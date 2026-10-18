/*
  Le script de l'extension DANS un onglet de l'IA (injecté à la demande par
  background.js, monde isolé : la page ne le voit pas, sa politique de
  contenu ne le bloque pas).

    read({maxChars})        la page en texte : un élément utile par ligne,
                            avec un REPÈRE [n] (lien, bouton, champ, liste,
                            case, élément cliquable) — les repères valent
                            jusqu'à la lecture suivante
    click({ref})            cliquer (événements de script)
    point({ref})            le centre de l'élément, à l'écran (un vrai clic,
                            par chrome.debugger, background.js)
    type({ref, text, clear, submit})
    focus({ref})
    select({ref, value})
    key({key})
    scroll({direction, ref})
    wait({text, ms})
    banner({on})            le bandeau « piloté par l'IA », avec Arrêter

  Injecté plusieurs fois (une page rechargée, un second appel) : il ne
  s'installe qu'une fois par document.
*/
(() => {
  if (globalThis.__llmProxyAgent) return;

  const MAX_TEXT = 140;            // caractères d'un texte d'élément
  let refs = new Map();            // repère → élément (dernière lecture)

  const clean = (s) => String(s || "").replace(/\s+/g, " ").trim();
  const cut = (s, n = MAX_TEXT) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

  function visible(el) {
    if (!(el instanceof Element)) return false;
    const css = getComputedStyle(el);
    if (css.display === "none" || css.visibility === "hidden" || css.opacity === "0") return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 || r.height > 0 || el.tagName === "OPTION";
  }

  const INTERACTIVE = "a[href], button, input:not([type=hidden]), select, textarea, summary, "
    + "[role=button], [role=link], [role=checkbox], [role=radio], [role=tab], [role=menuitem], "
    + "[role=option], [role=switch], [role=combobox], [contenteditable=''], [contenteditable=true], [onclick]";

  // Le nom d'un champ : son label, aria-label, placeholder, name.
  function fieldName(el) {
    const byId = el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    const wrap = el.closest("label");
    return clean(el.getAttribute("aria-label") || (byId && byId.innerText) || (wrap && wrap.innerText)
      || el.getAttribute("placeholder") || el.getAttribute("title") || el.name || "");
  }

  function describe(el) {
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute("role");
    const text = cut(clean(el.innerText || el.value || el.getAttribute("aria-label") || el.title || ""));
    if (tag === "a") {
      let href = el.getAttribute("href") || "";
      try { href = new URL(href, location.href).href; } catch { /* telle quelle */ }
      if (href.startsWith(location.origin)) href = href.slice(location.origin.length) || "/";
      return `link "${text || cut(href, 60)}" → ${cut(href, 100)}`;
    }
    if (tag === "input") {
      const type = (el.getAttribute("type") || "text").toLowerCase();
      const name = cut(fieldName(el), 80);
      if (["checkbox", "radio"].includes(type)) return `${type} "${name}"${el.checked ? " [checked]" : ""}`;
      if (["submit", "button", "reset"].includes(type)) return `button "${cut(clean(el.value), 80)}"`;
      const value = type === "password" ? (el.value ? "••••" : "") : cut(clean(el.value), 80);
      return `${type} field "${name}"${value ? ` = "${value}"` : ""}${el.disabled ? " [disabled]" : ""}${el.required ? " [required]" : ""}`;
    }
    if (tag === "textarea") return `textarea "${cut(fieldName(el), 80)}"${el.value ? ` = "${cut(clean(el.value), 120)}"` : ""}`;
    if (tag === "select") {
      const opts = [...el.options].slice(0, 15).map((o) => (o.selected ? `*${clean(o.text)}*` : clean(o.text)));
      return `select "${cut(fieldName(el), 60)}": ${opts.join(" | ")}${el.options.length > 15 ? " | …" : ""}`;
    }
    if (el.isContentEditable) return `editable "${cut(fieldName(el) || text, 80)}"`;
    if (tag === "button" || role === "button") return `button "${text || cut(fieldName(el), 60)}"${el.disabled ? " [disabled]" : ""}`;
    if (role) return `${role} "${text}"${el.getAttribute("aria-checked") === "true" || el.getAttribute("aria-selected") === "true" ? " [selected]" : ""}`;
    return `clickable "${text}"`;
  }

  // ── lire ──────────────────────────────────────────────────────────────
  function read({ maxChars = 12000 } = {}) {
    refs = new Map();
    let n = 0;
    const lines = [];
    let chars = 0;
    let cutAt = 0;
    let bullet = false;              // la prochaine ligne : un élément de liste
    const push = (line) => {
      if (chars > maxChars) { cutAt++; return; }
      if (bullet) { line = line.replace(/^(\s*)/, "$1• "); bullet = false; }
      lines.push(line);
      chars += line.length + 1;
    };
    const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "SVG", "HEAD", "META", "LINK"]);
    function walk(node, depth) {
      if (node.nodeType === Node.TEXT_NODE) {
        const t = clean(node.textContent);
        if (t) push("  ".repeat(depth) + cut(t, 300));
        return;
      }
      if (node.nodeType !== Node.ELEMENT_NODE) return;
      const el = /** @type {Element} */ (node);
      if (SKIP.has(el.tagName) || el.id === "__llm-proxy-banner") return;
      if (!visible(el)) return;
      if (el.matches(INTERACTIVE)) {
        const ref = ++n;
        refs.set(ref, el);
        push("  ".repeat(depth) + `[${ref}] ${describe(el)}`);
        // Le texte d'un lien, d'un bouton, les options d'une liste sont
        // déjà dans sa ligne.
        if (el.tagName === "SELECT" || (!el.isContentEditable && !el.querySelector(INTERACTIVE))) return;
      } else if (/^H[1-6]$/.test(el.tagName)) {
        push("  ".repeat(depth) + "#".repeat(Number(el.tagName[1])) + " " + cut(clean(el.innerText), 200));
        return;
      } else if (el.tagName === "IMG") {
        const alt = clean(el.getAttribute("alt"));
        if (alt) push("  ".repeat(depth) + `[image "${cut(alt, 100)}"]`);
        return;
      } else if (el.tagName === "IFRAME") {
        push("  ".repeat(depth) + `[frame ${cut(el.getAttribute("src") || "", 80)}]`);
        return;
      }
      const deeper = ["UL", "OL", "TABLE", "FORM", "NAV", "DIALOG", "FIELDSET"].includes(el.tagName) ? depth + 1 : depth;
      if (el.tagName === "DIALOG" || el.getAttribute("role") === "dialog") push("  ".repeat(depth) + "[dialog]");
      if (el.tagName === "LI") bullet = true;
      // Le texte d'un libellé de champ est déjà le nom du champ.
      const label = el.tagName === "LABEL" && el.control;
      const root = el.shadowRoot || el;
      for (const c of root.childNodes) if (!(label && c.nodeType === Node.TEXT_NODE)) walk(c, deeper);
    }
    walk(document.body || document.documentElement, 0);
    const out = lines;
    const se = document.scrollingElement || document.documentElement;
    const where = `scrolled ${Math.round(se.scrollTop)} of ${Math.max(0, Math.round(se.scrollHeight - innerHeight))} px`;
    return {
      title: document.title, url: location.href, refs: n,
      text: out.join("\n") + (cutAt ? `\n… (cut: ${cutAt} more lines; raise max_chars, or scroll and read again)` : ""),
      where,
    };
  }

  function get(ref) {
    const el = refs.get(Number(ref));
    if (!el) throw new Error(`no element [${ref}] in the latest read of this tab; read the page again`);
    if (!el.isConnected) throw new Error(`element [${ref}] is gone (the page changed); read the page again`);
    return el;
  }

  // ── agir ──────────────────────────────────────────────────────────────
  function click({ ref }) {
    const el = get(ref);
    el.scrollIntoView({ block: "center", inline: "center" });
    const r = el.getBoundingClientRect();
    const at = { bubbles: true, cancelable: true, composed: true, view: window,
                 clientX: r.left + r.width / 2, clientY: r.top + r.height / 2, button: 0 };
    el.dispatchEvent(new PointerEvent("pointerdown", at));
    el.dispatchEvent(new MouseEvent("mousedown", at));
    if (el.focus) el.focus({ preventScroll: true });
    el.dispatchEvent(new PointerEvent("pointerup", at));
    el.dispatchEvent(new MouseEvent("mouseup", at));
    el.click();
    return { done: describe(el) };
  }

  function point({ ref }) {
    const el = get(ref);
    el.scrollIntoView({ block: "center", inline: "center" });
    const r = el.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2, done: describe(el) };
  }

  function focus({ ref }) {
    const el = get(ref);
    el.scrollIntoView({ block: "center" });
    el.focus();
    if (el.select && (el.tagName === "INPUT" || el.tagName === "TEXTAREA")) el.select();
    return { done: describe(el) };
  }

  // La valeur d'un champ par le setter NATIF : React et consorts suivent
  // ainsi le changement (un `el.value =` direct leur échappe).
  function setValue(el, value) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype
      : el.tagName === "SELECT" ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
    el.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
  }

  function enter(el) {
    const at = { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true, composed: true };
    const go = el.dispatchEvent(new KeyboardEvent("keydown", at));
    el.dispatchEvent(new KeyboardEvent("keypress", at));
    el.dispatchEvent(new KeyboardEvent("keyup", at));
    // Un Enter de script ne soumet pas le formulaire : on le fait.
    if (go && el.form && el.tagName === "INPUT") {
      if (el.form.requestSubmit) el.form.requestSubmit(); else el.form.submit();
    }
  }

  function type({ ref, text, clear = true, submit = false }) {
    const el = get(ref);
    el.scrollIntoView({ block: "center" });
    el.focus();
    const t = String(text ?? "");
    if (el.isContentEditable) {
      if (clear) document.execCommand("selectAll", false);
      document.execCommand("insertText", false, t);
    } else if ("value" in el) {
      setValue(el, clear ? t : el.value + t);
    } else {
      throw new Error(`element [${ref}] is not a text field`);
    }
    if (submit) enter(el);
    return { done: describe(el) };
  }

  function select({ ref, value }) {
    const el = get(ref);
    if (el.tagName !== "SELECT") throw new Error(`element [${ref}] is not a list (select)`);
    const want = clean(value).toLowerCase();
    const opt = [...el.options].find((o) => o.value === value) || [...el.options].find((o) => clean(o.text).toLowerCase() === want)
      || [...el.options].find((o) => clean(o.text).toLowerCase().includes(want));
    if (!opt) throw new Error(`no option "${value}" in [${ref}]; options: ${[...el.options].map((o) => clean(o.text)).join(" | ")}`);
    setValue(el, opt.value);
    return { done: `selected "${clean(opt.text)}" in ${describe(el)}` };
  }

  function key({ key: k }) {
    const el = document.activeElement || document.body;
    const name = String(k || "");
    if (name === "Enter") { enter(el); return { done: `pressed Enter on ${el === document.body ? "the page" : describe(el)}` }; }
    const at = { key: name, code: name.length === 1 ? "Key" + name.toUpperCase() : name, bubbles: true, cancelable: true, composed: true };
    el.dispatchEvent(new KeyboardEvent("keydown", at));
    if (name.length === 1 && "value" in el) setValue(el, el.value + name);
    el.dispatchEvent(new KeyboardEvent("keyup", at));
    return { done: `pressed ${name} on ${el === document.body ? "the page" : describe(el)}` };
  }

  function scroll({ direction = "down", ref }) {
    if (ref !== undefined && ref !== null) {
      const el = get(ref);
      el.scrollIntoView({ block: "center" });
      return { done: `scrolled to ${describe(el)}` };
    }
    const se = document.scrollingElement || document.documentElement;
    if (direction === "top") se.scrollTop = 0;
    else if (direction === "bottom") se.scrollTop = se.scrollHeight;
    else se.scrollBy(0, (direction === "up" ? -1 : 1) * innerHeight * 0.85);
    return { done: `scrolled ${direction}: ${Math.round(se.scrollTop)} of ${Math.max(0, Math.round(se.scrollHeight - innerHeight))} px` };
  }

  async function wait({ text, ms }) {
    if (!text) {
      await new Promise((r) => setTimeout(r, Math.min(Number(ms) || 1000, 10000)));
      return { done: `waited ${Math.min(Number(ms) || 1000, 10000)} ms` };
    }
    const until = Date.now() + Math.min(Number(ms) || 10000, 10000);
    while (Date.now() < until) {
      if ((document.body ? document.body.innerText : "").includes(text)) return { done: `"${text}" is shown` };
      await new Promise((r) => setTimeout(r, 200));
    }
    throw new Error(`"${text}" did not appear within ${Math.round(Math.min(Number(ms) || 10000, 10000) / 1000)} s`);
  }

  // ── le bandeau ────────────────────────────────────────────────────────
  function banner({ on }) {
    let host = document.getElementById("__llm-proxy-banner");
    if (!on) { if (host) host.remove(); return { done: "bandeau retiré" }; }
    if (host) return { done: "bandeau" };
    host = document.createElement("div");
    host.id = "__llm-proxy-banner";
    host.style.cssText = "position:fixed;z-index:2147483647;left:50%;bottom:12px;transform:translateX(-50%);";
    const root = host.attachShadow({ mode: "closed" });
    root.innerHTML = `<style>
      div{display:flex;align-items:center;gap:10px;padding:6px 8px 6px 12px;border-radius:999px;
        background:#1d1c1a;color:#fff;font:13px/1.3 system-ui,sans-serif;box-shadow:0 6px 20px rgba(0,0,0,.35)}
      i{width:8px;height:8px;border-radius:50%;background:#d96b47;animation:p 1.4s infinite}
      @keyframes p{50%{opacity:.35}}
      button{border:0;border-radius:999px;padding:4px 10px;background:#d96b47;color:#fff;font:inherit;cursor:pointer}
    </style><div><i></i>Piloté par l'IA (llm-proxy)<button type="button">Arrêter</button></div>`;
    root.querySelector("button").addEventListener("click", () => {
      chrome.runtime.sendMessage({ type: "stop" });
    });
    document.documentElement.appendChild(host);
    return { done: "bandeau" };
  }

  const OPS = { read, click, point, focus, type, select, key, scroll, wait, banner };
  globalThis.__llmProxyAgent = async (op, args) => {
    const f = OPS[op];
    if (!f) return { error: `unknown page operation ${op}` };
    try {
      return { ok: true, ...(await f(args || {})) };
    } catch (e) {
      return { error: String(e && e.message || e) };
    }
  };
})();
