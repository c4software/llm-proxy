import { when } from "../lib/format.js";
import * as history from "./history.js";
import { forget as forgetTools } from "./tools/index.js";

// Les CONVERSATIONS : un magasin partagé, lu par le chat (ChatPanel) ET par
// le menu latéral de la page (app.js), qui en affiche la liste. Gardées
// par le proxy, une liste par compte connecté (chat/history.js).
export const ChatStore = (() => {
  const { reactive, computed, watch, toRaw } = Vue;
  const state = reactive({
    key: "", convs: [], currentId: null,
    listing: false,                 // la première page du menu arrive du proxy
    more: null,                     // la page suivante (son décalage), null : tout est là
    loadingMore: false,
    total: 0,                       // les conversations gardées par le proxy
    loadError: "",                  // la conversation affichée n'a pas pu être lue
    searching: false,               // la recherche du proxy est en cours
    found: [],                      // les ids qu'elle a trouvés
    streaming: false,               // une réponse en cours : pas de changement
    confirming: null,               // id de la conversation à supprimer
    renaming: null, renameText: "",
    storageWarning: "",
    query: "",                      // la recherche du menu latéral
  });
  // Ce qu'une conversation neuve reçoit (modèle, outils) : posé par le chat,
  // qui connaît le catalogue et la session.
  let defaults = () => ({ model: "", tools: [] });
  // Ce que le chat règle sur une conversation lue du proxy.
  let prepare = () => {};
  const uid = () => Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
  // Le menu tient des RÉSUMÉS (`stub` : id, titre, dates, `count` messages),
  // lus par pages ; une conversation n'arrive entière qu'ouverte.
  const count = (c) => (c.stub ? c.count : c.messages.length);
  const sorted = computed(() => [...state.convs].sort((a, b) => b.updated - a.updated));
  const current = computed(() => {
    const c = state.convs.find((x) => x.id === state.currentId);
    return c && !c.stub ? c : null;
  });
  // La conversation affichée, encore un résumé : elle arrive.
  const pending = computed(() => state.convs.find((c) => c.id === state.currentId && c.stub) || null);
  // La recherche : chaque mot, sans casse ni accents, dans le titre ou
  // le texte d'un message ; celles que le menu n'a pas encore lues, le
  // proxy les cherche (state.found). La conversation affichée reste listée.
  const fold = (t) => String(t || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
  const filtered = computed(() => {
    const words = fold(state.query).split(/\s+/).filter(Boolean);
    if (!words.length) return sorted.value;
    return sorted.value.filter((c) => {
      if (c.id === state.currentId || state.found.includes(c.id)) return true;
      const text = fold(c.title + (c.stub ? "" : "\n" + c.messages.map((m) => m.content).join("\n")));
      return words.every((w) => text.includes(w));
    });
  });

  // L'historique du COMPTE connecté, gardé par le proxy (chat/history.js).
  // Rien n'est relu si c'est déjà le sien. `ready` : la première page du
  // menu et la conversation affichée sont là.
  let ready = Promise.resolve();
  const saved = new Map();          // id → le JSON envoyé en dernier
  let wanted = null;                // l'id de l'adresse (#chat/<id>), avant la liste

  // Des résumés ajoutés au menu ; une conversation déjà là ne change pas.
  function merge(list) {
    for (const s of list) {
      const c = state.convs.find((x) => x.id === s.id);
      if (!c) state.convs.push({ ...s, stub: true });
      else if (c.stub) Object.assign(c, s);
    }
  }
  // Une conversation entière, à la place de son résumé.
  function adopt(doc) {
    saved.set(doc.id, JSON.stringify(doc));
    prepare(doc);
    const i = state.convs.findIndex((c) => c.id === doc.id);
    if (i < 0) state.convs.push(doc);
    else if (state.convs[i].stub) state.convs.splice(i, 1, doc);
    return state.convs.find((c) => c.id === doc.id);
  }
  // La conversation `id` entière (lue du proxy si besoin) ; null : inconnue.
  const inflight = new Map();
  function full(id) {
    const c = state.convs.find((x) => x.id === id);
    if (c && !c.stub) return Promise.resolve(c);
    if (!inflight.has(id)) {
      const key = state.key;
      inflight.set(id, history.one(id).then((doc) => {
        if (state.key !== key) return null;
        return doc ? adopt(doc) : null;
      }).finally(() => inflight.delete(id)));
    }
    return inflight.get(id);
  }
  // La conversation affichée, si ce n'est qu'un résumé : lue.
  async function fetchCurrent() {
    const id = state.currentId;
    if (!pending.value) return;
    state.loadError = "";
    try {
      if (await full(id)) return;
      // Supprimée ailleurs entre-temps.
      state.convs = state.convs.filter((c) => c.id !== id);
      if (state.currentId === id) {
        state.currentId = sorted.value[0] ? sorted.value[0].id : null;
        if (!state.currentId) newConv();
      }
    } catch (e) {
      if (state.currentId === id) state.loadError = "Conversation illisible : " + e.message;
    }
  }
  watch(() => state.currentId, fetchCurrent);

  function open(client) {
    const key = client || "open";
    if (state.key === key) return ready;
    // Une conversation commencée avant (pas encore gardée) reste ; celles
    // d'un autre compte partent.
    state.convs = state.convs.filter((c) => !c.stub && !saved.has(c.id));
    state.key = key;
    state.listing = true;
    saved.clear();
    // La conversation de l'adresse est lue EN MÊME TEMPS que la première
    // page du menu : elle peut être bien plus loin dans la liste.
    const first = wanted;
    wanted = null;
    ready = Promise.all([history.page(0), first ? history.one(first).catch(() => null) : null])
      .then(([page, doc]) => {
        if (state.key !== key) return;
        merge(page.list.filter((s) => !saved.has(s.id)));
        state.more = page.next;
        state.total = page.total;
        if (doc) state.currentId = adopt(doc).id;
        else if (!state.currentId && state.convs.length) state.currentId = sorted.value[0].id;
        state.storageWarning = "";
        return fetchCurrent();
      }).catch((e) => {
        state.storageWarning = "Historique indisponible : " + e.message;
      }).finally(() => { state.listing = false; });
    return ready;
  }

  // La page suivante du menu (au bout de la liste).
  async function loadMore() {
    if (state.more === null || state.loadingMore || state.listing) return;
    const key = state.key;
    state.loadingMore = true;
    try {
      const page = await history.page(state.more);
      if (state.key !== key) return;
      merge(page.list);
      state.more = page.next;
      state.total = page.total;
    } catch (e) {
      state.storageWarning = "Liste des conversations incomplète : " + e.message;
    } finally { state.loadingMore = false; }
  }

  // La recherche du menu, aussi chez le proxy (les conversations pas encore
  // lues), un peu après la frappe.
  let searchTimer = null, searchSeq = 0;
  watch(() => state.query, (q) => {
    clearTimeout(searchTimer);
    const seq = ++searchSeq;
    state.found = [];
    state.searching = !!q.trim() && !!state.key;
    if (!state.searching) return;
    searchTimer = setTimeout(async () => {
      try {
        const list = await history.search(q.trim());
        if (seq !== searchSeq) return;
        merge(list);
        state.found = list.map((c) => c.id);
      } catch { /* la recherche locale reste */ }
      finally { if (seq === searchSeq) state.searching = false; }
    }, 300);
  });

  // Enregistrement différé : un flux modifie le message des dizaines de
  // fois par seconde, le proxy n'a besoin que d'un état de temps en
  // temps. Seules partent les conversations qui ont changé — jamais une
  // conversation incognito, ni une conversation encore vide.
  let saveTimer = null, running = null, again = false;
  const kept = (c) => !c.stub && !c.incognito && c.messages.length;
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
    const empty = state.convs.find((c) => !c.stub && !c.messages.length && !!c.incognito === incognito);
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

  // La conversation que nomme l'adresse : ouverte tout de suite si elle est
  // là, sinon dès que la liste arrive (introuvable : la plus récente).
  function want(id) {
    if (state.convs.some((c) => c.id === id)) return select(id);
    if (!state.key || state.listing) { wanted = id; return; }
    full(id).then((c) => { if (c) select(id); }).catch(() => {});
  }

  function remove(id) {
    if (state.confirming !== id) { state.confirming = id; return; }
    const gone = state.convs.find((c) => c.id === id);
    state.convs = state.convs.filter((c) => c.id !== id);
    forgetTools(id);                 // ce que les outils gardaient pour elle (la base SQL…)
    if (gone && !gone.incognito && (gone.stub || saved.has(id))) {
      saved.delete(id);
      state.total = Math.max(0, state.total - 1);
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
  async function endRename(c) {
    if (state.renaming !== c.id) return;
    const t = state.renameText.trim();
    state.renaming = null;
    if (!t) return;
    c.title = t.slice(0, 120);
    // Un résumé : la conversation entière, pour que le titre soit gardé.
    if (c.stub) {
      try { const conv = await full(c.id); if (conv) conv.title = t.slice(0, 120); }
      catch (e) { state.storageWarning = "Titre non enregistré : " + e.message; }
    }
  }

  // Exporter : la conversation en Markdown, images comprises (data URL),
  // téléchargée par le navigateur. Le raisonnement n'y est pas.
  async function exportMd(c) {
    if (c.stub) c = await full(c.id);
    if (!c) return;
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


  return { state, sorted, filtered, current, pending, count, open, loadMore, fetchCurrent,
           newConv, persist, select, want, remove, startRename, endRename, exportMd, when, uid,
           setDefaults(fn) { defaults = fn; }, setPrepare(fn) { prepare = fn; } };
})();
