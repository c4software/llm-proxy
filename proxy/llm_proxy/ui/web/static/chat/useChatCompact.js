// @ts-check
// Composable contexte et compactage du chat : jauge d'occupation, répartition
// par poste, résumé `/compact` (manuel ou automatique), annulation et
// autocomplétion des commandes « / ».
import { ref, computed, nextTick } from "vue";
import { useLocalStorage } from "@vueuse/core";
import { inline } from "./history.js";
import { readStream } from "./stream.js";
import { modelCaps } from "./browser.js";
import { num } from "../lib/format.js";
import * as Compact from "./compact.js";
import { apiMessages } from "./api.js";

const COMMANDS = [{
  name: "/compact", args: "[consigne]",
  hint: "résumer la conversation pour libérer du contexte (les messages restent affichés)",
}];

export function useChatCompact({
  API, current, genMode, modelEntry, requested, streaming, status, draft,
  input, uid, startRun, clearController, respond, onUnauthorized,
}) {
  const autoCompact = useLocalStorage("llm-proxy-auto-compact", true);
  const ctxOpen = ref(false);
  const contextMax = (conv) => { const e = conv && modelEntry(conv.model); return (e && e.context) || 0; };

  const context = computed(() => {
    const c = current.value;
    if (!c || !c.messages.length || genMode.value) return null;
    return Compact.occupancy(c, contextMax(c));
  });

  const ctxParts = computed(() => {
    const c = current.value, o = context.value, r = requested.value;
    if (!c || !o || !ctxOpen.value) return null;
    const parts = Compact.breakdown(c, o.metered ? o.tokens : 0, {
      guidance: r ? r.guidance : "",
      tools: r ? JSON.stringify(r.specs.map((d) => d.spec)) : "",
    });
    const used = parts.reduce((n, p) => n + p.tokens, 0);
    const scale = Math.max(o.max || 0, used) || 1;
    const free = o.max ? Math.max(0, o.max - used) : 0;
    return { parts: parts.map((p) => ({ ...p, pct: (p.tokens / scale) * 100 })), free };
  });

  const sentFrom = computed(() => (current.value ? Compact.start(current.value, current.value.messages.length) : 0));
  const contextNear = (conv) => {
    const o = Compact.occupancy(conv, contextMax(conv));
    return !!o.max && o.ratio >= Compact.AUTO_AT
      && conv.messages.slice(Compact.start(conv, conv.messages.length)).some((m) => m.role === "assistant");
  };

  const contextTitle = computed(() => {
    const o = context.value;
    if (!o) return "";
    const what = (o.measured ? "" : "≈ ") + num(o.tokens) + " tokens"
      + (o.max ? ` sur ${num(o.max)} (${Math.round(o.ratio * 100)} %)` : " (fenêtre du modèle inconnue)");
    return `Contexte : ${what} — /compact pour résumer la conversation`;
  });

  const tooLong = (m) => /context.{0,40}(length|window|size|limit)|contexte|too long|too many tokens|maximum.*(length|tokens)|exceeds?.*(context|tokens)/i.test(m.error || "");

  async function compact(conv, focus = "", auto = false, upto = conv.messages.length) {
    if (streaming.value) return false;
    const from = Compact.start(conv, upto);
    const covered = conv.messages.slice(from, upto).filter((m) => m.role !== "summary").length;
    if (!conv.messages.slice(from, upto).some((m) => m.role === "assistant")) {
      status.value = "Rien à résumer depuis le dernier résumé.";
      return false;
    }
    const before = Compact.occupancy({ ...conv, messages: conv.messages.slice(0, upto) }, contextMax(conv)).tokens;
    conv.messages.splice(upto, 0, {
      id: uid(), role: "summary", content: "", reasoning: "", tools: [],
      sources: [], model: conv.model, usage: null, error: "", started: Date.now(), duration: 0,
      running: true, auto, focus, covered, before,
    });
    const s = conv.messages[upto];
    const ctrl = startRun(auto ? "Contexte presque plein : résumé de la conversation…" : "Résumé de la conversation…");
    try {
      const msgs = await inline(apiMessages(conv, upto));
      const entry = modelEntry(conv.model);
      const once = async (list) => {
        if (entry && entry.answer) {
          await entry.answer(list, s, ctrl.signal, (t) => { status.value = t; });
          return;
        }
        const body = { model: conv.model, stream: true, stream_options: { include_usage: true }, messages: list };
        if (modelCaps(entry).reasoning) body.chat_template_kwargs = { enable_thinking: false };
        const r = await fetch(API, {
          method: "POST", signal: ctrl.signal,
          headers: {
            "content-type": "application/json", accept: "text/event-stream",
            ...(conv.incognito ? { "x-incognito": "1" } : {}),
          },
          body: JSON.stringify(body),
        });
        if (r.status === 401) { onUnauthorized(); throw new Error("session expirée"); }
        if (!r.ok) {
          let j = null;
          try { j = await r.json(); } catch { /* corps illisible */ }
          throw new Error((j && j.error && (j.error.message || j.error)) || `HTTP ${r.status}`);
        }
        if ((r.headers.get("content-type") || "").includes("event-stream")) await readStream(r.body, s);
        else { const j = await r.json(); s.content = (((j.choices || [])[0] || {}).message || {}).content || ""; s.usage = j.usage || null; }
      };
      try {
        await once(Compact.request(msgs, focus));
      } catch (e) {
        if (e.name === "AbortError" || !tooLong({ error: e.message })) throw e;
        s.light = true;
        status.value = "Contexte trop long pour le résumer tel quel : résumé d'une version allégée…";
        await once(Compact.request(Compact.lighten(msgs), focus));
      }
      s.content = (s.content || "").trim();
      if (!s.error && !s.content) s.error = "le modèle n'a rien rendu";
    } catch (e) {
      s.error = e.name === "AbortError" ? "arrêté" : e.message;
    } finally {
      s.running = false;
      s.reasoning = "";
      s.duration = (Date.now() - s.started) / 1000;
      streaming.value = false;
      clearController();
      conv.updated = Date.now();
    }
    if (s.error) {
      conv.messages.splice(conv.messages.indexOf(s), 1);
      status.value = "Résumé impossible : " + s.error + (auto ? " — l'historique part en entier." : ".");
      return false;
    }
    status.value = "";
    return true;
  }

  function compactNow() {
    ctxOpen.value = false;
    const c = current.value;
    if (c && !streaming.value) compact(c);
  }

  function uncompact(i) {
    const c = current.value;
    if (!c || streaming.value || !c.messages[i] || c.messages[i].role !== "summary") return;
    c.messages.splice(i, 1);
  }

  async function retryCompacted() {
    const c = current.value;
    if (!c || streaming.value) return;
    const last = c.messages[c.messages.length - 1];
    if (last && last.role === "assistant") c.messages.pop();
    const q = c.messages.length - 1;
    if (q < 0 || c.messages[q].role !== "user") return;
    if (await compact(c, "", true, q)) respond(c);
  }

  const commandHints = computed(() => {
    const d = draft.value;
    if (!d.startsWith("/") || /\s/.test(d) || genMode.value) return [];
    return COMMANDS.filter((c) => c.name.startsWith(d.toLowerCase()));
  });

  function useCommand(c) {
    draft.value = c.name + " ";
    nextTick(() => { if (input.value) input.value.focus(); });
  }

  return {
    autoCompact, ctxOpen, contextMax, context, ctxParts, sentFrom, contextNear,
    contextTitle, tooLong, compact, compactNow, uncompact, retryCompacted,
    commandHints, useCommand,
  };
}
