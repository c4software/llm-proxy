/*
  L'onglet « Chat » du tableau de bord : un composant à part, enregistré
  par app.js sous <chat-panel>, comme la mémoire (memory.js). Il a ses
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

  MÉMOIRE : le panneau de la mémoire (memory.js, `compact`), à
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

  INCOGNITO : une conversation marquée `incognito` n'est jamais écrite
  dans le stockage du navigateur, et disparaît dès qu'on la quitte (ou
  qu'on recharge la page). Ses requêtes portent `X-Incognito: 1` : le
  proxy n'en garde rien (ni échange d'outils, ni cache web, pas l'outil
  `memory`), et un fichier rendu ne vit que le temps de s'afficher. Les
  statistiques (des comptes, sans contenu) restent.

  MODÈLES D'IMAGES : /ui/models?images=1 les ajoute au catalogue
  (`image`). Choisi, un tel modèle reçoit le dernier message, précédé des
  demandes de la même suite d'images (le contexte : « plus sombre » suit
  ce qu'il retouche), par
    POST /ui/images/generations   /v1/images/generations
    POST /ui/images/edits         /v1/images/edits, si une image est
                                  jointe (ou « Retoucher » une image rendue)
  de 1 à 4 images, une requête chacune, à la taille choisie (celles de
  [tools.image_generation].sizes). L'image rendue (`b64_json`, ou une
  `url` absolue) devient une pièce jointe de la réponse : gardée comme
  celles des messages, et retirée de même si le stockage déborde. Un clic
  l'agrandit.
*/
import { ChatStore } from "./store.js";
import { readAttachment } from "./attachments.js";
import { imagePrompt, oneImage } from "./images.js";
import { readStream } from "./stream.js";
import * as workspace from "./workspace.js";
import { Markdown } from "../lib/markdown.js";
import { num, size } from "../lib/format.js";

