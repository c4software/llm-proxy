import { when } from "../lib/format.js";
import * as history from "./history.js";

// Les CONVERSATIONS : un magasin partagé, lu par le chat (ChatPanel) ET par
// le menu latéral de la page (app.js), qui en affiche la liste. Gardées
// par le proxy, une liste par compte connecté (chat/history.js).
export const ChatStore = (() => {
  const { reactive, computed, watch, toRaw } = Vue;
  const state = reactive({
    key: "", convs: [], currentId: null,
    loading: false,                 // l'historique arrive du proxy
    streaming: false,               // une réponse en cours : pas de changement
    confirming: null,               // id de la conversation à supprimer
    renaming: null, renameText: "",
    storageWarning: "",
    query: "",                      // la recherche du menu latéral
  });
  // Ce qu'une conversation neuve reçoit (modèle, outils) : posé par le chat,
  // qui connaît le catalogue et la session.
  let defaults = () => ({ model: "", tools: [] });
  const uid = () => Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
  const sorted = computed(() => [...state.convs].sort((a, b) => b.updated - a.updated));
  const current = computed(() => state.convs.find((c) => c.id === state.currentId) || null);
  // La recherche : chaque mot, sans casse ni accents, dans le titre ou
  // le texte d'un message. La conversation affichée reste listée.
  const fold = (t) => String(t || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
  const filtered = computed(() => {
    const words = fold(state.query).split(/\s+/).filter(Boolean);
    if (!words.length) return sorted.value;
    return sorted.value.filter((c) => {
      if (c.id === state.currentId) return true;
      const text = fold(c.title + "\n" + c.messages.map((m) => m.content).join("\n"));
      return words.every((w) => text.includes(w));
    });
  });

  // L'historique du COMPTE connecté, gardé par le proxy (chat/history.js).
  // Rien n'est relu si c'est déjà le sien. `ready` : la liste est là.
  let ready = Promise.resolve();
  const saved = new Map();          // id → le JSON envoyé en dernier
  function open(client) {
    const key = client || "open";
    if (state.key === key) return ready;
    state.key = key;
    state.loading = true;
    saved.clear();
    ready = history.load().then((list) => {
      if (state.key !== key) return;
      list = list.filter((c) => c && c.id && Array.isArray(c.messages));
      for (const c of list) saved.set(c.id, JSON.stringify(c));
      // Une conversation commencée pendant le chargement reste en tête.
      state.convs = [...state.convs.filter((c) => !saved.has(c.id)), ...list];
      if (!state.currentId && state.convs[0]) state.currentId = state.convs[0].id;
      state.storageWarning = "";
    }).catch((e) => {
      state.storageWarning = "Historique indisponible : " + e.message;
    }).finally(() => { state.loading = false; });
    return ready;
  }

  // Enregistrement différé : un flux modifie le message des dizaines de
  // fois par seconde, le proxy n'a besoin que d'un état de temps en
  // temps. Seules partent les conversations qui ont changé — jamais une
  // conversation incognito, ni une conversation encore vide.
  let saveTimer = null, running = null, again = false;
  const kept = (c) => !c.incognito && c.messages.length;
  async function persist() {
    saveTimer = null;
    if (running) { again = true; return running; }
    running = (async () => {
      for (const c of toRaw(state.convs)) {
        if (!kept(c) || saved.get(c.id) === JSON.stringify(c)) continue;
        try {
          await history.save(c);
          saved.set(c.id, JSON.stringify(c));
          state.storageWarning = "";
        } catch (e) {
          state.storageWarning = `« ${c.title} » n'est pas enregistrée : ${e.message}`;
        }
      }
    })();
    try { await running; } finally { running = null; }
    if (again) { again = false; return persist(); }
  }
  watch(() => state.convs, () => {
    if (!saveTimer) saveTimer = setTimeout(persist, 1000);
  }, { deep: true });
  // La page quittée ou cachée : ce qui reste part tout de suite.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden" && saveTimer) {
      clearTimeout(saveTimer);
      persist();
    }
  });

  function newConv(incognito = false) {
    if (state.streaming) return;
    // Une conversation vide (du même genre) est réutilisée plutôt que
    // dupliquée.
    const empty = state.convs.find((c) => !c.messages.length && !!c.incognito === incognito);
    if (empty) { state.currentId = empty.id; return; }
    const base = current.value;
    const fresh = defaults();
    const conv = {
      id: uid(), title: "Nouvelle conversation", created: Date.now(),
      updated: Date.now(), model: base ? base.model : fresh.model,
      system: base ? base.system : "", temperature: null,
      showReasoning: true, tools: base ? [...base.tools] : fresh.tools,
      reasoning: base ? (base.reasoning || "auto") : "auto",
      imageSize: base ? base.imageSize : undefined,
      imageCount: base ? base.imageCount || 1 : 1,
      incognito, messages: [],
    };
    state.convs.unshift(conv);
    state.currentId = conv.id;
  }

  // Une conversation incognito qu'on quitte disparaît.
  watch(() => state.currentId, (now, was) => {
    const left = state.convs.find((c) => c.id === was);
    if (left && left.incognito && now !== was) {
      state.convs = state.convs.filter((c) => c.id !== was);
    }
  });

  function select(id) {
    if (state.streaming && id !== state.currentId) return;
    state.currentId = id;
    state.confirming = null;
  }

  function remove(id) {
    if (state.confirming !== id) { state.confirming = id; return; }
    const gone = state.convs.find((c) => c.id === id);
    state.convs = state.convs.filter((c) => c.id !== id);
    if (gone && !gone.incognito && saved.has(id)) {
      saved.delete(id);
      // Après un enregistrement en cours, qui la recréerait.
      Promise.resolve(running).then(() => history.drop(id)).catch((e) => {
        state.storageWarning = "Suppression non enregistrée : " + e.message;
      });
    }
    state.confirming = null;
    if (state.currentId === id) {
      state.currentId = sorted.value[0] ? sorted.value[0].id : null;
      if (!state.currentId) newConv();
    }
  }

  function startRename(c) { state.renaming = c.id; state.renameText = c.title; }
  function endRename(c) {
    if (state.renaming !== c.id) return;
    const t = state.renameText.trim();
    if (t) c.title = t.slice(0, 120);
    state.renaming = null;
  }

  // Exporter : la conversation en Markdown, images comprises (data URL),
  // téléchargée par le navigateur. Le raisonnement n'y est pas.
  async function exportMd(c) {
    const fence = (t) => "`".repeat(Math.max(3, ...(String(t).match(/`+/g) || [""]).map((b) => b.length + 1)));
    const out = [`# ${c.title}`, "", `*${new Date(c.created).toLocaleString("fr-FR")} · ${c.model}*`, ""];
    if (c.system && c.system.trim()) out.push("> Prompt système : " + c.system.trim().replace(/\n/g, "\n> "), "");
    for (const m of c.messages) {
      out.push(m.role === "user" ? "## Vous" : `## ${m.model || "Assistant"}`, "");
      for (const a of m.attachments || []) {
        if (a.kind === "image" && a.data) {
          const url = await history.dataUrl(a.data).catch(() => a.data);
          out.push(`![${a.name}](${url})`, "");
        }
        else if (a.kind === "text") { const f = fence(a.data); out.push(`\`${a.name}\``, "", f, a.data, f, ""); }
        else out.push(`*(${a.name} : image absente)*`, "");
      }
      if (m.content) out.push(m.content, "");
      if (m.error) out.push(`> Erreur : ${m.error}`, "");
      if ((m.sources || []).length) {
        out.push(...m.sources.map((x) => `- [${x.title || x.url}](${x.url})`), "");
      }
    }
    const blob = new Blob([out.join("\n")], { type: "text/markdown;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = (fold(c.title).replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "conversation") + ".md";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }


  return { state, sorted, filtered, current, open, newConv, persist, select, remove,
           startRename, endRename, exportMd, when, uid,
           setDefaults(fn) { defaults = fn; } };
})();
