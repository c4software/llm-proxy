import { when } from "../lib/format.js";

// Les CONVERSATIONS : un magasin partagé, lu par le chat (ChatPanel) ET par
// le menu latéral de la page (app.js), qui en affiche la liste. Dans
// ce navigateur (localStorage), une liste par clé connectée.
export const ChatStore = (() => {
  const { reactive, computed, watch, toRaw } = Vue;
  const state = reactive({
    key: "", convs: [], currentId: null,
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

  // La liste de cette clé. Rien n'est relu si c'est déjà elle.
  function open(client) {
    const key = "llm-proxy-chats:" + (client || "open");
    if (state.key === key) return;
    state.key = key;
    let list = [];
    try {
      list = JSON.parse(localStorage.getItem(key) || "[]");
      if (!Array.isArray(list)) list = [];
    } catch { list = []; }
    state.convs = list;
    state.currentId = list[0] ? list[0].id : null;
  }

  // Enregistrement différé : un flux modifie le message des dizaines de
  // fois par seconde, le stockage n'a besoin que du dernier état.
  let saveTimer = null;
  // Ce qui s'écrit : jamais une conversation incognito.
  const kept = () => toRaw(state.convs).filter((c) => !c.incognito);
  function persist() {
    if (!state.key) return;
    try {
      localStorage.setItem(state.key, JSON.stringify(kept()));
      state.storageWarning = "";
    } catch {
      // Plein : on retire les images des AUTRES conversations, puis on
      // réessaie. Leurs messages restent.
      let dropped = 0;
      for (const c of state.convs) {
        if (c.id === state.currentId) continue;
        for (const m of c.messages) {
          for (const a of m.attachments || []) {
            if (a.kind === "image" && a.data) { a.data = null; dropped++; }
          }
        }
      }
      try {
        localStorage.setItem(state.key, JSON.stringify(kept()));
        state.storageWarning = dropped
          ? `Stockage du navigateur plein : ${dropped} image(s) d'anciennes conversations retirée(s).`
          : "";
      } catch {
        state.storageWarning = "Stockage du navigateur plein : cette conversation "
          + "n'est plus enregistrée. Supprimez-en d'anciennes.";
      }
    }
  }
  watch(() => state.convs, () => {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(persist, 400);
  }, { deep: true });

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
    state.convs = state.convs.filter((c) => c.id !== id);
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
  function exportMd(c) {
    const fence = (t) => "`".repeat(Math.max(3, ...(String(t).match(/`+/g) || [""]).map((b) => b.length + 1)));
    const out = [`# ${c.title}`, "", `*${new Date(c.created).toLocaleString("fr-FR")} · ${c.model}*`, ""];
    if (c.system && c.system.trim()) out.push("> Prompt système : " + c.system.trim().replace(/\n/g, "\n> "), "");
    for (const m of c.messages) {
      out.push(m.role === "user" ? "## Vous" : `## ${m.model || "Assistant"}`, "");
      for (const a of m.attachments || []) {
        if (a.kind === "image" && a.data) out.push(`![${a.name}](${a.data})`, "");
        else if (a.kind === "text") { const f = fence(a.data); out.push(`\`${a.name}\``, "", f, a.data, f, ""); }
        else out.push(`*(${a.name} : image retirée de l'historique local)*`, "");
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


  return { state, sorted, filtered, current, open, newConv, select, remove,
           startRename, endRename, exportMd, when, uid,
           setDefaults(fn) { defaults = fn; } };
})();