export const ChatPanel = {
  props: { session: { type: Object, required: true } },
  emits: ["unauthorized"],
  setup(props, { emit }) {
    const { ref, reactive, computed, watch, nextTick, onMounted, onUpdated, onUnmounted, toRaw } = Vue;
    const API = "/ui/chat/completions";
    // Tours de lecture du dossier au plus, par réponse.
    const MAX_ROUNDS = 25;
    const LAST_MODEL = "llm-proxy-chat-model";
    const MEMORY_OPEN = "llm-proxy-chat-memory";
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
    const S = ChatStore;
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
    const modelIds = computed(() => models.value.filter((m) => !m.image).map((m) => m.id));
    // Le modèle de la conversation est-il un modèle d'images ?
    const isImage = (id) => !!(models.value.find((m) => m.id === id) || {}).image;
    const imageMode = computed(() => !!current.value && isImage(current.value.model));
    const imageSizes = computed(() => chat.value.image_sizes || ["512x512"]);
    // Le modèle d'une nouvelle conversation : celui que le proxy
    // présélectionne ([ui].chat_model), s'il est au catalogue ; sinon le
    // dernier choisi dans ce navigateur, sinon le premier.
    function defaultModel() {
      const preset = chat.value.model;
      if (preset && modelIds.value.includes(preset)) return preset;
      const last = localStorage.getItem(LAST_MODEL);
      if (last && modelIds.value.includes(last)) return last;
      return modelIds.value[0] || last || "";
    }
    // Des images à un modèle que le catalogue ne dit pas capable de les
    // lire : envoyées quand même (le catalogue peut se tromper), mais dit.
    const blind = computed(() => {
      const c = current.value;
      const m = c && models.value.find((x) => x.id === c.model);
      return !!(m && !m.vision && !m.image && pending.value.some((a) => a.kind === "image"));
    });
    // Un modèle d'images ne reçoit qu'UNE image (à retoucher) : ni fichier
    // texte, ni seconde image.
    const unsent = computed(() => imageMode.value && (pending.value.some((a) => a.kind !== "image")
      || pending.value.filter((a) => a.kind === "image").length > 1));

    async function loadModels() {
      try {
        const r = await fetch("/ui/models?images=1", { headers: { accept: "application/json" } });
        if (r.status === 401) { emit("unauthorized"); return; }
        if (!r.ok) throw new Error("HTTP " + r.status);
        const j = await r.json();
        // Le proxy ne rend que les modèles de conversation, et d'images.
        models.value = (j.data || []).filter((m) => m.id)
          .sort((a, b) => a.id.localeCompare(b.id));
        modelsError.value = "";
        if (current.value && !current.value.model) current.value.model = defaultModel();
      } catch (e) {
        modelsError.value = "Catalogue illisible : " + e.message;
      }
    }

    watch(() => current.value && current.value.model, (m) => {
      if (m && !isImage(m)) localStorage.setItem(LAST_MODEL, m);
    });

    // Incognito : la conversation vide change de genre ; sinon, une neuve
    // de l'autre genre (l'incognito quittée disparaît).
    function toggleIncognito() {
      const c = current.value;
      if (!c || streaming.value) return;
      if (!c.messages.length) c.incognito = !c.incognito;
      else S.newConv(!c.incognito);
    }

    function toggleTool(type) {
      const c = current.value;
      if (!c) return;
      c.tools = c.tools.includes(type) ? c.tools.filter((t) => t !== type)
        : [...c.tools, type];
    }

    // ── pièces jointes ──────────────────────────────────────────────────
    async function addFiles(list) {
      for (const file of list) {
        try {
          pending.value.push(await readAttachment(file));
          status.value = "";
        } catch (e) {
          status.value = `${file.name} : ${e.message}`;
        }
      }
    }
    function onPick(e) { addFiles([...e.target.files]); e.target.value = ""; }

    // ── dossier ouvert (chat/workspace.js) ──────────────────────────────
    // En mémoire seulement, par conversation : `conv.folder` n'en garde
    // que le nom (après un rechargement : « à rouvrir »).
    const folders = new Map();
    const folderTick = ref(0);            // Map non réactive : un compteur
    const folderReady = computed(() => (folderTick.value, !!current.value
      && folders.has(current.value.id)));
    const folderCount = computed(() => (folderTick.value, current.value
      && folders.has(current.value.id) ? folders.get(current.value.id).count : 0));
    function onFolder(e) {
      const c = current.value;
      const list = [...e.target.files];
      e.target.value = "";
      if (!c || !list.length) return;
      const ws = workspace.openFolder(list);
      if (!ws.count) { status.value = `${ws.name} : aucun fichier lisible.`; return; }
      folders.set(c.id, ws);
      c.folder = { name: ws.name, count: ws.count };
      folderTick.value++;
      status.value = `Dossier « ${ws.name} » ouvert : ${ws.count} fichier${ws.count > 1 ? "s" : ""}`
        + (ws.ignored ? ` (${ws.ignored} ignoré${ws.ignored > 1 ? "s" : ""} : .git, node_modules…)` : "")
        + " — le modèle les lit à la demande.";
    }
    function closeFolder() {
      const c = current.value;
      if (!c) return;
      folders.delete(c.id);
      delete c.folder;
      folderTick.value++;
    }
    // Le menu « Joindre » : fichiers, ou un dossier. Se ferme au clic dehors.
    const attachOpen = ref(false);
    function onDocClick(e) {
      if (attachOpen.value && !e.target.closest(".attach-menu")) attachOpen.value = false;
    }
    document.addEventListener("click", onDocClick);
    onUnmounted(() => document.removeEventListener("click", onDocClick));
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
        else if ((m.steps || []).length) {
          // Une réponse qui a lu le dossier : ses appels et leurs résultats,
          // puis son dernier texte — le modèle retrouve ce qu'il a lu.
          out.push(...workspace.stepMessages(m.steps));
          if (m.final) out.push({ role: "assistant", content: m.final });
        }
        else if (m.content) out.push({ role: "assistant", content: m.content });
        else if ((m.attachments || []).length) {
          out.push({ role: "assistant", content: "(image générée : "
            + m.attachments.map((a) => a.name).join(", ") + ")" });
        }
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
      if (isImage(conv.model)) return generate(conv);
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
      // Incognito : rien de persistant, donc pas l'outil `memory`.
      const tools = conv.tools.filter((t) => available.value.has(t)
        && !(conv.incognito && t === "memory"));
      // Le dossier ouvert : ses outils, exécutés ici (chat/workspace.js).
      const ws = folders.get(conv.id) || null;
      body.tools = [...tools.map((type) => ({ type })), ...(ws ? workspace.TOOLS : [])];
      if (!body.tools.length) delete body.tools;
      try {
        // Un tour par requête : tant que le modèle appelle les outils du
        // dossier, la page les exécute et le relance avec leurs résultats.
        for (let round = 0; ; round++) {
          const start = live.content.length;
          if (live.steps) body.messages = [...apiMessages(conv, upto), ...workspace.stepMessages(live.steps)];
          const calls = [];
          const r = await fetch(API, {
            method: "POST", signal: controller.signal,
            headers: { "content-type": "application/json", accept: "text/event-stream",
                       ...(conv.incognito ? { "x-incognito": "1" } : {}) },
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
          await readStream(r.body, live, { calls, onMemory: () => { memoryStamp.value++; } });
          const asked = calls.filter((c) => c && workspace.TOOL_NAMES.has(c.name));
          if (!ws || !asked.length || live.error) {
            if (live.steps) live.final = live.content.slice(start);
            break;
          }
          if (round >= MAX_ROUNDS) {
            live.error = `Arrêté après ${MAX_ROUNDS} tours de lecture du dossier.`;
            break;
          }
          const step = { content: live.content.slice(start), calls: [] };
          for (const c of asked) {
            const id = c.id || "call_" + uid();
            live.tools.push({ id, name: c.name, arguments: c.arguments, status: "running" });
            const chip = live.tools[live.tools.length - 1];
            const result = await workspace.run(ws, c.name, c.arguments);
            Object.assign(chip, { status: result.startsWith("Error:") ? "error" : "done",
                                  chars: result.length, preview: result.slice(0, 600) });
            step.calls.push({ id, name: c.name, arguments: c.arguments, result });
          }
          (live.steps || (live.steps = [])).push(step);
          if (live.content && !live.content.endsWith("\n")) live.content += "\n\n";
        }
      } catch (e) {
        if (e.name !== "AbortError") live.error = "Connexion interrompue : " + e.message;
        else if (!live.content) live.error = "Arrêté.";
      } finally {
        done(conv, live, ctrl);
      }
    }

    function done(conv, live, ctrl) {
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

    // Un modèle d'images : le dernier message (avec son contexte) → une ou
    // plusieurs images, pièces jointes de la réponse. Une image jointe au
    // message est RETOUCHÉE (/ui/images/edits). Les images partent une à
    // une (n = 1 chacune : plus d'un serveur ignore `n`), et s'affichent
    // à mesure.
    async function generate(conv) {
      const msg = { id: uid(), role: "assistant", content: "", reasoning: "",
                    tools: [], sources: [], attachments: [], image: true, prompt: "",
                    model: conv.model, usage: null, error: "",
                    started: Date.now(), duration: 0 };
      const ask = [...conv.messages].reverse().find((m) => m.role === "user");
      const source = ask && (ask.attachments || []).find((a) => a.kind === "image" && a.data);
      const prompt = imagePrompt(conv, !!source);
      conv.messages.push(msg);
      conv.updated = Date.now();
      const live = conv.messages[conv.messages.length - 1];
      live.prompt = prompt;
      streaming.value = true;
      status.value = "";
      const ctrl = controller = new AbortController();
      const size = imageSizes.value.includes(conv.imageSize) ? conv.imageSize
        : (chat.value.image_size || imageSizes.value[0]);
      const count = Math.min(Math.max(Number(conv.imageCount) || 1, 1), 4);
      try {
        if (!ask || !ask.content.trim()) { live.error = "Décrire l'image à générer."; return; }
        for (let k = 0; k < count && !ctrl.signal.aborted; k++) {
          const got = await oneImage(conv, { model: conv.model, prompt, size, n: 1,
            ...(source ? { image: source.data } : {}) }, ctrl.signal, () => emit("unauthorized"));
          const { revised, ...image } = got;
          live.attachments.push(image);
          if (revised && !live.content) live.content = "*" + revised + "*";
        }
      } catch (e) {
        live.error = e.name === "AbortError" ? (live.attachments.length ? "" : "Arrêté.") : e.message;
      } finally {
        done(conv, live, ctrl);
      }
    }

    // L'image agrandie (clic sur une image rendue), et la retouche : l'image
    // rejoint les pièces jointes du message en cours.
    const zoom = ref(null);
    function retouch(a) {
      pending.value = [...pending.value.filter((p) => p.kind !== "image"),
        { kind: "image", name: a.name, type: a.type, size: a.size, data: a.data }];
      zoom.value = null;
      nextTick(() => { if (input.value) input.value.focus(); });
    }
    function onZoomKey(e) { if (e.key === "Escape" && zoom.value) zoom.value = null; }
    window.addEventListener("keydown", onZoomKey);

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
        entry[field] = { src, html: Markdown.render(src) };
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

    // Le défilement que la page fait elle-même (`autoTop`) ne décroche pas
    // le fil : son évènement `scroll` arrive APRÈS coup, quand une image
    // a pu allonger le fil entre-temps — il se croirait loin de la fin.
    let autoTop = -1;
    function toEnd() {
      const el = thread.value;
      if (!el || !pinned) return;
      el.scrollTop = el.scrollHeight;
      autoTop = el.scrollTop;
    }
    function onScroll() {
      const el = thread.value;
      if (el.scrollTop === autoTop) return;
      pinned = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
    }
    onUpdated(toEnd);
    // Une image chargée après le rendu allonge le fil : il la suit aussi.
    const onImageLoad = toEnd;

    // La dernière ligne non vide d'un raisonnement, sans les marques de
    // Markdown, et son rang (la clé de l'animation : elle ne se rejoue qu'à
    // une NOUVELLE ligne, pas à chaque mot).
    // L'aperçu d'un raisonnement plié : UNE ligne lisible, remplacée au plus
    // toutes les REASON_PACE ms (à son rythme, pas à celui du modèle).
    // `n` change à chaque remplacement : c'est la clé de la transition.
    const REASON_PACE = 1800;
    const REASON_TAIL = 70;          // au-delà, une phrase en cours montre sa fin
    const shown = reactive({});      // id du message → { text, n }
    function cleanMd(text) {
      return String(text || "").replace(/[*_`]/g, "")
        .replace(/^\s*(#+|[-*>]|\d+[.)])\s+/gm, "");
    }
    // La ligne à montrer : la dernière phrase finie ; si la phrase en cours
    // est courte, la précédente (finie) ; si elle est longue, ses derniers
    // mots, précédés de « … ». Même règle pour une ligne courte ou longue.
    function candidate(text) {
      const t = cleanMd(text);
      const parts = t.split(/(?<=[.!?:;…])\s+|\n+/).map((p) => p.trim()).filter(Boolean);
      if (!parts.length) return "";
      const last = parts[parts.length - 1];
      if (/[.!?:;…]\s*$|\n\s*$/.test(t)) return last;
      if (last.length <= REASON_TAIL) return parts[parts.length - 2] || last;
      // Le dernier mot, encore en train de s'écrire, est retiré.
      const cut = last.slice(-REASON_TAIL).replace(/\s+\S*$/, "");
      return "… " + cut.slice(cut.indexOf(" ") + 1);
    }
    function preview(m, i) {
      const live = streaming.value && i === current.value.messages.length - 1 && !m.content;
      if (!live) return { text: candidate(m.reasoning), n: "fin" };
      return shown[m.id] || { text: "", n: 0 };
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
        const text = candidate(m.reasoning);
        const s = shown[m.id];
        if (text && (!s || s.text !== text)) shown[m.id] = { text, n: (s ? s.n : 0) + 1 };
      }, REASON_PACE);
    });
    onUnmounted(() => { clearInterval(pace); window.removeEventListener("keydown", onZoomKey); });

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

    const when = S.when;
    const host = (url) => { try { return new URL(url).host; } catch { return url; } };

    onMounted(async () => {
      if (!S.state.convs.length) S.newConv();
      await loadModels();
      for (const c of S.state.convs) {
        if (!c.model) c.model = defaultModel();
        if (!c.imageSize) c.imageSize = chat.value.image_size;
        if (!c.imageCount) c.imageCount = 1;
        if (!c.reasoning) c.reasoning = "auto";
      }
      nextTick(() => {
        if (thread.value) thread.value.scrollTop = thread.value.scrollHeight;
        autosize();
      });
    });

    return { chat, toolGroups, available, current, currentId,
             models, modelsError, blind, unsent, imageMode, imageSizes, toggleIncognito, zoom, retouch, exportMd: S.exportMd, draft, pending, streaming, status,
             settingsOpen,
             storageWarning, dragging, narrow, memoryOpen, memoryStamp, toggleMemory, thread, input, canSend,
             toggleTool, onPick, onFolder, closeFolder, folderReady, folderCount, attachOpen,
             onDrop, onPaste, send, stop, queue, resume, unqueue, regenerate, edit, onKey, html,
             copyText, onThreadClick, onScroll, onImageLoad, argSummary, preview, num, size, when,
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
       :class="{ 'with-memory': memoryOpen && $props.session.memory, incognito: current && current.incognito }">
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
            <optgroup label="Conversation">
              <option v-for="m in models.filter((m) => !m.image)" :key="m.id" :value="m.id">{{ m.id }}{{ m.vision ? " · vision" : "" }}</option>
            </optgroup>
            <optgroup label="Génération d'images" v-if="models.some((m) => m.image)">
              <option v-for="m in models.filter((m) => m.image)" :key="m.id" :value="m.id">{{ m.id }}</option>
            </optgroup>
          </select>
        </label>
        <button type="button" class="btn ghost ico-btn incognito-btn" :disabled="streaming"
                :aria-pressed="String(!!current.incognito)" @click="toggleIncognito"
                :title="current.incognito ? 'Quitter le mode incognito' : 'Conversation incognito : rien n’est gardé'">
          <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M2 11h20M5 11l2.5-6h9l2.5 6M10 16.5c1.3-.8 2.7-.8 4 0"/><circle cx="7" cy="16.5" r="3"/><circle cx="17" cy="16.5" r="3"/></svg>
          <span class="lbl">Incognito</span></button>
        <button type="button" class="btn ghost ico-btn" v-if="$props.session.memory"
                :aria-pressed="String(memoryOpen)" @click="toggleMemory()"
                title="Mémoire : ce que les modèles ont retenu">
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
        <div class="field">
          <span>Conversation</span>
          <div><button type="button" class="btn ghost" :disabled="!current.messages.length"
                       @click="exportMd(current)">Exporter en Markdown</button></div>
        </div>
      </div>
      <p class="chat-incognito" v-if="current.incognito">Incognito : rien n'est gardé, ni dans
        ce navigateur ni par le proxy (ni mémoire, ni cache), et la conversation disparaît dès
        que vous la quittez ou rechargez la page.</p>
      <p class="chat-warn" v-if="modelsError">{{ modelsError }}</p>
      <p class="chat-warn" v-if="storageWarning">{{ storageWarning }}</p>

      <div class="thread" ref="thread" @scroll="onScroll" @click="onThreadClick">
        <div class="empty" v-if="!current.messages.length">
          <div class="big">{{ imageMode ? "Quelle image ?" : current.incognito ? "Une question, sans trace ?" : "Une question ?" }}</div>
          <div v-if="!current.model">Choisir un modèle</div>
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
            <!-- Plié de base : une ligne d'aperçu, remplacée à son rythme
                 (clé = n), et le CONTENEUR miroite tant que le modèle
                 réfléchit (jamais le span coupé par « … », qui transite). -->
            <details class="reasoning" v-if="m.reasoning && current.showReasoning">
              <summary>
                <span class="r-label">Raisonnement</span>
                <span class="r-last"
                      :class="{ live: streaming && i === current.messages.length - 1 && !m.content }">
                  <transition name="r-swap" mode="out-in">
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
            <div class="gen-images" :class="{ multi: m.attachments.length > 1 }" v-if="m.attachments && m.attachments.length">
              <template v-for="a in m.attachments" :key="a.name">
                <button v-if="a.data" type="button" class="gen-image" title="Agrandir"
                        @click="zoom = a"><img :src="a.data" :alt="a.name" @load="onImageLoad"></button>
                <span v-else class="chip file">{{ a.name }}<span class="dim">
                  · image retirée de l'historique local</span></span>
              </template>
            </div>
            <details class="gen-prompt" v-if="m.image && m.prompt && m.prompt.includes('\\n')">
              <summary>prompt envoyé (avec le contexte)</summary><pre>{{ m.prompt }}</pre></details>
            <div class="md" v-html="html(m)"></div>
            <div class="typing" v-if="streaming && i === current.messages.length - 1 && !m.error
                 && (m.image ? (m.attachments || []).length < (current.imageCount || 1) : !m.content)">
              <i></i><i></i><i></i><span class="dim" v-if="m.image">génération de l'image{{ (current.imageCount || 1) > 1
                ? " " + ((m.attachments || []).length + 1) + " / " + current.imageCount : "" }}…</span></div>
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
              <!-- Une image : ses actions ici ; plusieurs : dans l'image agrandie. -->
              <template v-if="m.image && m.attachments.length === 1 && m.attachments[0].data">
                <a class="link" :href="m.attachments[0].data" :download="m.attachments[0].name">Télécharger</a>
                <button type="button" class="link" v-if="m.attachments[0].data.startsWith('data:')"
                        :disabled="streaming" @click="retouch(m.attachments[0])">Retoucher</button>
              </template>
              <span v-else-if="m.image && m.attachments.length > 1">cliquer une image pour la télécharger ou la retoucher</span>
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
        <div class="attachments" v-if="pending.length || (current && current.folder)">
          <span class="chip folder" v-if="current && current.folder"
                :title="folderReady ? 'Le modèle peut lister, lire et chercher dans ce dossier' : 'Rouvrir le dossier pour que le modèle y accède'">
            <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h6l2 2h10v11H3z"/></svg>
            {{ current.folder.name }}/
            <span class="dim" v-if="folderReady">{{ num(folderCount) }} fichier{{ folderCount > 1 ? "s" : "" }}</span>
            <label class="link" v-else>à rouvrir<input type="file" webkitdirectory multiple hidden @change="onFolder"></label>
            <button type="button" class="icon" title="Fermer le dossier" @click="closeFolder">×</button>
          </span>
          <span v-for="(a, k) in pending" :key="k" class="chip" :class="{ file: a.kind !== 'image' }">
            <img v-if="a.kind === 'image'" :src="a.data" alt="">
            {{ a.name }} <span class="dim">{{ size(a.size) }}</span>
            <button type="button" class="icon" title="Retirer" @click="pending.splice(k, 1)">×</button>
          </span>
        </div>
        <textarea ref="input" v-model="draft" rows="1" @keydown="onKey" @paste="onPaste"
                  :placeholder="narrow ? (streaming ? 'Réponse en cours… (mise en file)' : imageMode ? 'Décrire l’image…' : 'Écrire un message…')
                    : streaming ? 'Réponse en cours — Entrée pour mettre un message en file'
                    : imageMode ? 'Décrire l’image à générer — Entrée pour envoyer'
                    : 'Écrire un message — Entrée pour envoyer'"
                  title="Entrée pour envoyer, Maj+Entrée pour aller à la ligne"></textarea>
        <div class="composer-bar">
          <div class="attach-menu">
            <button type="button" class="btn ghost attach ico-btn" aria-haspopup="menu"
                    :aria-expanded="String(attachOpen)" @click="attachOpen = !attachOpen"
                    title="Joindre : des fichiers (ou glisser-déposer, ou coller), ou un dossier que le modèle lira">
              <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11.5l-8.2 8.2a5 5 0 0 1-7-7L13 4.5a3.3 3.3 0 0 1 4.7 4.7l-8.2 8.2a1.7 1.7 0 0 1-2.4-2.4L14.6 7.5"/></svg>
              <span class="lbl">Joindre</span></button>
            <!-- v-show : les champs restent dans la page pendant que le
                 sélecteur du système est ouvert. -->
            <div class="menu" role="menu" v-show="attachOpen" @click="attachOpen = false">
              <label role="menuitem"><svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 3h9l3 3v15H6z"/></svg>
                <span>Fichiers…<small>images, textes : joints au message</small></span>
                <input type="file" multiple hidden @change="onPick"></label>
              <label role="menuitem"><svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h6l2 2h10v11H3z"/></svg>
                <span>Dossier…<small>le modèle le parcourt et lit ce qu'il lui faut</small></span>
                <input type="file" webkitdirectory multiple hidden @change="onFolder"></label>
            </div>
          </div>
          <label class="effort" v-if="imageMode" title="Nombre d'images par demande">
            <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z"/></svg>
            <select v-model.number="current.imageCount" :disabled="streaming" aria-label="Nombre d'images">
              <option v-for="k in 4" :key="k" :value="k">× {{ k }}</option>
            </select>
          </label>
          <label class="effort" v-if="imageMode" title="Taille de l'image (largeur × hauteur) ; plus grande, plus lente">
            <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16v14H4zM4 15l5-5 4 4 3-3 4 4"/></svg>
            <select v-model="current.imageSize" :disabled="streaming" aria-label="Taille de l'image">
              <option v-for="z in imageSizes" :key="z" :value="z">{{ z.replace("x", " × ") }}</option>
            </select>
          </label>
          <label class="effort" v-if="!imageMode" title="Niveau de réflexion du modèle : auto (celui du modèle), non, faible, moyenne, élevée">
            <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-3.5 10.9c.6.5 1 1.2 1 2.1h5c0-.9.4-1.6 1-2.1A6 6 0 0 0 12 3z"/></svg>
            <select v-model="current.reasoning" :disabled="streaming" aria-label="Réflexion">
              <option value="auto">auto</option>
              <option value="none">non</option>
              <option value="low">faible</option>
              <option value="medium">moyenne</option>
              <option value="high">élevée</option>
            </select>
          </label>
          <template v-if="imageMode"></template>
          <div class="range tools-toggle" v-else-if="toolGroups.length" role="group" aria-label="Outils hébergés">
            <button v-for="g in toolGroups" :key="g.type" type="button"
                    :disabled="current.incognito && g.type === 'memory'"
                    :aria-pressed="String(current.tools.includes(g.type) && !(current.incognito && g.type === 'memory'))"
                    :title="current.incognito && g.type === 'memory' ? 'Pas de mémoire en incognito'
                      : g.tools.join(', ')" @click="toggleTool(g.type)">{{ g.label }}</button>
          </div>
          <span class="dim" v-else-if="!chat.hosted_tools" title="[chat] hosted_tools = true dans config.toml">
            outils hébergés inactifs</span>
          <span class="status" :class="{ warn: (blind || unsent) && !status }">{{ status
            || (unsent ? "seule la première image est envoyée (à retoucher)"
            : blind ? "ce modèle ne lit peut-être pas les images" : "") }}</span>
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
    <teleport to="body">
    <div class="lightbox" v-if="zoom" @click.self="zoom = null" role="dialog" aria-label="Image agrandie">
      <img :src="zoom.data" :alt="zoom.name" @click="zoom = null">
      <div class="lightbox-bar">
        <span class="dim">{{ zoom.name }}</span>
        <a class="btn ghost" :href="zoom.data" :download="zoom.name">Télécharger</a>
        <button type="button" class="btn ghost" v-if="zoom.data.startsWith('data:')"
                :disabled="streaming" @click="retouch(zoom)">Retoucher</button>
        <button type="button" class="btn" @click="zoom = null">Fermer</button>
      </div>
    </div>
    </teleport>
  </div>`,
};
