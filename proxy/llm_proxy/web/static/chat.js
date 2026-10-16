/*
  L'onglet « Chat » du tableau de bord : un composant à part, enregistré
  par dashboard.js sous <chat-panel>, comme la section Mémoire. Il a ses
  propres appels et ne touche pas aux statistiques.

  Routes (même cookie que /ui/usage — la clé de la personne connectée) :
    GET  /ui/models             le catalogue (/v1/models)
    POST /ui/chat/completions   /v1/chat/completions, en flux ; ouvert par
                                [ui] chat = true dans config.toml
  Ce que la page sait du proxy (chat ouvert, outils hébergés déclarables)
  lui vient de /ui/session, par la prop `session`.

  CONVERSATIONS : dans CE navigateur (localStorage), une liste par clé
  connectée — rien n'est gardé côté proxy. Chacune garde son modèle, son
  prompt système, sa température et ses outils. Si le stockage déborde
  (des images jointes), les images des autres conversations sont
  retirées de l'historique local, pas leurs messages.

  OUTILS HÉBERGÉS : un interrupteur par TYPE déclarable
  (`{"type": "web_search"}` présente web_search, web_fetch, web_grep). Le
  proxy exécute les appels et les annonce dans le flux (`x_hosted_tool`,
  propre au chat de la page) : lancé, puis fini avec le début du résultat.
  L'échange caché (appels et résultats) n'est jamais dans l'historique de
  la page : c'est la mémoire du proxy ([chat].memory) qui le réinsère,
  tant que la réponse est renvoyée telle qu'elle a été reçue.

  MÉMOIRE : le panneau de la section Mémoire (memory.js, `compact`), à
  droite du fil, relu dès que le modèle a appelé l'outil `memory`.

  FILE D'ATTENTE : pendant une réponse, Entrée met le message en file
  (conv.queue, gardée avec la conversation) au lieu de l'envoyer. À la fin
  d'une réponse RÉUSSIE, le premier message de la file part tout seul ;
  une réponse arrêtée ou en erreur met la file en pause (« Envoyer la
  suite » la relance) — enchaîner sur un échec n'aurait pas de sens. Un
  message en file se modifie (il revient dans la zone de saisie) ou se
  retire.

  PIÈCES JOINTES : les images partent en `image_url` (data URL) — au
  modèle de les lire ; les fichiers texte sont joints au message, entre
  balises <file name="…">. Le reste est refusé.
*/
// Les CONVERSATIONS : un magasin partagé, lu par le chat (ChatPanel) ET par
// le menu latéral de la page (dashboard.js), qui en affiche la liste. Dans
// ce navigateur (localStorage), une liste par clé connectée.
window.ChatStore = (() => {
  const { reactive, computed, watch, toRaw } = Vue;
  const state = reactive({
    key: "", convs: [], currentId: null,
    streaming: false,               // une réponse en cours : pas de changement
    confirming: null,               // id de la conversation à supprimer
    renaming: null, renameText: "",
    storageWarning: "",
  });
  // Ce qu'une conversation neuve reçoit (modèle, outils) : posé par le chat,
  // qui connaît le catalogue et la session.
  let defaults = () => ({ model: "", tools: [] });
  const uid = () => Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
  const sorted = computed(() => [...state.convs].sort((a, b) => b.updated - a.updated));
  const current = computed(() => state.convs.find((c) => c.id === state.currentId) || null);

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
  function persist() {
    if (!state.key) return;
    try {
      localStorage.setItem(state.key, JSON.stringify(toRaw(state.convs)));
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
        localStorage.setItem(state.key, JSON.stringify(toRaw(state.convs)));
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

  function newConv() {
    if (state.streaming) return;
    // Une conversation vide est réutilisée plutôt que dupliquée.
    const empty = state.convs.find((c) => !c.messages.length);
    if (empty) { state.currentId = empty.id; return; }
    const base = current.value;
    const fresh = defaults();
    const conv = {
      id: uid(), title: "Nouvelle conversation", created: Date.now(),
      updated: Date.now(), model: base ? base.model : fresh.model,
      system: base ? base.system : "", temperature: null,
      showReasoning: true, tools: base ? [...base.tools] : fresh.tools,
      reasoning: base ? (base.reasoning || "auto") : "auto",
      messages: [],
    };
    state.convs.unshift(conv);
    state.currentId = conv.id;
  }

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

  const when = (ts) => {
    const d = new Date(ts), today = new Date();
    return d.toDateString() === today.toDateString()
      ? d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" })
      : d.toLocaleDateString("fr-FR", { day: "numeric", month: "short" });
  };

  return { state, sorted, current, open, newConv, select, remove,
           startRename, endRename, when, uid,
           setDefaults(fn) { defaults = fn; } };
})();

window.ChatPanel = {
  props: { session: { type: Object, required: true } },
  emits: ["unauthorized"],
  setup(props, { emit }) {
    const { ref, reactive, computed, watch, nextTick, onMounted, onUpdated, onUnmounted, toRaw } = Vue;
    const API = "/ui/chat/completions";
    const LAST_MODEL = "llm-proxy-chat-model";
    const MEMORY_OPEN = "llm-proxy-chat-memory";
    const MAX_TEXT_FILE = 256 * 1024;
    const MAX_IMAGE = 8 * 1024 * 1024;
    const TEXT_EXT = /\.(txt|md|markdown|csv|tsv|json|jsonl|ya?ml|toml|ini|cfg|conf|log|xml|html?|css|js|mjs|ts|tsx|jsx|py|rb|go|rs|java|kt|c|h|cc|cpp|hpp|cs|php|sh|bash|zsh|sql|r|lua|swift|vue|svelte|tex|env|dockerfile|makefile)$/i;
    // Noms lisibles des types d'outils ; un type inconnu garde le sien.
    const TOOL_LABELS = {
      web_search: "Web", code_execution: "Code", code_interpreter: "Code",
      image_generation: "Images", memory: "Mémoire", ocr: "OCR",
      transcribe: "Transcription",
    };

    const chat = computed(() => props.session.chat || {});
    const toolGroups = computed(() => (chat.value.tools || []).map((g) => ({
      ...g,
      label: TOOL_LABELS[g.type] || (g.type.startsWith("mcp:")
        ? "MCP " + g.type.slice(4) : g.type),
    })));
    const available = computed(() => new Set(toolGroups.value.map((g) => g.type)));
    // Les outils cochés d'office dans une conversation neuve : ceux que
    // [chat].always présente aux autres interfaces, sinon le web.
    function defaultTools() {
      const always = new Set(chat.value.always || []);
      const picked = toolGroups.value.filter((g) => g.tools.some((t) => always.has(t)))
        .map((g) => g.type);
      if (picked.length) return picked;
      return available.value.has("web_search") ? ["web_search"] : [];
    }

    // ── état ────────────────────────────────────────────────────────────
    // Les conversations : le magasin partagé avec le menu latéral.
    const S = window.ChatStore;
    S.open(props.session.client);
    const current = S.current;
    const currentId = computed(() => S.state.currentId);
    const models = ref([]);
    const modelsError = ref("");
    const draft = ref("");
    const pending = ref([]);          // pièces jointes du message en cours
    const streaming = ref(false);
    const status = ref("");
    const settingsOpen = ref(false);
    const storageWarning = computed(() => S.state.storageWarning);
    const dragging = ref(false);
    // Panneau mémoire : ouvert ou non (préférence de ce navigateur), et
    // un compteur qui le fait se relire après un appel de l'outil.
    const memoryOpen = ref((() => {
      try { return localStorage.getItem(MEMORY_OPEN) === "1"; } catch { return false; }
    })());
    const memoryStamp = ref(0);
    function toggleMemory(open = !memoryOpen.value) {
      memoryOpen.value = open;
      try { localStorage.setItem(MEMORY_OPEN, open ? "1" : "0"); } catch { /* sans stockage */ }
    }
    const thread = ref(null);
    // Écran étroit : textes courts (la zone de saisie, sur mobile).
    const narrowQuery = window.matchMedia ? window.matchMedia("(max-width: 760px)") : null;
    const narrow = ref(!!(narrowQuery && narrowQuery.matches));
    if (narrowQuery) narrowQuery.addEventListener("change", (e) => { narrow.value = e.matches; });
    const input = ref(null);
    let controller = null;
    let pinned = true;                // le fil suit la fin de la réponse

    // Une réponse en cours : le menu ne change pas de conversation.
    watch(streaming, (v) => { S.state.streaming = v; });
    // Ce qu'une conversation neuve reçoit, et ce que fait le chat quand la
    // conversation change (le menu la choisit) : suivre la fin du fil, et
    // la zone de saisie prête pour une conversation neuve.
    S.setDefaults(() => ({ model: defaultModel(), tools: defaultTools() }));
    watch(() => S.state.currentId, () => {
      pinned = true;
      nextTick(() => {
        if (thread.value) thread.value.scrollTop = thread.value.scrollHeight;
        if (current.value && !current.value.messages.length && input.value) input.value.focus();
      });
    });
    const uid = S.uid;

    // ── modèles ─────────────────────────────────────────────────────────
    const modelIds = computed(() => models.value.map((m) => m.id));
    function defaultModel() {
      const last = localStorage.getItem(LAST_MODEL);
      if (last && modelIds.value.includes(last)) return last;
      return modelIds.value[0] || last || "";
    }
    // Des images à un modèle que le catalogue ne dit pas capable de les
    // lire : envoyées quand même (le catalogue peut se tromper), mais dit.
    const blind = computed(() => {
      const c = current.value;
      const m = c && models.value.find((x) => x.id === c.model);
      return !!(m && !m.vision && pending.value.some((a) => a.kind === "image"));
    });

    async function loadModels() {
      try {
        const r = await fetch("/ui/models", { headers: { accept: "application/json" } });
        if (r.status === 401) { emit("unauthorized"); return; }
        if (!r.ok) throw new Error("HTTP " + r.status);
        const j = await r.json();
        // Le proxy ne rend que les modèles de conversation.
        models.value = (j.data || []).filter((m) => m.id)
          .sort((a, b) => a.id.localeCompare(b.id));
        modelsError.value = "";
        if (current.value && !current.value.model) current.value.model = defaultModel();
      } catch (e) {
        modelsError.value = "Catalogue illisible : " + e.message;
      }
    }

    watch(() => current.value && current.value.model, (m) => {
      if (m) localStorage.setItem(LAST_MODEL, m);
    });

    function toggleTool(type) {
      const c = current.value;
      if (!c) return;
      c.tools = c.tools.includes(type) ? c.tools.filter((t) => t !== type)
        : [...c.tools, type];
    }

    // ── pièces jointes ──────────────────────────────────────────────────
    const readAs = (file, how) => new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(r.result);
      r.onerror = () => reject(r.error);
      r[how](file);
    });

    async function addFiles(list) {
      for (const file of list) {
        try {
          if (file.type.startsWith("image/")) {
            if (file.size > MAX_IMAGE) throw new Error("image de plus de 8 Mo");
            pending.value.push({ kind: "image", name: file.name, type: file.type,
                                 size: file.size, data: await readAs(file, "readAsDataURL") });
          } else if (file.type.startsWith("text/") || TEXT_EXT.test(file.name)
                     || /json|xml|yaml|javascript|toml|csv/.test(file.type)) {
            if (file.size > MAX_TEXT_FILE) throw new Error("fichier texte de plus de 256 Ko");
            pending.value.push({ kind: "text", name: file.name, type: file.type || "text/plain",
                                 size: file.size, data: await readAs(file, "readAsText") });
          } else {
            throw new Error("seuls les images et les fichiers texte sont acceptés");
          }
          status.value = "";
        } catch (e) {
          status.value = `${file.name} : ${e.message}`;
        }
      }
    }
    function onPick(e) { addFiles([...e.target.files]); e.target.value = ""; }
    function onDrop(e) {
      dragging.value = false;
      if (e.dataTransfer && e.dataTransfer.files.length) addFiles([...e.dataTransfer.files]);
    }
    function onPaste(e) {
      const files = [...(e.clipboardData ? e.clipboardData.files : [])];
      if (files.length) { e.preventDefault(); addFiles(files); }
    }

    // ── envoi ───────────────────────────────────────────────────────────
    // Pendant une réponse, « envoyer » met en file : seul le contenu compte.
    const canSend = computed(() => current.value
      && current.value.model && (draft.value.trim() || pending.value.length));
    const queue = computed(() => (current.value && current.value.queue) || []);

    // Le message tel que l'API le veut : texte seul, ou parties (texte,
    // fichiers texte, images) quand il y a des pièces jointes.
    function apiContent(m) {
      const files = (m.attachments || []).filter((a) => a.kind === "text");
      const images = (m.attachments || []).filter((a) => a.kind === "image" && a.data);
      let text = m.content || "";
      for (const f of files) {
        text += `\n\n<file name="${f.name.replace(/"/g, "'")}">\n${f.data}\n</file>`;
      }
      if (!images.length) return text;
      return [
        ...(text ? [{ type: "text", text }] : []),
        ...images.map((a) => ({ type: "image_url", image_url: { url: a.data } })),
      ];
    }

    function apiMessages(conv, upto) {
      const out = [];
      if (conv.system.trim()) out.push({ role: "system", content: conv.system });
      for (const m of conv.messages.slice(0, upto)) {
        if (m.role === "user") out.push({ role: "user", content: apiContent(m) });
        // Une réponse en erreur ou vide n'est pas renvoyée : le modèle ne
        // l'a jamais écrite.
        else if (m.content) out.push({ role: "assistant", content: m.content });
      }
      return out;
    }

    function send() {
      const c = current.value;
      if (!canSend.value) return;
      const item = { id: uid(), content: draft.value.trim(), attachments: pending.value };
      draft.value = "";
      pending.value = [];
      // Une réponse en cours, ou une file en pause : à la suite de la file,
      // dans l'ordre où les messages ont été écrits.
      if (streaming.value || (c.queue && c.queue.length)) {
        (c.queue || (c.queue = [])).push(item);
        if (!streaming.value) resume();
        return;
      }
      ask(c, item);
    }

    function ask(c, item) {
      c.messages.push({ id: item.id, role: "user", content: item.content,
                        attachments: item.attachments });
      if (c.messages.length === 1) {
        const first = item.content || item.attachments.map((a) => a.name).join(", ");
        c.title = first.replace(/\s+/g, " ").slice(0, 60) + (first.length > 60 ? "…" : "");
      }
      pinned = true;
      respond(c);
    }

    // Le premier message de la file part.
    function resume() {
      const c = current.value;
      if (!c || streaming.value || !c.queue || !c.queue.length) return;
      ask(c, c.queue.shift());
    }

    // Un message en file revient dans la zone de saisie (à la suite de ce
    // qui y est déjà), et quitte la file.
    function unqueue(k) {
      const c = current.value;
      const [item] = c.queue.splice(k, 1);
      draft.value = draft.value.trim() ? draft.value.trim() + "\n\n" + item.content : item.content;
      pending.value = [...pending.value, ...item.attachments];
      nextTick(() => { if (input.value) { input.value.focus(); autosize(); } });
    }

    async function respond(conv) {
      const msg = { id: uid(), role: "assistant", content: "", reasoning: "",
                    tools: [], sources: [], model: conv.model, usage: null,
                    error: "", started: Date.now(), duration: 0 };
      const upto = conv.messages.length;
      conv.messages.push(msg);
      conv.updated = Date.now();
      // Le message RÉACTIF : c'est lui que le flux modifie.
      const live = conv.messages[conv.messages.length - 1];
      streaming.value = true;
      status.value = "";
      const ctrl = controller = new AbortController();
      const body = {
        model: conv.model, stream: true, stream_options: { include_usage: true },
        messages: apiMessages(conv, upto),
      };
      if (conv.temperature !== null && conv.temperature !== "") {
        body.temperature = Number(conv.temperature);
      }
      // Le niveau de réflexion. `reasoning_effort` est compris de llama.cpp
      // (il ACTIVE la réflexion d'un Qwen3, éteinte par défaut) et de vLLM
      // (gpt-oss : courte ou longue) ; « non » coupe celle d'un modèle
      // hybride par son gabarit (enable_thinking) — gpt-oss, lui, réfléchit
      // toujours un peu. « auto » : rien n'est envoyé.
      const effort = conv.reasoning || "auto";
      if (["low", "medium", "high"].includes(effort)) body.reasoning_effort = effort;
      else if (effort === "none") body.chat_template_kwargs = { enable_thinking: false };
      const tools = conv.tools.filter((t) => available.value.has(t));
      if (tools.length) body.tools = tools.map((type) => ({ type }));
      try {
        const r = await fetch(API, {
          method: "POST", signal: controller.signal,
          headers: { "content-type": "application/json", accept: "text/event-stream" },
          body: JSON.stringify(body),
        });
        if (r.status === 401) { live.error = "Session expirée : reconnectez-vous."; emit("unauthorized"); return; }
        if (!r.ok) {
          let j = null;
          try { j = await r.json(); } catch { /* corps illisible */ }
          live.error = (j && j.error && (j.error.message || j.error)) || `HTTP ${r.status}`;
          return;
        }
        if (!(r.headers.get("content-type") || "").includes("event-stream")) {
          const j = await r.json();
          const choice = (j.choices || [])[0] || {};
          live.content = (choice.message || {}).content || "";
          live.usage = j.usage || null;
          return;
        }
        await readStream(r.body, live);
      } catch (e) {
        if (e.name !== "AbortError") live.error = "Connexion interrompue : " + e.message;
        else if (!live.content) live.error = "Arrêté.";
      } finally {
        live.duration = (Date.now() - live.started) / 1000;
        for (const t of live.tools) if (t.status === "running") t.status = "error";
        streaming.value = false;
        controller = null;
        conv.updated = Date.now();
        // La suite de la file : seulement après une réponse réussie, et si
        // la conversation affichée est toujours celle-ci.
        if (conv.queue && conv.queue.length) {
          if (ctrl.signal.aborted || live.error) {
            status.value = "File en pause après " + (ctrl.signal.aborted ? "l'arrêt." : "l'erreur.");
          } else if (current.value && current.value.id === conv.id) {
            nextTick(resume);
          }
        }
      }
    }

    async function readStream(stream, live) {
      const reader = stream.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, nl).replace(/\r$/, "");
          buf = buf.slice(nl + 1);
          if (!line.startsWith("data:")) continue;   // «: ping» et le reste
          const data = line.slice(5).trim();
          if (!data || data === "[DONE]") continue;
          let doc;
          try { doc = JSON.parse(data); } catch { continue; }
          take(doc, live);
        }
      }
    }

    function take(doc, live) {
      if (doc.error) {
        live.error = typeof doc.error === "string" ? doc.error
          : doc.error.message || "erreur du proxy";
        return;
      }
      if (doc.usage) live.usage = doc.usage;
      for (const choice of doc.choices || []) {
        const d = choice.delta || {};
        if (d.content) live.content += d.content;
        const thought = d.reasoning_content || d.reasoning;
        if (typeof thought === "string") live.reasoning += thought;
        if (d.x_hosted_tool) {
          const ev = d.x_hosted_tool;
          const known = live.tools.find((t) => t.id === ev.id);
          if (known) Object.assign(known, ev);
          else live.tools.push({ ...ev });
          if (ev.name === "memory" && ev.status !== "running") memoryStamp.value++;
        }
        for (const a of d.annotations || []) {
          const c = a.url_citation;
          if (c && !live.sources.some((s) => s.url === c.url)) {
            live.sources.push({ url: c.url, title: c.title || c.url });
          }
        }
      }
    }

    function stop() { if (controller) controller.abort(); }

    // Régénérer : la dernière réponse est remplacée, la question reste.
    function regenerate() {
      const c = current.value;
      if (streaming.value || !c) return;
      const last = c.messages[c.messages.length - 1];
      if (last && last.role === "assistant") c.messages.pop();
      if (c.messages.length) respond(c);
    }

    // Modifier une question : elle revient dans la zone de saisie, et la
    // conversation reprend d'elle.
    function edit(i) {
      const c = current.value;
      if (streaming.value || !c) return;
      const m = c.messages[i];
      draft.value = m.content;
      pending.value = [...(m.attachments || [])].filter((a) => a.data);
      c.messages.splice(i);
      nextTick(() => { if (input.value) { input.value.focus(); autosize(); } });
    }

    function onKey(e) {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        send();
      }
    }
    function autosize() {
      const el = input.value;
      if (!el) return;
      el.style.height = "auto";
      // scrollHeight ne compte pas la bordure (box-sizing: border-box).
      const want = el.scrollHeight + el.offsetHeight - el.clientHeight;
      el.style.height = Math.min(want, 260) + "px";
      // Une barre de défilement seulement au-delà de la hauteur maximale :
      // en deçà, un arrondi de hauteur de ligne en ferait paraître une.
      el.style.overflowY = want > 260 ? "auto" : "hidden";
    }
    watch(draft, () => nextTick(autosize));

    // ── rendu ───────────────────────────────────────────────────────────
    // Le Markdown d'un message n'est recalculé que si son texte a changé :
    // pendant un flux, seul le dernier message se réanalyse.
    const cache = new WeakMap();
    function html(m, field = "content") {
      const raw = toRaw(m);
      let entry = cache.get(raw);
      if (!entry) cache.set(raw, entry = {});
      const src = m[field] || "";
      if (!entry[field] || entry[field].src !== src) {
        entry[field] = { src, html: window.Markdown.render(src) };
      }
      return entry[field].html;
    }

    // Copier : le bouton des blocs de code (rendus en v-html, donc par
    // délégation) et celui de chaque réponse.
    async function copyText(text, button) {
      try {
        await navigator.clipboard.writeText(text);
        if (button) {
          const was = button.textContent;
          button.textContent = "Copié";
          setTimeout(() => { button.textContent = was; }, 1200);
        }
      } catch { status.value = "Copie refusée par le navigateur."; }
    }
    function onThreadClick(e) {
      const b = e.target.closest("button.copy");
      if (!b) return;
      const code = b.closest(".code").querySelector("code");
      copyText(code ? code.textContent : "", b);
    }

    function onScroll() {
      const el = thread.value;
      pinned = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
    }
    onUpdated(() => {
      const el = thread.value;
      if (el && pinned) el.scrollTop = el.scrollHeight;
    });

    // La dernière ligne non vide d'un raisonnement, sans les marques de
    // Markdown, et son rang (la clé de l'animation : elle ne se rejoue qu'à
    // une NOUVELLE ligne, pas à chaque mot).
    // L'aperçu d'un raisonnement plié : une PHRASE entière à la fois, jamais
    // un texte en train de s'écrire (il défilerait, illisible). Pendant le
    // flux, la dernière phrase COMPLÈTE, remplacée au plus toutes les
    // REASON_PACE ms — à son rythme, pas à celui du modèle : les phrases
    // écrites entre deux sont sautées. Le flux fini, la dernière phrase,
    // même inachevée. `n` change à chaque remplacement : la transition.
    const REASON_PACE = 1800;
    const shown = reactive({});      // id du message → { text, n }
    function phrases(text) {
      const clean = String(text || "").replace(/[*_`]/g, "")
        .replace(/^\s*(#+|[-*>]|\d+[.)])\s+/gm, "");
      const parts = clean.split(/(?<=[.!?:;…])\s+|\n+/).map((p) => p.trim()).filter(Boolean);
      const done = /[.!?:;…]\s*$|\n\s*$/.test(clean);
      return { parts, complete: done ? parts : parts.slice(0, -1) };
    }
    function preview(m, i) {
      const live = streaming.value && i === current.value.messages.length - 1 && !m.content;
      const { parts, complete } = phrases(m.reasoning);
      if (!live) return { text: parts[parts.length - 1] || "", n: "fin" };
      const s = shown[m.id];
      // Avant le premier pas : la PREMIÈRE phrase (elle grandit vers la
      // droite tant qu'elle s'écrit), jamais la dernière — elle changerait
      // au rythme du modèle.
      if (!s) return { text: parts[0] || "", n: 0 };
      return s;
    }
    // Le pas de l'aperçu, tant qu'une réponse réfléchit.
    let pace = null;
    watch(streaming, (on) => {
      clearInterval(pace);
      if (!on) { for (const k in shown) delete shown[k]; return; }
      pace = setInterval(() => {
        const msgs = current.value ? current.value.messages : [];
        const m = msgs[msgs.length - 1];
        if (!m || m.role !== "assistant" || m.content || !m.reasoning) return;
        const { complete } = phrases(m.reasoning);
        const next = complete[complete.length - 1];
        const s = shown[m.id];
        if (next && (!s || s.text !== next)) shown[m.id] = { text: next, n: (s ? s.n : 0) + 1 };
      }, REASON_PACE);
    });
    onUnmounted(() => clearInterval(pace));

    // Ce que l'appel d'outil cherchait, en une ligne.
    function argSummary(t) {
      let a = {};
      try { a = JSON.parse(t.arguments || "{}"); } catch { return t.arguments; }
      // La mémoire : la commande et le fichier, pas le texte écrit.
      if (t.name === "memory" && a.command) {
        return [a.command, a.path || a.old_path, a.new_path ? "→ " + a.new_path : ""]
          .filter(Boolean).join(" ");
      }
      const v = a.query || a.queries || a.url || a.urls || a.pattern || a.prompt
        || a.path || a.command || a.code || Object.values(a)[0];
      const s = Array.isArray(v) ? v.join(" · ") : typeof v === "string" ? v
        : v === undefined ? "" : JSON.stringify(v);
      return s.length > 140 ? s.slice(0, 140) + "…" : s;
    }

    const num = (n) => Number(n || 0).toLocaleString("fr-FR");
    const size = (n) => n >= 1e6 ? (n / 1e6).toFixed(1).replace(".", ",") + " Mo"
      : Math.max(1, Math.round(n / 1e3)) + " Ko";
    const when = S.when;
    const host = (url) => { try { return new URL(url).host; } catch { return url; } };

    onMounted(async () => {
      if (!S.state.convs.length) S.newConv();
      await loadModels();
      for (const c of S.state.convs) {
        if (!c.model) c.model = defaultModel();
        if (!c.reasoning) c.reasoning = "auto";
      }
      nextTick(() => {
        if (thread.value) thread.value.scrollTop = thread.value.scrollHeight;
        autosize();
      });
    });

    return { chat, toolGroups, available, current, currentId,
             models, modelsError, blind, draft, pending, streaming, status,
             settingsOpen,
             storageWarning, dragging, narrow, memoryOpen, memoryStamp, toggleMemory, thread, input, canSend,
             toggleTool, onPick,
             onDrop, onPaste, send, stop, queue, resume, unqueue, regenerate, edit, onKey, html,
             copyText, onThreadClick, onScroll, argSummary, preview, num, size, when,
             host };
  },
  template: `
  <div class="panel chat-off" v-if="!chat.enabled">
    <div class="empty">
      <div class="big">Le chat n'est pas ouvert sur ce proxy</div>
      <div>Ajouter <code>[ui]</code> <code>chat = true</code> dans
        <code>config.toml</code> puis redémarrer. Les requêtes du chat sont
        comptées à la clé connectée, comme celles de l'API.</div>
    </div>
  </div>
  <div class="panel chat" v-else
       :class="{ 'with-memory': memoryOpen && $props.session.memory }">
    <section class="chat-main" v-if="current"
             @dragover.prevent="dragging = true" @dragleave.self="dragging = false"
             @drop.prevent="onDrop">
      <header class="chat-head">
        <h2 :title="current.title">{{ current.title }}</h2>
        <label class="model-pick" title="Modèle">
          <select v-model="current.model" :disabled="streaming">
            <option v-if="!models.length && current.model" :value="current.model">{{ current.model }}</option>
            <option v-if="current.model && models.length && !models.some((m) => m.id === current.model)"
                    :value="current.model">{{ current.model }} (absent du catalogue)</option>
            <option v-for="m in models" :key="m.id" :value="m.id">{{ m.id }}{{ m.vision ? " · images" : "" }}</option>
          </select>
        </label>
        <button type="button" class="btn ghost ico-btn" v-if="$props.session.memory"
                :aria-pressed="String(memoryOpen)" @click="toggleMemory()" title="Mémoire">
          <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 3h9l3 3v15H6zM9 9h6M9 13h6M9 17h4"/></svg>
          <span class="lbl">Mémoire</span></button>
        <button type="button" class="btn ghost ico-btn" :aria-pressed="String(settingsOpen)"
                @click="settingsOpen = !settingsOpen" title="Réglages">
          <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h10M18 7h2M4 17h4M12 17h8M16 5v4M10 15v4"/></svg>
          <span class="lbl">Réglages</span></button>
      </header>

      <div class="chat-settings" v-if="settingsOpen">
        <label class="field wide">
          <span>Prompt système</span>
          <textarea v-model="current.system" rows="3" spellcheck="false"
                    placeholder="Instructions données au modèle avant la conversation (facultatif)."></textarea>
        </label>
        <div class="field">
          <span>Température</span>
          <div class="temp">
            <label class="check"><input type="checkbox" :checked="current.temperature === null"
              @change="current.temperature = $event.target.checked ? null : 0.7"> celle du modèle</label>
            <template v-if="current.temperature !== null">
              <input type="range" min="0" max="2" step="0.05" v-model.number="current.temperature">
              <b>{{ Number(current.temperature).toFixed(2) }}</b>
            </template>
          </div>
        </div>
        <div class="field">
          <span>Affichage</span>
          <label class="check"><input type="checkbox" v-model="current.showReasoning">
            montrer le raisonnement du modèle</label>
        </div>
      </div>
      <p class="chat-warn" v-if="modelsError">{{ modelsError }}</p>
      <p class="chat-warn" v-if="storageWarning">{{ storageWarning }}</p>

      <div class="thread" ref="thread" @scroll="onScroll" @click="onThreadClick">
        <div class="empty" v-if="!current.messages.length">
          <div class="big">Une question ?</div>
          <div>{{ current.model || "Choisir un modèle" }}<template v-if="current.tools.length">
            · outils : {{ toolGroups.filter((g) => current.tools.includes(g.type)).map((g) => g.label).join(", ") }}</template></div>
        </div>
        <article v-for="(m, i) in current.messages" :key="m.id" class="msg" :class="m.role">
          <template v-if="m.role === 'user'">
            <div class="bubble">
              <div class="attachments" v-if="m.attachments && m.attachments.length">
                <template v-for="a in m.attachments" :key="a.name">
                  <img v-if="a.kind === 'image' && a.data" :src="a.data" :alt="a.name">
                  <span v-else class="chip file">{{ a.name }}<span class="dim" v-if="a.kind === 'image'">
                    · image retirée de l'historique local</span></span>
                </template>
              </div>
              <div class="text" v-if="m.content">{{ m.content }}</div>
            </div>
            <div class="msg-meta">
              <button type="button" class="link" :disabled="streaming" @click="edit(i)">Modifier</button>
            </div>
          </template>
          <template v-else>
            <!-- Plié de base, même pendant le flux : plié, sa DERNIÈRE ligne,
                 qui glisse en place à chaque nouvelle ligne (clé = rang de
                 la ligne), et miroite tant que le modèle réfléchit. -->
            <details class="reasoning" v-if="m.reasoning && current.showReasoning">
              <summary>
                <span class="r-label">Raisonnement</span>
                <span class="r-last"
                      :class="{ live: streaming && i === current.messages.length - 1 && !m.content }">
                  <!-- Durée FIXE : le reflet est une animation infinie sur le
                       même élément, Vue attendrait sa fin pour toujours. -->
                  <transition name="r-swap" mode="out-in" type="transition" :duration="280">
                    <span :key="preview(m, i).n">{{ preview(m, i).text }}</span>
                  </transition></span>
              </summary>
              <div class="md" v-html="html(m, 'reasoning')"></div>
            </details>
            <div class="tool-calls" v-if="m.tools.length">
              <details v-for="t in m.tools" :key="t.id" class="tool" :class="t.status">
                <summary>
                  <i class="state"></i><b>{{ t.name }}</b>
                  <span class="arg">{{ argSummary(t) }}</span>
                  <span class="dim" v-if="t.status === 'running'">en cours…</span>
                  <span class="dim" v-else-if="t.status === 'limit'">limite d'appels atteinte</span>
                  <span class="dim" v-else-if="t.chars !== undefined">{{ num(t.chars) }} car.</span>
                </summary>
                <pre v-if="t.preview">{{ t.preview }}{{ t.chars > t.preview.length ? "\\n…" : "" }}</pre>
              </details>
            </div>
            <div class="md" v-html="html(m)"></div>
            <div class="typing" v-if="streaming && i === current.messages.length - 1 && !m.content && !m.error">
              <i></i><i></i><i></i></div>
            <p class="msg-error" v-if="m.error">{{ m.error }}</p>
            <div class="sources" v-if="m.sources.length">
              <a v-for="s in m.sources" :key="s.url" :href="s.url" target="_blank"
                 rel="noopener noreferrer" :title="s.url">{{ s.title || host(s.url) }}
                <span class="dim">{{ host(s.url) }}</span></a>
            </div>
            <div class="msg-meta" v-if="!(streaming && i === current.messages.length - 1)">
              <span>{{ m.model }}</span>
              <span v-if="m.usage">{{ num(m.usage.prompt_tokens) }} → {{ num(m.usage.completion_tokens) }} tokens</span>
              <span v-if="m.duration">{{ m.duration.toFixed(1) }} s</span>
              <button type="button" class="link" v-if="m.content" @click="copyText(m.content, $event.target)">Copier</button>
              <button type="button" class="link" v-if="i === current.messages.length - 1"
                      @click="regenerate">Régénérer</button>
            </div>
          </template>
        </article>
      </div>

      <form class="composer" :class="{ dragging }" @submit.prevent="send">
        <div class="queue" v-if="queue.length">
          <div class="queue-head">
            <span>En file · {{ queue.length }}</span>
            <span class="dim" v-if="!streaming">en pause</span>
            <button type="button" class="link" v-if="!streaming" @click="resume">Envoyer la suite</button>
          </div>
          <ol>
            <li v-for="(q, k) in queue" :key="q.id">
              <span class="text">{{ q.content || q.attachments.map((a) => a.name).join(", ") }}</span>
              <span class="dim" v-if="q.content && q.attachments.length">+ {{ q.attachments.length }} pièce{{ q.attachments.length > 1 ? "s" : "" }}</span>
              <button type="button" class="link" @click="unqueue(k)">Modifier</button>
              <button type="button" class="icon" title="Retirer de la file" @click="queue.splice(k, 1)">×</button>
            </li>
          </ol>
        </div>
        <div class="attachments" v-if="pending.length">
          <span v-for="(a, k) in pending" :key="k" class="chip" :class="{ file: a.kind !== 'image' }">
            <img v-if="a.kind === 'image'" :src="a.data" alt="">
            {{ a.name }} <span class="dim">{{ size(a.size) }}</span>
            <button type="button" class="icon" title="Retirer" @click="pending.splice(k, 1)">×</button>
          </span>
        </div>
        <textarea ref="input" v-model="draft" rows="1" @keydown="onKey" @paste="onPaste"
                  :placeholder="narrow ? (streaming ? 'Réponse en cours… (mise en file)' : 'Écrire un message…')
                    : streaming ? 'Réponse en cours — Entrée pour mettre un message en file'
                    : 'Écrire un message — Entrée pour envoyer'"
                  title="Entrée pour envoyer, Maj+Entrée pour aller à la ligne"></textarea>
        <div class="composer-bar">
          <label class="btn ghost attach ico-btn" title="Joindre : images ou fichiers texte (ou glisser-déposer, ou coller)">
            <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11.5l-8.2 8.2a5 5 0 0 1-7-7L13 4.5a3.3 3.3 0 0 1 4.7 4.7l-8.2 8.2a1.7 1.7 0 0 1-2.4-2.4L14.6 7.5"/></svg>
            <span class="lbl">Joindre</span><input type="file" multiple hidden @change="onPick"></label>
          <label class="effort" title="Niveau de réflexion du modèle : auto (celui du modèle), non, faible, moyenne, élevée">
            <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-3.5 10.9c.6.5 1 1.2 1 2.1h5c0-.9.4-1.6 1-2.1A6 6 0 0 0 12 3z"/></svg>
            <select v-model="current.reasoning" :disabled="streaming" aria-label="Réflexion">
              <option value="auto">auto</option>
              <option value="none">non</option>
              <option value="low">faible</option>
              <option value="medium">moyenne</option>
              <option value="high">élevée</option>
            </select>
          </label>
          <div class="range tools-toggle" v-if="toolGroups.length" role="group" aria-label="Outils hébergés">
            <button v-for="g in toolGroups" :key="g.type" type="button"
                    :aria-pressed="String(current.tools.includes(g.type))"
                    :title="g.tools.join(', ')" @click="toggleTool(g.type)">{{ g.label }}</button>
          </div>
          <span class="dim" v-else-if="!chat.hosted_tools" title="[chat] hosted_tools = true dans config.toml">
            outils hébergés inactifs</span>
          <span class="status" :class="{ warn: blind && !status }">{{ status
            || (blind ? "ce modèle ne lit peut-être pas les images" : "") }}</span>
          <button type="submit" class="btn ico-btn send" :class="{ ghost: streaming }" :disabled="!canSend"
                  :title="streaming ? 'Mettre en file' : 'Envoyer'">
            <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5M5 12l7-7 7 7"/></svg>
            <span class="lbl">{{ streaming ? "Mettre en file" : "Envoyer" }}</span></button>
          <button v-if="streaming" type="button" class="btn danger ico-btn" @click="stop" title="Arrêter">
            <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M7 7h10v10H7z"/></svg>
            <span class="lbl">Arrêter</span></button>
        </div>
      </form>
    </section>
    <memory-panel v-if="memoryOpen && $props.session.memory" compact
                  :stamp="memoryStamp" @close="toggleMemory(false)"></memory-panel>
  </div>`,
};
