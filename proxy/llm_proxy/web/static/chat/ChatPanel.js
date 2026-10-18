/*
  L'onglet « Chat » : un composant enregistré par app.js sous
  <chat-panel>, avec ses propres appels.

  Routes (même cookie que /ui/usage — la clé de la personne connectée) :
    GET  /ui/models?all=1       le catalogue (/v1/models), lib/catalog.js
    POST /ui/chat/completions   /v1/chat/completions, en flux ; ouvert par
                                [ui] chat = true dans config.toml
  Ce que la page sait du proxy (chat ouvert, outils hébergés déclarables)
  lui vient de /ui/session, par la prop `session`.

  CONVERSATIONS : gardées par le proxy, liées au compte connecté
  (chat/history.js, /ui/conversations ; leurs images sous /ui/assets).

  OUTILS HÉBERGÉS : un interrupteur par TYPE déclarable
  (`{"type": "web_search"}` présente web_search, web_fetch, web_grep). Le
  proxy exécute les appels et les annonce dans le flux (`x_hosted_tool`,
  propre au chat de la page). L'échange caché n'est jamais dans
  l'historique de la page : la mémoire du proxy ([chat].memory) le
  réinsère, tant que la réponse est renvoyée telle qu'elle a été reçue.

  FILE D'ATTENTE : pendant une réponse, Entrée met le message en file
  (conv.queue, gardée avec la conversation). Seule une réponse RÉUSSIE
  enchaîne sur le suivant ; un arrêt ou une erreur met la file en pause
  (« Envoyer la suite ») — enchaîner sur un échec n'aurait pas de sens.

  INCOGNITO : jamais envoyée à l'historique, disparaît dès qu'on la
  quitte (ou qu'on recharge). Ses requêtes portent `X-Incognito: 1` : le
  proxy n'en garde rien (ni échange d'outils, ni cache web, pas l'outil
  `memory`), et un fichier rendu ne vit que le temps de s'afficher. Les
  statistiques (des comptes, sans contenu) restent.

  MODÈLES D'IMAGES (`image` au catalogue) : le dernier message, précédé
  des demandes de la même suite d'images (chat/images.js), part à
  /ui/images/generations — ou /ui/images/edits si une image est jointe.
  Une requête par image, à la taille choisie ([tools.image_generation]
  .sizes) ; l'image rendue devient une pièce jointe de la réponse.

  CONTEXTE (chat/compact.js) : la jauge de la barre dit ce que la
  conversation occupe chez le modèle (sa dernière mesure, `usage`, sur
  la fenêtre du catalogue, `context`). /compact [consigne] la RÉSUME : un
  message `summary` remplace, chez le modèle, tout ce qui le précède (qui
  reste affiché). Automatique, au choix, avant un message quand le
  contexte dépasse AUTO_AT ; « Résumer et réessayer » sur une réponse
  refusée pour un contexte trop long.
*/
import { ChatStore } from "./store.js";
import { inline, dataUrl, ownImage } from "./history.js";
import { readAttachment } from "./attachments.js";
import { imagePrompt, oneImage } from "./images.js";
import { readStream } from "./stream.js";
import { partialString } from "../lib/partialjson.js";
import { availableBrowserModels, browserModel, modelCaps } from "./browser.js";
import { catalog, catalogState, refreshCatalog } from "../lib/catalog.js";
import * as workspace from "./workspace.js";
import * as notify from "./notify.js";
import * as tools from "./tools/index.js";
import * as artifactBridge from "./artifact-bridge.js";
import { ArtifactPanel } from "./ArtifactPanel.js";
import { download } from "../lib/files.js";
import * as voice from "./voice.js";
import { Markdown } from "../lib/markdown.js";
import { num, size } from "../lib/format.js";
import * as Compact from "./compact.js";

export const ChatPanel = {
  components: { ArtifactPanel },
  props: { session: { type: Object, required: true } },
  emits: ["unauthorized"],
  setup(props, { emit }) {
    const { ref, reactive, computed, watch, nextTick, onMounted, onUpdated, onUnmounted, toRaw } = Vue;
    const API = "/ui/chat/completions";
    // Tours d'outils de la page au plus, par réponse.
    const MAX_ROUNDS = 25;
    const LAST_MODEL = "llm-proxy-chat-model";
    const lastModel = VueUse.useLocalStorage(LAST_MODEL, "");
    const MEMORY_OPEN = "llm-proxy-chat-memory";
    // Noms lisibles des types d'outils ; un type inconnu garde le sien.
    const TOOL_LABELS = {
      web_search: "Web", code_execution: "Code", code_interpreter: "Code",
      image_generation: "Images", memory: "Mémoire", ocr: "OCR",
      transcribe: "Transcription", browser: "Navigateur piloté",
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

    // Les familles d'outils du menu (tools/) : celles que ce
    // navigateur sait faire, cochées par conversation ; une conversation
    // neuve reprend le dernier choix fait dans ce navigateur.
    const deviceFamilies = tools.menuFamilies();
    const devicePrefs = VueUse.useLocalStorage("llm-proxy-chat-device", null,
                                               { serializer: VueUse.StorageSerializers.object });
    const defaultDevice = () => (Array.isArray(devicePrefs.value) ? devicePrefs.value : tools.defaultFamilies());

    // ── état ────────────────────────────────────────────────────────────
    // Les conversations : le magasin partagé avec le menu latéral.
    const S = ChatStore;
    S.open(props.session.client);
    const current = S.current;
    const currentId = computed(() => S.state.currentId);
    const draft = ref("");
    const pending = ref([]);          // pièces jointes du message en cours
    const streaming = ref(false);
    const status = ref("");
    const settingsOpen = ref(false);
    const storageWarning = computed(() => S.state.storageWarning);
    // Des fichiers glissés sur le chat (useDropZone : entrées et sorties
    // des éléments enfants comptées).
    const dropZone = ref(null);
    const { isOverDropZone: dragging } = VueUse.useDropZone(dropZone, {
      onDrop: (files) => { if (files && files.length) addFiles(files); } });
    // Panneau mémoire : ouvert ou non (préférence de ce navigateur), et
    // un compteur qui le fait se relire après un appel de l'outil.
    const memoryOpen = VueUse.useLocalStorage(MEMORY_OPEN, false,
                                              { serializer: { read: (v) => v === "1", write: (v) => (v ? "1" : "0") } });
    const memoryStamp = ref(0);
    // Les artifacts de la conversation (show_artifact) : leur panneau, à la
    // place de celui de la mémoire — un seul panneau à la fois.
    const artifactsOpen = ref(false);
    const artifactId = ref("");
    function openArtifact(id) {
      artifactId.value = id;
      artifactsOpen.value = true;
      memoryOpen.value = false;
    }
    // Le modèle teste un artifact (inspect_artifact, run_in_artifact) : le
    // panneau s'ouvre dessus s'il ne l'est pas.
    artifactBridge.setOpener(openArtifact);
    function toggleArtifacts(open = !artifactsOpen.value) {
      artifactsOpen.value = open;
      if (open) memoryOpen.value = false;
    }
    // ── voix (voice.js) : écouter ───────────────────────────────
    // La synthèse vocale du navigateur : tout de suite, sans modèle. Le
    // choix (voix, débit) reste dans ce navigateur.
    const voicePrefs = reactive(voice.loadPrefs());
    watch(voicePrefs, (p) => voice.savePrefs({ ...p }), { deep: true });
    const voiceList = voice.voices;
    const voiceName = computed(() => {
      const v = voiceList.value.find((x) => x.uri === voicePrefs.voice);
      return v ? v.name : "voix du navigateur";
    });
    // Le message en cours de lecture (voice.state : une seule à la fois).
    const playing = computed(() => ({ id: voice.state.id.startsWith("msg:") ? voice.state.id.slice(4) : "" }));
    function listen(m) {
      if (playing.value.id === m.id) { voice.stop(); return; }
      try {
        voice.speak("msg:" + m.id, voice.speakable(m.final || m.content),
                    { voice: voicePrefs.voice, rate: voicePrefs.rate });
      } catch (e) {
        status.value = "Écoute : " + e.message;
      }
    }
    onUnmounted(() => { if (voice.state.id.startsWith("msg:")) voice.stop(); });

    // Prévenir de la fin d'une réponse (notify.js), réglage du navigateur.
    const notifyOn = ref(notify.enabled());
    async function toggleNotify(e) { notifyOn.value = await notify.setEnabled(e.target.checked); }
    function toggleMemory(open = !memoryOpen.value) {
      memoryOpen.value = open;
      if (open) artifactsOpen.value = false;
    }
    const thread = ref(null);
    // Écran étroit : textes courts (la zone de saisie, sur mobile).
    const narrow = VueUse.useMediaQuery("(max-width: 760px)");
    const input = ref(null);
    let controller = null;
    let pinned = true;                // le fil suit la fin de la réponse

    // Une réponse en cours : le menu ne change pas de conversation.
    watch(streaming, (v) => { S.state.streaming = v; });
    // Ce qu'une conversation neuve reçoit, et ce que fait le chat quand la
    // conversation change (le menu la choisit) : suivre la fin du fil, et
    // la zone de saisie prête pour une conversation neuve.
    S.setDefaults(() => ({ model: defaultModel(), tools: defaultTools(), device: defaultDevice() }));
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
    // Les modèles : ceux du catalogue du proxy (de conversation et
    // d'images ; lib/catalog.js, gardé dans le navigateur), puis ceux du
    // navigateur (browser.js), décrits avec ce qu'ils savent faire.
    const browserModels = ref([]);
    const models = computed(() => catalogState.data.filter((m) => m.chat || m.image)
      .sort((a, b) => a.id.localeCompare(b.id)).concat(browserModels.value));
    // Sans catalogue gardé, l'échec se dit ; avec, le bouton le rappelle.
    const modelsError = computed(() => catalogState.error && !catalogState.data.length
      ? "Catalogue illisible : " + catalogState.error : "");
    const modelsLoading = computed(() => catalogState.loading);
    const modelsTitle = computed(() => catalogState.loading ? "Lecture du catalogue…"
      : catalogState.error ? "Catalogue illisible (" + catalogState.error + ") — relire"
      : "Relire le catalogue" + (catalogState.at ? " (lu à " + new Date(catalogState.at)
        .toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" }) + ")" : ""));
    // L'entrée du modèle de la conversation, et ce qu'il sait faire : le
    // chat n'en connaît que cela (outils, réflexion, dossier, réponse).
    const modelEntry = (id) => models.value.find((m) => m.id === id) || browserModel(id);
    const caps = computed(() => modelCaps(current.value && modelEntry(current.value.model)));
    // La liste, groupée par fournisseur (le préfixe « fournisseur/ » de
    // l'identifiant, ou le `group` de l'entrée) : sous chaque groupe, le
    // seul nom du modèle. La conversation d'abord, puis le navigateur,
    // puis les images.
    const modelGroups = computed(() => {
      const groups = new Map();
      for (const m of models.value) {
        const cut = m.id.indexOf("/");
        const from = m.group || (cut > 0 ? m.id.slice(0, cut) : "Autres");
        const label = m.image ? "Images · " + from : from;
        if (!groups.has(label)) groups.set(label, { label, rank: m.image ? 2 : m.browser ? 1 : 0, models: [] });
        const name = m.name || (cut > 0 ? m.id.slice(cut + 1) : m.id);
        const notes = [m.vision && !m.image && "vision",
                       m.availability && m.availability !== "available" && "à télécharger"];
        groups.get(label).models.push({ id: m.id, text: [name, ...notes.filter(Boolean)].join(" · ") });
      }
      return [...groups.values()].sort((a, b) => a.rank - b.rank);
    });
    const isImage = (id) => !!(models.value.find((m) => m.id === id) || {}).image;
    const imageMode = computed(() => !!current.value && isImage(current.value.model));
    const imageSizes = computed(() => chat.value.image_sizes || ["512x512"]);
    // Le modèle d'une nouvelle conversation : celui que le proxy
    // présélectionne ([ui].chat_model), s'il est au catalogue ; sinon le
    // dernier choisi dans ce navigateur, sinon le premier.
    function defaultModel() {
      const preset = chat.value.model;
      if (preset && modelIds.value.includes(preset)) return preset;
      const last = lastModel.value;
      if (last && modelIds.value.includes(last)) return last;
      const first = models.value.find((m) => !m.image && m.byDefault !== false);
      return (first && first.id) || last || "";
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
      // Chrome peut mettre quelques secondes à dire ses modèles : le
      // catalogue ne les attend pas.
      availableBrowserModels().then((got) => { browserModels.value = got; });
      try {
        await catalog();
        if (current.value && !current.value.model) current.value.model = defaultModel();
      } catch (e) {
        if (e.status === 401) emit("unauthorized");
      }
    }
    async function reloadModels() {
      availableBrowserModels().then((got) => { browserModels.value = got; });
      try { await refreshCatalog(); } catch (e) { if (e.status === 401) emit("unauthorized"); }
    }

    watch(() => current.value && current.value.model, (m) => {
      if (m && !isImage(m)) lastModel.value = m;
    });

    // Incognito : la conversation vide change de genre ; sinon, une neuve
    // de l'autre genre (l'incognito quittée disparaît).
    function toggleIncognito() {
      const c = current.value;
      if (!c || streaming.value) return;
      if (!c.messages.length) c.incognito = !c.incognito;
      else S.newConv(!c.incognito);
    }

    const deviceOn = computed(() => (current.value && current.value.device) || defaultDevice());
    function toggleDevice(key) {
      const c = current.value;
      if (!c) return;
      const on = deviceOn.value;
      c.device = on.includes(key) ? on.filter((k) => k !== key) : [...on, key];
      devicePrefs.value = c.device;
    }
    // Le menu « Outils » : ceux du proxy (hébergés) et ceux du navigateur,
    // ce que le modèle sait utiliser (`caps`), cochés par conversation.
    const hostedShown = computed(() => (caps.value.hostedTools ? toolGroups.value : []));
    const devicesShown = computed(() => (caps.value.pageTools ? [] : deviceFamilies));
    const hostedOn = (g) => !!current.value && current.value.tools.includes(g.type)
      && !(current.value.incognito && g.type === "memory");
    const activeTools = computed(() => [
      ...hostedShown.value.filter(hostedOn).map((g) => g.label),
      ...devicesShown.value.filter((f) => deviceOn.value.includes(f.key)).map((f) => f.label)]);
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
      status.value = "";
    }
    // Chrome, Edge : le sélecteur du système, qui permet d'écrire ensuite.
    const canPick = workspace.canPick();
    async function pickFolder() {
      attachOpen.value = false;
      const c = current.value;
      if (!c) return;
      try {
        const ws = await workspace.pickFolder();
        if (!ws.count) { status.value = `${ws.name} : aucun fichier lisible.`; return; }
        folders.set(c.id, ws);
        c.folder = { name: ws.name, count: ws.count };
        folderTick.value++;
        status.value = "";
      } catch (e) {
        if (e.name !== "AbortError") status.value = "Dossier : " + e.message;
      }
    }
    function closeFolder() {
      const c = current.value;
      if (!c) return;
      folders.delete(c.id);
      delete c.folder;
      folderTick.value++;
    }
    // Les menus « Joindre » (fichiers, ou un dossier) et « Outils » : se
    // ferment au clic dehors.
    const attachOpen = ref(false);
    const toolsOpen = ref(false);
    // L'outil `browser` agit par l'extension du compte : le menu dit si
    // elle est connectée (relu à chaque ouverture).
    const browserLink = ref(null);
    watch(toolsOpen, async (open) => {
      if (!open || !available.value.has("browser")) return;
      try { browserLink.value = await (await fetch("/ui/browser/link")).json(); } catch { browserLink.value = null; }
    });
    function onDocClick(e) {
      if (attachOpen.value && !e.target.closest(".attach-menu:not(.tools-menu)")) attachOpen.value = false;
      if (toolsOpen.value && !e.target.closest(".tools-menu")) toolsOpen.value = false;
      if (ctxOpen.value && !e.target.closest(".ctx-menu")) ctxOpen.value = false;
    }
    VueUse.useEventListener(document, "click", onDocClick);
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

    // `guidance` : les consignes des outils actifs (tools.guidance), à la
    // suite du prompt système de la conversation.
    // Après un RÉSUMÉ (compact.js) : lui, au prompt système, et ce qui le
    // suit — pas ce qui le précède.
    function apiMessages(conv, upto, guidance = "") {
      const out = [];
      const from = Compact.start(conv, upto);
      const summary = from ? Compact.preamble(conv.messages[from - 1]) : "";
      const system = [conv.system.trim(), summary, guidance].filter(Boolean).join("\n\n");
      if (system) out.push({ role: "system", content: system });
      for (const m of conv.messages.slice(from, upto)) {
        if (m.role === "summary") continue;
        if (m.role === "user") out.push({ role: "user", content: apiContent(m) });
        // Une réponse en erreur ou vide n'est pas renvoyée : le modèle ne
        // l'a jamais écrite.
        else if ((m.steps || []).length) {
          // Une réponse qui a lu le dossier : ses appels et leurs résultats,
          // puis son dernier texte — le modèle retrouve ce qu'il a lu.
          out.push(...tools.stepMessages(m.steps, { past: true }));
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
      // /compact [consigne] : résumer, rien n'est envoyé comme message.
      const cmd = draft.value.trim().match(/^\/compact(?:\s+([\s\S]*))?$/i);
      if (cmd && !pending.value.length) {
        if (streaming.value) { status.value = "Le résumé attendra la fin de la réponse : /compact ensuite."; return; }
        draft.value = "";
        compact(c, (cmd[1] || "").trim());
        return;
      }
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
      abandonWaiting(c);
      ask(c, item);
    }

    async function ask(c, item) {
      // Le contexte presque plein : résumer d'abord ce qui précède le
      // message (affiché tout de suite), puis l'envoyer.
      const near = autoCompact.value && contextNear(c);
      c.messages.push({ id: item.id, role: "user", content: item.content,
                        attachments: item.attachments });
      if (c.messages.length === 1) {
        const first = item.content || item.attachments.map((a) => a.name).join(", ");
        c.title = first.replace(/\s+/g, " ").slice(0, 60) + (first.length > 60 ? "…" : "");
      }
      pinned = true;
      if (near && !(await compact(c, "", true, c.messages.length - 1))) {
        if (!streaming.value) respond(c);
        return;
      }
      respond(c);
    }

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

    // ── une réponse ─────────────────────────────────────────────────────
    // Un tour par requête : tant que le modèle appelle des outils de la
    // PAGE (tools/), elle les exécute et le relance avec leurs
    // résultats. Un outil qui attend l'utilisateur (une carte) MET EN PAUSE : la boucle s'arrête, l'état reste dans le message
    // (`step` : le tour en cours, `waiting` : la carte), et la réponse de
    // l'utilisateur la relance (respond(conv, message)) — même après un
    // rechargement.
    async function respond(conv, resumeMsg = null) {
      if (!resumeMsg && isImage(conv.model)) return generate(conv);
      if (!resumeMsg) {
        conv.messages.push({ id: uid(), role: "assistant", content: "", reasoning: "",
                             tools: [], sources: [], model: conv.model, usage: null,
                             error: "", started: Date.now(), duration: 0 });
      }
      // Le message RÉACTIF : c'est lui que le flux modifie.
      const live = resumeMsg || conv.messages[conv.messages.length - 1];
      const upto = conv.messages.indexOf(live);
      conv.updated = Date.now();
      streaming.value = true;
      status.value = "";
      const ctrl = controller = new AbortController();
      // Un modèle qui répond lui-même, sur l'appareil (browser.js) : sans
      // le proxy ; la question à choix y est la même carte, la même pause.
      const entry = modelEntry(conv.model);
      if (entry && entry.answer) {
        let paused = false;
        try {
          paused = await deviceTurns(conv, live, upto, ctrl, entry.answer);
        } catch (e) {
          if (e.name !== "AbortError") live.error = "Modèle du navigateur : " + e.message;
          else if (!live.content) live.error = "Arrêté.";
        } finally {
          status.value = "";
          done(conv, live, ctrl, paused);
        }
        return;
      }
      const body = { model: conv.model, stream: true, stream_options: { include_usage: true } };
      if (conv.temperature !== null && conv.temperature !== "") {
        body.temperature = Number(conv.temperature);
      }
      // Le niveau de réflexion. `reasoning_effort` est compris de llama.cpp
      // (il ACTIVE la réflexion d'un Qwen3, éteinte par défaut) et de vLLM
      // (gpt-oss : courte ou longue) ; « non » coupe celle d'un modèle
      // hybride par son gabarit (enable_thinking) — gpt-oss, lui, réfléchit
      // toujours un peu. « auto » : rien n'est envoyé.
      const effort = modelCaps(modelEntry(conv.model)).reasoning ? conv.reasoning || "auto" : "auto";
      if (["low", "medium", "high"].includes(effort)) body.reasoning_effort = effort;
      else if (effort === "none") body.chat_template_kwargs = { enable_thinking: false };
      // Incognito : rien de persistant, donc pas l'outil `memory`.
      // Ce que le modèle ne sait pas faire (`caps`) n'est pas envoyé.
      const can = modelCaps(modelEntry(conv.model));
      const hosted = can.hostedTools ? conv.tools.filter((t) => available.value.has(t)
        && !(conv.incognito && t === "memory")) : [];
      // Les outils de la page (tools/) : ceux des familles actives — le
      // dossier s'il est ouvert —, et leurs consignes au prompt système.
      const ctx = toolCtx(conv, live);
      if (!can.folder) ctx.ws = null;
      const families = conv.device || defaultDevice();
      body.tools = [...hosted.map((type) => ({ type })), ...tools.specs(ctx, families)
        .filter((t) => !can.pageTools || can.pageTools.includes(t.function.name))];
      const guidance = tools.guidance(ctx, families);
      let paused = false;
      try {
        for (;;) {
          // Un tour à demander au modèle — sauf à la reprise d'une pause.
          if (!(live.step && live.step.queue.length)) {
            if ((live.steps || []).length >= MAX_ROUNDS) {
              live.error = `Arrêté après ${MAX_ROUNDS} tours d'outils.`;
              break;
            }
            const start = live.content.length;
            body.messages = await inline([...apiMessages(conv, upto, guidance), ...tools.stepMessages(live.steps)]);
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
            await readStream(r.body, live, { calls, onCall: (slot) => writing(live, slot),
                                             onMemory: () => { memoryStamp.value++; } });
            const asked = calls.filter((c) => c && tools.isTool(c.name))
              .map((c) => ({ id: c.id || "call_" + uid(), name: c.name, arguments: c.arguments }));
            if (!asked.length || live.error) {
              if (live.steps) live.final = live.content.slice(start);
              break;
            }
            live.step = { content: live.content.slice(start), calls: [], queue: asked };
          }
          while (live.step.queue.length) {
            const c = live.step.queue[0];
            let chip = live.tools.find((t) => t.id === c.id);
            if (!chip) {
              live.tools.push({ id: c.id, name: c.name, arguments: c.arguments, status: "running" });
              chip = live.tools[live.tools.length - 1];
            } else if (chip.status === "writing") {
              Object.assign(chip, { arguments: c.arguments, status: "running" });
            }
            let result;
            let image = "";
            // (Une carte déjà montrée reste la réponse, même si l'attente
            // ne serait plus nécessaire : une permission donnée depuis.)
            if ((live.waiting && live.waiting.id === c.id)
                || tools.waits(c.name, tools.parseArgs(c.arguments), ctx)) {
              if (live.waiting && live.waiting.id === c.id) {
                // La carte de cet appel : répondue (reprise), ou encore en attente.
                if (live.waiting.result === undefined) { paused = true; return; }
                result = live.waiting.result;
                image = live.waiting.image || "";
                live.waiting = null;
              } else {
                const card = await tools.prepare(c.name, c.arguments, ctx);
                if (card.error) result = card.error;
                else {
                  live.waiting = { id: c.id, ...card };
                  chip.status = "waiting";
                  paused = true;
                  notify.notify(tools.notice(live.waiting), conv.title);
                  return;
                }
              }
            } else {
              const got = await tools.run(c.name, c.arguments, ctx);
              result = got.result;
              image = got.image || "";
              if (got.artifact) openArtifact(got.artifact);
            }
            Object.assign(chip, { status: result.startsWith("Error:") ? "error" : "done",
                                  chars: result.length, preview: result.slice(0, 600) });
            live.step.calls.push({ id: c.id, name: c.name, arguments: c.arguments, result,
                                   ...(image ? { image } : {}) });
            live.step.queue.shift();
          }
          (live.steps || (live.steps = [])).push({ content: live.step.content, calls: live.step.calls });
          live.step = null;
          if (live.content && !live.content.endsWith("\n")) live.content += "\n\n";
        }
      } catch (e) {
        if (e.name !== "AbortError") live.error = "Connexion interrompue : " + e.message;
        else if (!live.content) live.error = "Arrêté.";
      } finally {
        done(conv, live, ctrl, paused);
      }
    }

    // Les tours d'un modèle de l'appareil (`answer`) : sa réponse, et s'il
    // pose des questions, la carte d'ask_user (un appel d'outil comme un
    // autre dans live.step / live.steps : un autre modèle relit l'échange
    // tel quel). Rend true quand une carte attend.
    async function deviceTurns(conv, live, upto, ctrl, answer) {
      for (;;) {
        if (!(live.step && live.step.queue.length)) {
          if ((live.steps || []).length >= MAX_ROUNDS) {
            live.error = `Arrêté après ${MAX_ROUNDS} questions.`;
            return false;
          }
          const start = live.content.length;
          const msgs = await inline([...apiMessages(conv, upto), ...tools.stepMessages(live.steps)]);
          const args = await answer(msgs, live, ctrl.signal, (s) => { status.value = s; });
          if (!args) {
            if (live.steps) live.final = live.content.slice(start);
            return false;
          }
          live.step = { content: live.content.slice(start), calls: [],
                        queue: [{ id: "call_" + uid(), name: tools.TOOL.ASK_USER, arguments: JSON.stringify(args) }] };
        }
        const c = live.step.queue[0];
        let chip = live.tools.find((t) => t.id === c.id);
        if (!chip) {
          live.tools.push({ id: c.id, name: c.name, arguments: c.arguments, status: "running" });
          chip = live.tools[live.tools.length - 1];
        }
        let result;
        if (live.waiting && live.waiting.id === c.id) {
          if (live.waiting.result === undefined) return true;
          result = live.waiting.result;
          live.waiting = null;
        } else {
          const card = await tools.prepare(c.name, c.arguments, toolCtx(conv, live));
          if (card.error) result = card.error;
          else {
            live.waiting = { id: c.id, ...card };
            chip.status = "waiting";
            notify.notify(tools.notice(live.waiting), conv.title);
            return true;
          }
        }
        Object.assign(chip, { status: result.startsWith("Error:") ? "error" : "done",
                              chars: result.length, preview: result.slice(0, 600) });
        live.step.calls.push({ id: c.id, name: c.name, arguments: c.arguments, result });
        live.step.queue.shift();
        (live.steps || (live.steps = [])).push({ content: live.step.content, calls: live.step.calls });
        live.step = null;
        if (live.content && !live.content.endsWith("\n")) live.content += "\n\n";
      }
    }

    // Un appel de la page qui s'écrit encore (un artifact peut prendre des
    // minutes) : sa puce paraît dès son nom, « écriture… », et compte ce
    // qui s'écrit — mise à jour quatre fois par seconde au plus.
    function writing(live, slot) {
      if (!slot.name || !tools.isTool(slot.name)) return;
      if (!slot.id) slot.id = "call_" + uid();
      let chip = live.tools.find((t) => t.id === slot.id);
      if (!chip) {
        live.tools.push({ id: slot.id, name: slot.name, arguments: "", status: "writing", chars: 0, at: 0 });
        chip = live.tools[live.tools.length - 1];
      }
      const now = Date.now();
      if (chip.status !== "writing" || now - chip.at < 250) return;
      chip.at = now;
      chip.chars = slot.arguments.length;
      // Le titre (ou le nom, le chemin) : au début des arguments.
      if (!chip.label) {
        const head = slot.arguments.slice(0, 2000);
        chip.label = partialString(head, "title") || partialString(head, "name")
          || partialString(head, "path") || partialString(head, "id") || "";
      }
    }

    // ── les cartes ─────────────────────────────────────────────────────
    // Un outil qui attend l'utilisateur montre une CARTE (m.waiting) : le
    // gabarit commun (tools.view), ou le composant de l'outil — dans le
    // fil, ou dans la feuille du bas. Sa réponse devient le résultat de
    // l'outil, et la réponse du modèle reprend.

    // Ce que les outils reçoivent du tour : la conversation, le message,
    // le dossier ouvert, ce que le modèle sait lire, un téléchargement.
    function toolCtx(conv, live) {
      return { conv, live, ws: folders.get(conv.id) || null,
               vision: !!(modelEntry(conv.model) || {}).vision, download };
    }
    function settle(m, result) {
      const c = current.value;
      if (!c || streaming.value || !m.waiting) return;
      m.waiting.result = result;
      respond(c, m);
    }
    // Une action de la carte (un bouton) : l'API de l'appareil part de ce
    // clic (le geste que le navigateur exige). Répondue, elle rend un
    // résultat, et peut-être une image (capture, photo) : jointe à la
    // réponse, et envoyée au modèle avec le résultat. Une erreur levée
    // s'affiche ; la carte reste.
    async function cardAct(m, action, payload) {
      const w = m.waiting;
      if (!w || w.busy || streaming.value) return;
      const c = current.value;
      let out = null;
      w.busy = true;
      try {
        out = await tools.act(w, action, toolCtx(c, m), payload);
      } catch (e) {
        status.value = e.message;
      } finally {
        w.busy = false;
      }
      if (!out || m.waiting !== w) return;
      tools.release(w);
      if (out.image) {
        (m.attachments || (m.attachments = [])).push({ kind: "image", name: out.name || "image.jpg", data: out.image });
        w.image = out.image;
      }
      settle(m, out.result);
    }
    const cardView = (w) => tools.view(w);
    // Les cartes DURABLES (m.kept) : laissées sous la réponse par un outil,
    // leurs boutons se rejouent à volonté ; la note d'un clic (« Partagé. »)
    // ne dure que le temps de la page.
    const keptView = (k) => tools.keptView(k);
    const keptNotes = reactive({});
    async function keptAct(m, k, action) {
      if (keptNotes[k.id] === "…") return;
      keptNotes[k.id] = "…";
      try {
        keptNotes[k.id] = (await tools.keptAct(k, action, toolCtx(current.value, m))) || "";
      } catch (e) {
        keptNotes[k.id] = e.message;
      }
    }
    const cardComponent = (w) => tools.component(w);
    const cardPlacement = (w) => tools.placement(w);
    const cardPending = (w) => tools.pending(w);
    const folderCtx = () => (folderTick.value, current.value ? toolCtx(current.value, null) : {});
    // La carte de la feuille du bas (la dernière réponse), et la feuille
    // ouverte ou réduite : rouverte à chaque nouvelle carte.
    const sheet = computed(() => {
      const ms = (current.value && current.value.messages) || [];
      const m = ms[ms.length - 1];
      return m && m.waiting && tools.placement(m.waiting) === "sheet" ? m : null;
    });
    const sheetOpen = ref(true);
    watch(() => sheet.value && sheet.value.waiting.id, (id) => { if (id) sheetOpen.value = true; });
    // Le badge d'un fichier : son extension, en capitales (« TXT » sans).
    const extOf = (name) => ((String(name).match(/\.([a-z0-9]{1,6})$/i) || [])[1] || "txt").toUpperCase();
    // Un fichier sous la réponse : son texte, ou ce que l'outil qui l'a
    // produit en relit au moment du clic (la base SQL de la conversation).
    async function downloadOut(f) {
      const content = await tools.fileContent(f);
      if (content === null || content === undefined) {
        status.value = `${f.name} : introuvable dans ce navigateur (supprimé, ou créé sur un autre).`;
        return;
      }
      download({ name: f.name, content }, f.type);
    }
    // Un nouveau message pendant qu'une carte attend : elle est close sans
    // réponse, et le tour en cours avec elle (ses autres appels aussi).
    function abandonWaiting(c) {
      const m = c.messages[c.messages.length - 1];
      if (!m || !m.waiting || !m.step) return;
      const waited = tools.abandon(m.waiting);
      for (const q of m.step.queue) {
        const result = q.id === m.waiting.id ? waited : "Non exécuté : l'utilisateur est passé à autre chose.";
        m.step.calls.push({ ...q, result });
        const chip = m.tools.find((t) => t.id === q.id);
        if (chip) Object.assign(chip, { status: "error", preview: result, chars: result.length });
      }
      (m.steps || (m.steps = [])).push({ content: m.step.content, calls: m.step.calls });
      m.step = null;
      m.waiting = null;
    }

    function done(conv, live, ctrl, paused = false) {
      live.duration = (Date.now() - live.started) / 1000;
      for (const t of live.tools) if (t.status === "running" || t.status === "writing") t.status = "error";
      streaming.value = false;
      controller = null;
      conv.updated = Date.now();
      if (paused) return;            // une carte attend : la file aussi
      if (!ctrl.signal.aborted && !live.error) {
        notify.notify("Réponse terminée", (live.final || live.content || conv.title).trim());
      }
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

    // ── contexte : la jauge, le résumé (compact.js) ─────────────────────
    const autoCompact = VueUse.useLocalStorage("llm-proxy-auto-compact", true);
    const ctxOpen = ref(false);
    const contextMax = (conv) => { const e = conv && modelEntry(conv.model); return (e && e.context) || 0; };
    const context = computed(() => {
      const c = current.value;
      if (!c || !c.messages.length || imageMode.value) return null;
      return Compact.occupancy(c, contextMax(c));
    });
    // Le premier message encore envoyé au modèle : ceux d'avant sont résumés.
    const sentFrom = computed(() => (current.value ? Compact.start(current.value, current.value.messages.length) : 0));
    const contextNear = (conv) => {
      const o = Compact.occupancy(conv, contextMax(conv));
      return !!o.max && o.ratio >= Compact.AUTO_AT && conv.messages.slice(Compact.start(conv, conv.messages.length))
        .some((m) => m.role === "assistant");
    };
    const contextTitle = computed(() => {
      const o = context.value;
      if (!o) return "";
      const what = (o.measured ? "" : "≈ ") + num(o.tokens) + " tokens"
        + (o.max ? ` sur ${num(o.max)} (${Math.round(o.ratio * 100)} %)` : " (fenêtre du modèle inconnue)");
      return `Contexte : ${what} — /compact pour résumer la conversation`;
    });
    // Un contexte refusé par le backend (trop long) : de quoi le dire.
    const tooLong = (m) => /context.{0,40}(length|window|size|limit)|contexte|too long|too many tokens|maximum.*(length|tokens)|exceeds?.*(context|tokens)/i.test(m.error || "");

    // Résumer la conversation jusqu'à `upto` (exclu) : un message
    // `summary` y est inséré, rempli par le modèle de la conversation, sans
    // outils. Rend true s'il a réussi ; sinon il disparaît, l'erreur dite.
    async function compact(conv, focus = "", auto = false, upto = conv.messages.length) {
      if (streaming.value) return false;
      const from = Compact.start(conv, upto);
      const covered = conv.messages.slice(from, upto).filter((m) => m.role !== "summary").length;
      if (!conv.messages.slice(from, upto).some((m) => m.role === "assistant")) {
        status.value = "Rien à résumer depuis le dernier résumé.";
        return false;
      }
      const before = Compact.occupancy({ ...conv, messages: conv.messages.slice(0, upto) }, contextMax(conv)).tokens;
      conv.messages.splice(upto, 0, { id: uid(), role: "summary", content: "", reasoning: "", tools: [],
        sources: [], model: conv.model, usage: null, error: "", started: Date.now(), duration: 0,
        running: true, auto, focus, covered, before });
      const s = conv.messages[upto];
      streaming.value = true;
      status.value = auto ? "Contexte presque plein : résumé de la conversation…" : "Résumé de la conversation…";
      const ctrl = controller = new AbortController();
      try {
        const msgs = await inline(apiMessages(conv, upto));
        const entry = modelEntry(conv.model);
        const once = async (list) => {
          if (entry && entry.answer) {
            await entry.answer(list, s, ctrl.signal, (t) => { status.value = t; });
            return;
          }
          const body = { model: conv.model, stream: true, stream_options: { include_usage: true }, messages: list };
          // Un résumé n'a pas besoin de réflexion (un modèle hybride s'en passe).
          if (modelCaps(entry).reasoning) body.chat_template_kwargs = { enable_thinking: false };
          const r = await fetch(API, {
            method: "POST", signal: ctrl.signal,
            headers: { "content-type": "application/json", accept: "text/event-stream",
                       ...(conv.incognito ? { "x-incognito": "1" } : {}) },
            body: JSON.stringify(body),
          });
          if (r.status === 401) { emit("unauthorized"); throw new Error("session expirée"); }
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
          // Le contexte déborde déjà : l'échange allégé, une fois.
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
        controller = null;
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
    // « Annuler le résumé » : l'historique repart en entier.
    function uncompact(i) {
      const c = current.value;
      if (!c || streaming.value || !c.messages[i] || c.messages[i].role !== "summary") return;
      c.messages.splice(i, 1);
    }
    // Une réponse refusée (contexte trop long) : résumer ce qui précède la
    // question, puis la reposer.
    async function retryCompacted() {
      const c = current.value;
      if (!c || streaming.value) return;
      const last = c.messages[c.messages.length - 1];
      if (last && last.role === "assistant") c.messages.pop();
      const q = c.messages.length - 1;
      if (q < 0 || c.messages[q].role !== "user") return;
      if (await compact(c, "", true, q)) respond(c);
    }

    // Les commandes de la zone de saisie : « / » les propose.
    const COMMANDS = [{ name: "/compact", args: "[consigne]",
                        hint: "résumer la conversation pour libérer du contexte (les messages restent affichés)" }];
    const commandHints = computed(() => {
      const d = draft.value;
      if (!d.startsWith("/") || /\s/.test(d) || imageMode.value) return [];
      return COMMANDS.filter((c) => c.name.startsWith(d.toLowerCase()));
    });
    function useCommand(c) {
      draft.value = c.name + " ";
      nextTick(() => { if (input.value) input.value.focus(); });
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
            ...(source ? { image: await dataUrl(source.data) } : {}) }, ctrl.signal, () => emit("unauthorized"));
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
    VueUse.onKeyStroke("Escape", () => { if (zoom.value) zoom.value = null; });

    function stop() { if (controller) controller.abort(); }

    // La dernière réponse est remplacée, la question reste.
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
      if (e.key === "Tab" && commandHints.value.length && draft.value !== commandHints.value[0].name + " ") {
        e.preventDefault();
        useCommand(commandHints.value[0]);
        return;
      }
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

    // Le CORPS d'une réponse : son texte, et les cartes durables qui s'y
    // placent (un graphique interactif : celles à `slot` et à composant).
    // Le modèle en place une en écrivant, sur une ligne seule,
    // ![titre](chart:<slot>) ; une carte qu'il ne place pas va en tête.
    const SLOT_MARK = /^[ \t]*!\[[^\]\n]*\]\((?:chart|card):([\w.-]+)\)[ \t]*$/gm;
    const slotted = (m) => (m.kept || []).filter((k) => k.slot && tools.keptComponent(k));
    function body(m) {
      const cards = slotted(m);
      if (!cards.length) return [{ html: html(m) }];
      const raw = toRaw(m);
      let entry = cache.get(raw);
      if (!entry) cache.set(raw, entry = {});
      const src = m.content || "";
      const key = src + "\u0000" + cards.map((k) => k.id).join(",");
      if (entry.body && entry.body.key === key) return entry.body.parts;
      const bySlot = new Map(cards.map((k) => [k.slot, k]));
      const used = new Set();
      const parts = [];
      let last = 0;
      for (const hit of src.matchAll(SLOT_MARK)) {
        const k = bySlot.get(hit[1]);
        if (!k || used.has(k.id)) continue;
        parts.push({ html: Markdown.render(src.slice(last, hit.index)) }, { k });
        used.add(k.id);
        last = hit.index + hit[0].length;
      }
      parts.push({ html: Markdown.render(src.slice(last)) });
      const out = [...cards.filter((k) => !used.has(k.id)).map((k) => ({ k })),
                   ...parts.filter((p) => p.k || p.html.trim())];
      entry.body = { key, parts: out };
      return out;
    }
    const keptComponent = (k) => tools.keptComponent(k);
    const keptBelow = (m) => (m.kept || []).filter((k) => !(k.slot && tools.keptComponent(k)));

    // Copier : le bouton des blocs de code (rendus en v-html, donc par
    // délégation) et celui de chaque réponse.
    const clipboard = VueUse.useClipboard({ legacy: true });
    async function copyText(text, button) {
      try {
        await clipboard.copy(text);
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
    onUnmounted(() => clearInterval(pace));

    // Ce que l'appel d'outil cherchait, en une ligne.
    function argSummary(t) {
      if (t.status === "writing") return t.label || "";
      let a = {};
      try { a = JSON.parse(t.arguments || "{}"); } catch { return t.arguments; }
      // Les outils de la page : ce qui les distingue, lisible (leur `summary`).
      if (tools.isTool(t.name)) return tools.summary(t.name, a);
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
      await S.open(props.session.client);
      if (!S.state.convs.length) S.newConv();
      await loadModels();
      for (const c of S.state.convs) {
        if (!c.model) c.model = defaultModel();
        if (!c.imageSize) c.imageSize = chat.value.image_size;
        if (!c.imageCount) c.imageCount = 1;
        if (!c.reasoning) c.reasoning = "auto";
        // Une carte du navigateur en cours au rechargement : ses boutons
        // reviennent (le flux de caméra, lui, est perdu).
        for (const m of c.messages) if (m.waiting && m.waiting.kind === "device") m.waiting.busy = false;
      }
      nextTick(() => {
        if (thread.value) thread.value.scrollTop = thread.value.scrollHeight;
        autosize();
      });
    });

    return { chat, toolGroups, available, current, currentId,
             ownImage, models, modelGroups, modelsError, modelsLoading, modelsTitle, reloadModels, caps, blind, unsent, imageMode, imageSizes, toggleIncognito, zoom, retouch, exportMd: S.exportMd, draft, pending, streaming, status,
             settingsOpen,
             storageWarning, dragging, narrow, memoryOpen, memoryStamp, toggleMemory, thread, input, canSend,
             toggleTool, onPick, onFolder, closeFolder, folderReady, folderCount, attachOpen,
             toolsOpen, browserLink, hostedShown, devicesShown, hostedOn, activeTools, deviceOn, toggleDevice,
             canPick, pickFolder, artifactsOpen, artifactId, openArtifact, toggleArtifacts,
             notifyOn, toggleNotify, notifySupported: notify.supported(),
             voicePrefs, voiceList, voiceName, voiceSupported: voice.supported, playing, listen,
             sheet, sheetOpen, cardAct, cardView, keptView, keptAct, keptNotes, body, keptComponent, keptBelow, cardComponent, cardPlacement, cardPending, folderCtx,
             download, downloadOut, extOf,
             dropZone, onPaste, send, stop, queue, resume, unqueue, regenerate, edit, onKey, html,
             copyText, onThreadClick, onScroll, onImageLoad, argSummary, preview, num, size, when,
             host, context, contextTitle, sentFrom, autoCompact, ctxOpen, compactNow, uncompact,
             retryCompacted, tooLong, commandHints, useCommand, kTokens: Compact.kTokens, estimate: Compact.estimate,
             AUTO_AT: Compact.AUTO_AT };
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
       :class="{ 'with-memory': (memoryOpen && $props.session.memory) || (artifactsOpen && current && (current.artifacts || []).length),
                 'side-wide': artifactsOpen, incognito: current && current.incognito }">
    <section class="chat-main" v-if="current" ref="dropZone">
      <header class="chat-head">
        <h2 :title="current.title">{{ current.title }}</h2>
        <label class="model-pick" title="Modèle">
          <select v-model="current.model" :disabled="streaming" :title="current.model">
            <option v-if="!models.length && current.model" :value="current.model">{{ modelsLoading ? "Chargement des modèles…" : current.model }}</option>
            <option v-if="!models.length && !current.model" value="" disabled>{{ modelsLoading ? "Chargement des modèles…" : "Aucun modèle" }}</option>
            <option v-if="current.model && models.length && !models.some((m) => m.id === current.model)"
                    :value="current.model">{{ current.model }} ({{ modelsLoading ? "chargement…" : "absent du catalogue" }})</option>
            <optgroup v-for="g in modelGroups" :key="g.label" :label="g.label">
              <option v-for="m in g.models" :key="m.id" :value="m.id">{{ m.text }}</option>
            </optgroup>
          </select>
        </label>
        <button type="button" class="btn ghost reload-models" :class="{ busy: modelsLoading }"
                :disabled="modelsLoading" :title="modelsTitle" :aria-label="modelsTitle" @click="reloadModels">
          <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 12a8 8 0 1 1-2.3-5.7M20 4v5h-5"/></svg></button>
        <button type="button" class="btn ghost ico-btn incognito-btn" :disabled="streaming"
                :aria-pressed="String(!!current.incognito)" @click="toggleIncognito"
                :title="current.incognito ? 'Quitter le mode incognito' : 'Conversation incognito : rien n’est gardé'">
          <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M2 11h20M5 11l2.5-6h9l2.5 6M10 16.5c1.3-.8 2.7-.8 4 0"/><circle cx="7" cy="16.5" r="3"/><circle cx="17" cy="16.5" r="3"/></svg>
          <span class="lbl">Incognito</span></button>
        <button type="button" class="btn ghost ico-btn" v-if="(current.artifacts || []).length"
                :aria-pressed="String(artifactsOpen)" @click="toggleArtifacts()"
                title="Les artifacts de la conversation (pages, documents, schémas)">
          <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 4h16v16H4zM4 9h16M9 9v11"/></svg>
          <span class="lbl">Artifacts ({{ current.artifacts.length }})</span></button>
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
        <div class="field" v-if="voiceSupported">
          <span>Voix</span>
          <div class="voice-pick">
            <label>lecture
              <select v-model="voicePrefs.voice"><option value="">voix du navigateur (défaut)</option>
                <option v-for="v in voiceList" :key="v.uri" :value="v.uri">{{ v.name }} — {{ v.lang }}{{ v.local ? "" : " (en ligne)" }}</option></select></label>
            <label>débit
              <select v-model.number="voicePrefs.rate">
                <option v-for="r in [0.8, 1, 1.2, 1.5, 2]" :key="r" :value="r">× {{ r }}</option></select></label>
          </div>
        </div>
        <div class="field" v-if="notifySupported">
          <span>Notifications</span>
          <label class="check"><input type="checkbox" :checked="notifyOn" @change="toggleNotify">
            prévenir quand une réponse se termine (onglet en arrière-plan)</label>
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
        <article v-for="(m, i) in current.messages" :key="m.id" class="msg"
                 :class="[m.role, { compacted: i < sentFrom && m.role !== 'summary' }]">
          <template v-if="m.role === 'summary'">
            <div class="compact-mark" role="separator">
              <span class="compact-label">
                <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M7 12h10M10 17h4"/></svg>
                {{ m.running ? "Résumé de la conversation…" : m.auto ? "Résumé automatique" : "Conversation résumée" }}
                <span class="dim" v-if="!m.running">· {{ m.covered }} message{{ m.covered > 1 ? "s" : "" }}
                  · ≈ {{ kTokens(m.before) }} → {{ kTokens(estimate(m.content)) }} tokens<template v-if="m.light">
                  · d'un historique allégé</template></span>
              </span>
            </div>
            <div class="typing" v-if="m.running"><i></i><i></i><i></i></div>
            <details class="compact-body" v-else>
              <summary>Voir le résumé<span class="dim" v-if="m.focus"> · consigne : {{ m.focus }}</span></summary>
              <div class="md" v-html="html(m)"></div>
            </details>
            <div class="msg-meta" v-if="!m.running">
              <span>{{ m.model }}</span>
              <span v-if="m.duration">{{ m.duration.toFixed(1) }} s</span>
              <span>les messages au-dessus ne sont plus envoyés au modèle</span>
              <button type="button" class="link" :disabled="streaming" @click="uncompact(i)"
                      title="Retirer ce résumé : tout l'historique repart au modèle">Annuler le résumé</button>
            </div>
          </template>
          <template v-else-if="m.role === 'user'">
            <div class="bubble">
              <div class="attachments" v-if="m.attachments && m.attachments.length">
                <template v-for="a in m.attachments" :key="a.name">
                  <img v-if="a.kind === 'image' && a.data" :src="a.data" :alt="a.name">
                  <span v-else class="chip file" :data-ext="extOf(a.name)">{{ a.name }}<span class="dim" v-if="a.kind === 'image'">
                    · image absente</span></span>
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
                  <span class="dim" v-if="t.status === 'writing'">écriture… {{ num(t.chars) }} car.</span>
                  <span class="dim" v-else-if="t.status === 'running'">en cours…</span>
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
                <span v-else class="chip file" :data-ext="extOf(a.name)">{{ a.name }}<span class="dim">
                  · image absente</span></span>
              </template>
            </div>
            <details class="gen-prompt" v-if="m.image && m.prompt && m.prompt.includes('\\n')">
              <summary>prompt envoyé (avec le contexte)</summary><pre>{{ m.prompt }}</pre></details>
            <template v-for="(part, n) in body(m)" :key="part.k ? part.k.id : 'md' + n">
              <component v-if="part.k" :is="keptComponent(part.k)" :k="part.k"></component>
              <div class="md" v-else v-html="part.html"></div>
            </template>
            <!-- Ce qui attend l'utilisateur : la CARTE d'un outil (tools/) —
                 son composant, ou le gabarit commun (view) ; une carte de la
                 feuille du bas n'a ici qu'une ligne qui la rouvre. -->
            <template v-if="m.waiting">
              <button type="button" class="ask-pending" v-if="cardPlacement(m.waiting) === 'sheet'"
                      @click="sheetOpen = true">{{ cardPending(m.waiting) }}</button>
              <component v-else-if="cardComponent(m.waiting)" :is="cardComponent(m.waiting)" :key="m.waiting.id"
                         :w="m.waiting" :busy="streaming || !!m.waiting.busy" :ctx="folderCtx()"
                         @act="(action, payload) => cardAct(m, action, payload)"></component>
              <div class="tool-card device-card" v-else>
                <template v-for="v in [cardView(m.waiting)]" :key="m.waiting.id">
                  <p class="write-head"><b>{{ v.title }}</b></p>
                  <p class="dim" v-if="v.detail">{{ v.detail }}</p>
                  <code class="device-url" v-if="v.code">{{ v.code }}</code>
                  <span class="device-swatch" v-if="v.swatch" :style="{ background: v.swatch }"></span>
                  <pre class="device-quote" v-if="v.quote">{{ v.quote }}</pre>
                  <video class="device-media" v-if="v.video" :srcObject.prop="v.video" autoplay muted playsinline></video>
                  <img class="device-media" v-if="v.preview" :src="v.preview" alt="Aperçu de l'image à envoyer">
                  <div class="card-actions">
                    <button v-for="a in v.actions" :key="a.id" type="button" :class="['btn', { ghost: !a.primary }]"
                            :disabled="streaming || m.waiting.busy" :title="a.title || ''"
                            @click="cardAct(m, a.id)">{{ a.label }}</button>
                    <span class="dim" v-if="v.note">{{ v.note }}</span>
                  </div>
                </template>
              </div>
            </template>
            <div class="tool-card device-card kept-card" v-for="k in keptBelow(m)" :key="k.id">
              <template v-for="v in [keptView(k)]" :key="k.id">
                <p class="write-head"><b>{{ v.title }}</b></p>
                <p class="dim" v-if="v.detail">{{ v.detail }}</p>
                <code class="device-url" v-if="v.code">{{ v.code }}</code>
                <span class="device-swatch" v-if="v.swatch" :style="{ background: v.swatch }"></span>
                <pre class="device-quote" v-if="v.quote">{{ v.quote }}</pre>
                <div class="card-actions">
                  <button v-for="a in v.actions" :key="a.id" type="button" :class="['btn', { ghost: !a.primary }]"
                          :disabled="keptNotes[k.id] === '…'" :title="a.title || ''"
                          @click="keptAct(m, k, a.id)">{{ a.label }}</button>
                  <span class="dim" v-if="keptNotes[k.id] && keptNotes[k.id] !== '…'">{{ keptNotes[k.id] }}</span>
                </div>
              </template>
            </div>
            <div class="out-files" v-if="m.files && m.files.length">
              <button type="button" class="chip file" v-for="f in m.files" :key="f.name" :data-ext="extOf(f.name)"
                      @click="downloadOut(f)" title="Télécharger">{{ f.name }}
                <span class="dim">{{ size(f.size) }}</span></button>
            </div>
            <div class="typing" v-if="streaming && i === current.messages.length - 1 && !m.error
                 && (m.image ? (m.attachments || []).length < (current.imageCount || 1) : !m.content)">
              <i></i><i></i><i></i><span class="dim" v-if="m.image">génération de l'image{{ (current.imageCount || 1) > 1
                ? " " + ((m.attachments || []).length + 1) + " / " + current.imageCount : "" }}…</span></div>
            <p class="msg-error" v-if="m.error">{{ m.error }}
              <button type="button" class="link" v-if="tooLong(m) && i === current.messages.length - 1 && !streaming"
                      @click="retryCompacted">Résumer la conversation et réessayer</button></p>
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
              <button type="button" class="link" v-if="m.content && voiceSupported" @click="listen(m)"
                      :title="'Lire à voix haute (' + voiceName + ')'">{{ playing.id === m.id
                        ? "Arrêter la lecture" : "Écouter" }}</button>
              <!-- Une image : ses actions ici ; plusieurs : dans l'image agrandie. -->
              <template v-if="m.image && m.attachments.length === 1 && m.attachments[0].data">
                <a class="link" :href="m.attachments[0].data" :download="m.attachments[0].name">Télécharger</a>
                <button type="button" class="link" v-if="ownImage(m.attachments[0].data)"
                        :disabled="streaming" @click="retouch(m.attachments[0])">Retoucher</button>
              </template>
              <span v-else-if="m.image && m.attachments.length > 1">cliquer une image pour la télécharger ou la retoucher</span>
              <button type="button" class="link" v-if="i === current.messages.length - 1"
                      @click="regenerate">Régénérer</button>
            </div>
          </template>
        </article>
      </div>

      <!-- La feuille du bas : la carte d'un outil qui s'y affiche (ask_user),
           par-dessus la zone de saisie ; réduite, une barre la rouvre. -->
      <transition name="sheet">
      <section class="ask-sheet" v-if="sheet" :class="{ min: !sheetOpen }" role="dialog"
               aria-label="Le modèle attend votre réponse">
        <component v-if="sheetOpen" :is="cardComponent(sheet.waiting)" :key="sheet.waiting.id"
                   :w="sheet.waiting" :busy="streaming || !!sheet.waiting.busy" :ctx="folderCtx()"
                   @act="(action, payload) => cardAct(sheet, action, payload)"
                   @minimize="sheetOpen = false"></component>
        <button type="button" class="sheet-grip" v-else @click="sheetOpen = true" aria-expanded="false">
          <span class="sheet-title">{{ cardPending(sheet.waiting) }}</span>
          <span class="dim">Ouvrir</span></button>
      </section>
      </transition>

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
            <button type="button" class="link" v-else-if="canPick" @click="pickFolder">à rouvrir</button>
            <label class="link" v-else>à rouvrir<input type="file" webkitdirectory multiple hidden @change="onFolder"></label>
            <button type="button" class="icon" title="Fermer le dossier" @click="closeFolder">×</button>
          </span>
          <span v-for="(a, k) in pending" :key="k" class="chip" :class="{ file: a.kind !== 'image' }">
            <img v-if="a.kind === 'image'" :src="a.data" alt="">
            {{ a.name }} <span class="dim">{{ size(a.size) }}</span>
            <button type="button" class="icon" title="Retirer" @click="pending.splice(k, 1)">×</button>
          </span>
        </div>
        <div class="command-hints" v-if="commandHints.length" role="listbox">
          <button type="button" v-for="c in commandHints" :key="c.name" role="option"
                  @mousedown.prevent="useCommand(c)">
            <code>{{ c.name }}</code> <span class="dim">{{ c.args }}</span> <small>{{ c.hint }}</small>
            <kbd>Tab</kbd></button>
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
              <button type="button" role="menuitem" v-if="caps.folder && canPick" @click.stop="pickFolder"><svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h6l2 2h10v11H3z"/></svg>
                <span>Dossier…<small>le modèle le parcourt, lit ce qu'il lui faut, propose des écritures</small></span></button>
              <label role="menuitem" v-else-if="caps.folder"><svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h6l2 2h10v11H3z"/></svg>
                <span>Dossier…<small>le modèle le parcourt, lit ce qu'il lui faut ; écritures téléchargées</small></span>
                <input type="file" webkitdirectory multiple hidden @change="onFolder"></label>
            </div>
          </div>
          <div class="attach-menu tools-menu" v-if="!imageMode && (hostedShown.length || devicesShown.length)">
            <button type="button" class="btn ghost attach ico-btn" aria-haspopup="menu"
                    :aria-expanded="String(toolsOpen)" @click="toolsOpen = !toolsOpen"
                    :title="activeTools.length ? 'Outils actifs : ' + activeTools.join(', ') : 'Aucun outil actif'">
              <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M14.7 6.3a4 4 0 0 0-5.4 5.2L3 17.8V21h3.2l6.3-6.3a4 4 0 0 0 5.2-5.4l-2.6 2.6-2.4-.6-.6-2.4z"/></svg>
              <span class="lbl">Outils</span><span class="count">{{ activeTools.length }}</span></button>
            <div class="menu" role="menu" v-show="toolsOpen">
              <template v-if="hostedShown.length">
                <p class="menu-head">Proxy</p>
                <label role="menuitemcheckbox" v-for="g in hostedShown" :key="g.type"
                       :aria-checked="String(hostedOn(g))"
                       :title="current.incognito && g.type === 'memory' ? 'Pas de mémoire en incognito' : ''">
                  <input type="checkbox" :checked="hostedOn(g)" @change="toggleTool(g.type)"
                         :disabled="current.incognito && g.type === 'memory'">
                  <span>{{ g.label }}<small v-if="g.type === 'browser'">{{ browserLink && browserLink.connected
                    ? "extension connectée" : "extension non connectée" }} ·
                    <a href="/ui/browser/extension.zip" download @click.stop>télécharger l'extension</a></small>
                    <small v-else>{{ current.incognito && g.type === 'memory'
                    ? "pas de mémoire en incognito" : g.tools.join(", ") }}</small></span></label>
              </template>
              <p class="menu-note" v-else-if="caps.hostedTools && !chat.hosted_tools"
                 title="[chat] hosted_tools = true dans config.toml">Outils du proxy inactifs</p>
              <template v-if="devicesShown.length">
                <p class="menu-head">Navigateur</p>
                <label role="menuitemcheckbox" v-for="f in devicesShown" :key="f.key"
                       :aria-checked="String(deviceOn.includes(f.key))">
                  <input type="checkbox" :checked="deviceOn.includes(f.key)" @change="toggleDevice(f.key)">
                  <span>{{ f.label }}<small>{{ f.hint }}</small></span></label>
              </template>
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
          <label class="effort" v-if="!imageMode && caps.reasoning" title="Niveau de réflexion du modèle : auto (celui du modèle), non, faible, moyenne, élevée">
            <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-3.5 10.9c.6.5 1 1.2 1 2.1h5c0-.9.4-1.6 1-2.1A6 6 0 0 0 12 3z"/></svg>
            <select v-model="current.reasoning" :disabled="streaming" aria-label="Réflexion">
              <option value="auto">auto</option>
              <option value="none">non</option>
              <option value="low">faible</option>
              <option value="medium">moyenne</option>
              <option value="high">élevée</option>
            </select>
          </label>
          <div class="attach-menu ctx-menu" v-if="context && context.tokens">
            <button type="button" class="btn ghost ctx-gauge" aria-haspopup="menu" :aria-expanded="String(ctxOpen)"
                    :class="{ warn: context.max && context.ratio >= 0.6, full: context.max && context.ratio >= AUTO_AT }"
                    @click="ctxOpen = !ctxOpen" :title="contextTitle">
              <svg class="ring" viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="8" class="track"/>
                <circle cx="10" cy="10" r="8" class="fill" v-if="context.max"
                        :stroke-dasharray="(Math.min(1, context.ratio) * 50.27).toFixed(2) + ' 50.27'"/></svg>
              <span class="lbl">{{ context.max ? Math.round(context.ratio * 100) + " %" : kTokens(context.tokens) }}</span></button>
            <div class="menu" role="menu" v-show="ctxOpen">
              <p class="menu-head">Contexte</p>
              <p class="menu-note">{{ context.measured ? "" : "≈ " }}{{ num(context.tokens) }} tokens{{ context.max
                ? " sur " + num(context.max) + " (" + Math.round(context.ratio * 100) + " %)" : "" }}<br>
                <span class="dim">{{ context.measured ? "mesuré par le modèle à sa dernière réponse"
                  : "estimé : la dernière mesure, plus ce qui a suivi" }}</span></p>
              <button type="button" role="menuitem" :disabled="streaming" @click="compactNow">
                <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M7 12h10M10 17h4"/></svg>
                <span>Résumer maintenant<small>/compact [consigne] — les messages restent affichés, le résumé part à leur place</small></span></button>
              <label role="menuitemcheckbox" :aria-checked="String(autoCompact)">
                <input type="checkbox" v-model="autoCompact">
                <span>Résumé automatique<small>{{ context.max ? "avant le message suivant, au-delà de " + Math.round(AUTO_AT * 100) + " % du contexte"
                  : "fenêtre de ce modèle inconnue : jamais automatique" }}</small></span></label>
            </div>
          </div>
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
    <artifact-panel v-else-if="artifactsOpen && current && (current.artifacts || []).length"
                    :artifacts="current.artifacts" :selected="artifactId"
                    @select="artifactId = $event" @close="toggleArtifacts(false)"></artifact-panel>
    <teleport to="body">
    <div class="lightbox" v-if="zoom" @click.self="zoom = null" role="dialog" aria-label="Image agrandie">
      <img :src="zoom.data" :alt="zoom.name" @click="zoom = null">
      <div class="lightbox-bar">
        <span class="dim">{{ zoom.name }}</span>
        <a class="btn ghost" :href="zoom.data" :download="zoom.name">Télécharger</a>
        <button type="button" class="btn ghost" v-if="ownImage(zoom.data)"
                :disabled="streaming" @click="retouch(zoom)">Retoucher</button>
        <button type="button" class="btn" @click="zoom = null">Fermer</button>
      </div>
    </div>
    </teleport>
  </div>`,
};
