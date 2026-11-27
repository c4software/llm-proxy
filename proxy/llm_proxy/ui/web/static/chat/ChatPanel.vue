<script>
// @ts-check
/*
  L'onglet « Chat » : un composant enregistré par main.js sous
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

  MODÈLES DE VOIX (`text-to-speech` au catalogue, voice/speech.js) :
  comme ceux d'images, le dernier message part à /ui/audio/speech ; la
  réponse est son audio, joint. La voix se choisit dans la barre (toute
  faite, ou clonée, gardée avec le compte) ; le prompt système donne le ton,
  ou décrit la voix (modèle de conception).

  MODÈLES DE TRANSCRIPTION (`automatic-speech-recognition` au catalogue,
  voice/transcribe.js) : l'audio du dernier message (joint, ou enregistré
  au micro de la barre) part à /ui/audio/transcriptions ; la réponse est
  son texte. Le texte du message, s'il y en a, guide le modèle.
*/
import * as Vue from "vue";
import * as VueUse from "@vueuse/core";
import { ChatStore } from "./store.js";
import { inline, dataUrl, ownImage } from "./history.js";
import { readAttachment } from "./attachments.js";
import { imagePrompt, oneImage } from "./images.js";
import { readStream } from "./stream.js";
import { partialString } from "../lib/partialjson.js";
import { modelCaps } from "./browser.js";
import * as workspace from "./workspace.js";
import * as notify from "./notify.js";
import * as tools from "./tools/index.js";
import * as artifactBridge from "./artifact-bridge.js";
import ArtifactPanel from "./ArtifactPanel.vue";
import AssetPanel, { conversationImages } from "./AssetPanel.vue";
import LinuxPanel from "./LinuxPanel.vue";
import ChatHeader from "./ChatHeader.vue";
import ChatSettings from "./ChatSettings.vue";
import ChatModals from "./ChatModals.vue";
import ChatComposer from "./ChatComposer.vue";
import ChatThread from "./ChatThread.vue";
import { useChatModels } from "./useChatModels.js";
import { useChatVoice } from "./useChatVoice.js";
import { useChatImages } from "./useChatImages.js";
import { useChatCards } from "./useChatCards.js";
import { useChatCompact } from "./useChatCompact.js";
import { setOpener as setLinuxOpener, vm as linuxVm } from "./tools/sandbox/linuxvm.js";
import { download } from "../lib/files.js";
import { Markdown } from "../lib/markdown.js";
import { num, size } from "../lib/format.js";
import * as Compact from "./compact.js";
import * as prompts from "../lib/prompts.js";
import * as api from "./api.js";
import { apiMessages } from "./api.js";
import { chipFor, recordCall, endStep, closeStep, pushStep, timeline } from "./turn.js";

const ChatPanel = {
  components: { ArtifactPanel, AssetPanel, LinuxPanel, ChatHeader, ChatSettings, ChatModals, ChatComposer, ChatThread },
  props: { session: { type: Object, required: true } },
  emits: ["unauthorized"],
  setup(props, { emit }) {
    const { ref, reactive, computed, watch, nextTick, onMounted, onUpdated, onUnmounted, toRaw } = Vue;
    const API = "/ui/chat/completions";
    // Une réponse du proxy (webhook) encore « en cours » passé ce délai :
    // le proxy a redémarré pendant le tour, elle ne viendra plus.
    const SERVER_STALE = 30 * 60 * 1000;
    const MEMORY_OPEN = "llm-proxy-chat-memory";
    // Noms lisibles des types d'outils ; un type inconnu garde le sien.
    const TOOL_LABELS = {
      web_search: "Web", code_execution: "Code", code_interpreter: "Code",
      image_generation: "Images", memory: "Mémoire", ocr: "OCR",
      transcribe: "Transcription", webhook: "Webhooks", repository: "Dépôts",
    };

    const chat = computed(() => props.session.chat || {});
    // Tours d'outils de la page au plus, par réponse ([ui].chat_max_rounds) ;
    // sans elle (session absente), aucun tour plutôt qu'une boucle sans fin.
    const maxRounds = () => chat.value.max_rounds ?? 0;
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
    // neuve reprend le dernier choix fait dans ce navigateur. Leurs
    // déclarations viennent du proxy (`chat.page` : sans celles qu'il
    // refuse, [ui].page_tools_disabled, ni celles d'une extension désactivée) ;
    // les natives sont installées tout de suite, celles des extensions
    // une fois leurs modules importés.
    const installTick = ref(0);
    // Les textes que la page ajoute aux messages (lib/prompts.js), du
    // catalogue du proxy.
    watch(() => props.session.chat && props.session.chat.prompts, (given) => { prompts.install(given); },
          { immediate: true });
    watch(() => props.session.chat && props.session.chat.page, (page) => {
      tools.install(page);
      installTick.value++;
      tools.installAll(page).then(() => { installTick.value++; });
    }, { immediate: true });
    const deviceFamilies = computed(() => (installTick.value, tools.menuFamilies()));
    // Le relais réseau de la VM Linux, ouvert à ce compte ([ui].linux_relay).
    watch(() => !!(props.session.chat && props.session.chat.linux_relay), (on) => { linuxVm.relay = on; },
          { immediate: true });
    const devicePrefs = VueUse.useLocalStorage("llm-proxy-chat-device", null,
                                               { serializer: VueUse.StorageSerializers.object });
    const defaultDevice = () => (Array.isArray(devicePrefs.value) ? devicePrefs.value : tools.defaultFamilies());

    // ── état ────────────────────────────────────────────────────────────
    // Les conversations : le magasin partagé avec le menu latéral.
    const S = ChatStore;
    S.open(props.session.client);
    const current = S.current;
    const currentId = computed(() => S.state.currentId);
    const pendingConv = S.pending;
    const loadError = computed(() => S.state.loadError);
    const retryLoad = () => S.fetchCurrent();
    const draft = ref("");
    const pending = ref([]);          // pièces jointes du message en cours
    const streaming = ref(false);
    const status = ref("");
    const settingsOpen = ref(false);
    const storageWarning = computed(() => S.state.storageWarning);
    // Une conversation enregistrée ailleurs (autre onglet, appareil) depuis
    // sa lecture : son enregistrement attend le choix de l'utilisateur.
    const conflict = computed(() => S.state.conflict);
    const resolveConflict = (choice) => S.resolveConflict(choice);
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
      linuxOpen.value = false;
      assetsOpen.value = false;
    }
    // Le modèle teste un artifact (inspect_artifact, run_in_artifact) : le
    // panneau s'ouvre dessus s'il ne l'est pas.
    artifactBridge.setOpener(openArtifact);
    function toggleArtifacts(open = !artifactsOpen.value) {
      artifactsOpen.value = open;
      if (open) { memoryOpen.value = false; linuxOpen.value = false; assetsOpen.value = false; }
    }
    // Les images générées de la conversation (AssetPanel) : un panneau de
    // plus, un seul à la fois. `assetKey` vide : la dernière.
    const assetsOpen = ref(false);
    const assetKey = ref("");
    const assets = computed(() => conversationImages(current.value));
    function toggleAssets(open = !assetsOpen.value) {
      assetsOpen.value = open;
      if (open) { memoryOpen.value = false; artifactsOpen.value = false; linuxOpen.value = false; }
    }
    // Retrouver une image dans le fil : son message, à l'écran.
    function locateAsset(a) {
      const el = thread.value && thread.value.querySelector(`[data-msg="${CSS.escape(a.msgId)}"]`);
      if (!el) return;
      pinned = false;
      el.scrollIntoView({ block: "center", behavior: "smooth" });
    }
    // Le terminal de la VM Linux (LinuxPanel) : un panneau de plus, un
    // seul à la fois. Toujours proposé (la famille Linux ne fait que donner
    // run_shell au modèle), sauf dans un navigateur qui ne sait pas, ou si
    // le proxy refuse la famille ([ui].page_tools_disabled).
    const linuxOpen = ref(false);
    const linuxOffered = computed(() => !!current.value
      && deviceFamilies.value.some((f) => f.key === tools.FAMILY.LINUX));
    function toggleLinux(open = !linuxOpen.value) {
      linuxOpen.value = open;
      if (open) { memoryOpen.value = false; artifactsOpen.value = false; assetsOpen.value = false; }
    }
    // Le premier run_shell d'une conversation ouvre le terminal, si c'est
    // celle qui est affichée.
    setLinuxOpener((conv) => {
      if (!current.value || current.value.id !== conv.id) return false;
      toggleLinux(true);
      return true;
    });
    // Prévenir de la fin d'une réponse (notify.js), réglage du navigateur.
    const notifyOn = ref(notify.enabled());
    async function toggleNotify(e) { notifyOn.value = await notify.setEnabled(e.target.checked); }
    function toggleMemory(open = !memoryOpen.value) {
      memoryOpen.value = open;
      if (open) { artifactsOpen.value = false; linuxOpen.value = false; assetsOpen.value = false; }
    }
    const threadComp = ref(null);
    const thread = computed(() => threadComp.value && threadComp.value.el);
    // Écran étroit : textes courts (la zone de saisie, sur mobile).
    const narrow = VueUse.useMediaQuery("(max-width: 760px)");
    const composer = ref(null);
    const input = computed(() => composer.value && composer.value.textarea);
    let controller = null;
    // Une réponse (ou un résumé, une image, une voix…) commence : le chat
    // passe en cours, et `stop()` l'arrête par ce contrôleur.
    function startRun(text = "") {
      streaming.value = true;
      status.value = text;
      return (controller = new AbortController());
    }
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

    // ── modèles (useChatModels.js) ──────────────────────────────────────
    const {
      models, modelsError, modelsLoading, modelsTitle, modelEntry,
      caps, modelGroups, isImage, imageMode, voiceOf, voiceMode, isAsr, asrMode,
      genMode, imageSizes, stepsOf, imageSteps, lorasOf, imageLoras, defaultModel,
      blind, unsent, loadModels, reloadModels,
    } = useChatModels({ chat, current, pending, onUnauthorized: () => emit("unauthorized") });

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
    const devicesShown = computed(() => (caps.value.pageTools ? [] : deviceFamilies.value));
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
    // Le sélecteur du système (useFileDialog) : des fichiers à joindre.
    const filesDialog = VueUse.useFileDialog({ multiple: true });
    filesDialog.onChange((files) => { if (files && files.length) addFiles([...files]); });
    const pickFiles = () => filesDialog.open({ reset: true });

    // ── dossier ouvert (chat/workspace.js) ──────────────────────────────
    // En mémoire seulement, par conversation : `conv.folder` n'en garde
    // que le nom (après un rechargement : « à rouvrir »).
    const folders = new Map();
    const folderTick = ref(0);            // Map non réactive : un compteur
    const folderReady = computed(() => (folderTick.value, !!current.value
      && folders.has(current.value.id)));
    const folderCount = computed(() => (folderTick.value, current.value
      && folders.has(current.value.id) ? folders.get(current.value.id).count : 0));
    // Sans le sélecteur de dossier qui permet d'écrire (canPick) : un
    // dossier lu par useFileDialog, ses écritures téléchargées.
    // Une conversation supprimée (ou partie avec un autre compte) : son
    // dossier ouvert est lâché.
    watch(() => S.state.convs.map((c) => c.id), (ids) => {
      const alive = new Set(ids);
      for (const id of [...folders.keys()]) if (!alive.has(id)) folders.delete(id);
    });
    const folderDialog = VueUse.useFileDialog({ directory: true, multiple: true });
    folderDialog.onChange((files) => onFolder(files ? [...files] : []));
    const pickFolderFiles = () => folderDialog.open({ reset: true });
    function onFolder(list) {
      const c = current.value;
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
    // Une réponse que le PROXY écrit dans la conversation affichée (un
    // webhook, webhook_turn.py : `serverTurn`). Pendant elle, la page
    // n'écrit pas dans la conversation (envoyer, modifier, régénérer) : elle
    // écrirait sur une révision dépassée (409).
    const serverBusy = computed(() => !!(current.value && current.value.messages.some((m) => m.serverTurn)));
    const canSend = computed(() => Boolean(current.value && !serverBusy.value
      && current.value.model && (draft.value.trim() || pending.value.length)));
    const queue = computed(() => (current.value && current.value.queue) || []);

    // Les messages et les outils d'une requête : chat/api.js (sans Vue,
    // testé). Ici, ce que le composant sait du modèle et du catalogue.
    const requestTools = (conv, ctx) => api.requestTools(conv, ctx, {
      caps: modelCaps(modelEntry(conv.model)), available: available.value,
      families: conv.device || defaultDevice(), groups: toolGroups.value });

    // Ce qui part au modèle sans être dans le fil, pour le PROCHAIN
    // message : le prompt système composé (le sien, un résumé, les
    // consignes des outils) et les fonctions déclarées — celles de la page,
    // et celles que le proxy ajoute pour les outils hébergés cochés.
    // Les tokens sont estimés (Compact.estimate), pas mesurés.
    // Les outils : relus quand le choix change ; le prompt système, qui
    // relit le fil, seulement s'il est affiché.
    const promptOpen = ref(false);
    const requested = computed(() => {
      const c = current.value;
      if (!c) return null;
      const { hosted, page, guidance } = requestTools(c, toolCtx(c, null));
      const specs = [
        ...toolGroups.value.filter((g) => hosted.includes(g.type))
          .flatMap((g) => (g.specs || []).map((spec) => ({ spec, from: "proxy" }))),
        ...page.map((spec) => ({ spec, from: "navigateur" }))]
        .map((d) => ({ ...d, name: d.spec.function.name, json: JSON.stringify(d.spec, null, 2),
                       tokens: Compact.estimate(JSON.stringify(d.spec)) }));
      return { guidance, specs, guidanceTokens: Compact.estimate(guidance),
               toolTokens: specs.reduce((n, d) => n + d.tokens, 0) };
    });
    const hiddenPrompt = computed(() => {
      const c = current.value, r = requested.value;
      if (!c || !r || !promptOpen.value) return null;
      const head = apiMessages(c, c.messages.length, r.guidance)[0];
      const system = head && head.role === "system" ? head.content : "";
      const systemTokens = Compact.estimate(system);
      return { ...r, system, systemTokens, total: systemTokens + r.toolTokens };
    });
    // Ce que coûte un choix du menu Outils, coché ou non : ses fonctions,
    // et la consigne de la famille.
    const hostedCost = (g) => Compact.estimate(JSON.stringify(g.specs || []));
    function familyCost(f) {
      const c = current.value;
      const ctx = c ? toolCtx(c, null) : null;
      return Compact.estimate(JSON.stringify(f.tools.map((t) => t.spec)) + (ctx ? f.guidance(ctx) : ""));
    }
    // Les outils cochés (fonctions et consignes) : ce qu'ils ajoutent à
    // chaque requête.
    const toolsCost = computed(() => (requested.value
      ? requested.value.toolTokens + requested.value.guidanceTokens : 0));
    function showPrompt() {
      toolsOpen.value = false;
      settingsOpen.value = true;
      promptOpen.value = true;
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
      // Un message modifié (flèche du haut, « Modifier ») : la conversation
      // reprend de lui — ce qui le suivait disparaît, maintenant seulement.
      const k = editing.value;
      editing.value = null;
      if (k !== null && !streaming.value && c.messages[k] && c.messages[k].role === "user") {
        abandonWaiting(c);
        c.messages.splice(k);
        ask(c, item);
        return;
      }
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
      if (!resumeMsg && voiceOf(conv.model)) return speakReply(conv);
      if (!resumeMsg && isAsr(conv.model)) return transcribeReply(conv);
      if (!resumeMsg) {
        conv.messages.push({ id: uid(), role: "assistant", content: "", reasoning: "",
                             tools: [], sources: [], model: conv.model, usage: null,
                             error: "", started: Date.now(), duration: 0 });
      }
      // Le message RÉACTIF : c'est lui que le flux modifie.
      const live = resumeMsg || conv.messages[conv.messages.length - 1];
      const upto = conv.messages.indexOf(live);
      conv.updated = Date.now();
      const ctrl = startRun();
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
      const ctx = toolCtx(conv, live, ctrl.signal);
      const { hosted, page, guidance, state } = requestTools(conv, ctx);
      body.tools = [...hosted.map((type) => ({ type })), ...page];
      // L'état des outils au moment où l'utilisateur envoie son message :
      // fixé une fois (une reprise, une régénération le gardent), pour que
      // les tours suivants rejouent ce message à l'identique.
      const question = conv.messages[upto - 1];
      if (question && question.role === "user" && typeof question.state !== "string") question.state = state;
      let paused = false;
      try {
        for (;;) {
          // Un tour à demander au modèle — sauf à la reprise d'une pause.
          if (!(live.step && live.step.queue.length)) {
            if ((live.steps || []).length >= maxRounds()) {
              live.error = `Arrêté après ${maxRounds()} tours d'outils.`;
              break;
            }
            const start = live.content.length;
            live.hidden = 0;      // chaque requête redit le sien (x_hidden)
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
            const got = await readStream(r.body, live, { calls, onCall: (slot) => writing(live, slot),
                                                         onMemory: () => { memoryStamp.value++; } });
            // Un flux fini sans [DONE] : coupé en route (proxy, réseau) —
            // ses appels peuvent être incomplets, rien n'est exécuté. Une
            // réponse arrêtée par sa limite de tokens : à continuer.
            live.cut = live.error ? "" : !got.done ? "stream" : got.finish === "length" ? "length" : "";
            const asked = calls.filter((c) => c && tools.isTool(c.name))
              .map((c) => ({ id: c.id || "call_" + uid(), name: c.name, arguments: c.arguments }));
            if (!asked.length || live.error || live.cut === "stream") {
              if (live.steps) live.final = live.content.slice(start);
              break;
            }
            live.step = { content: live.content.slice(start), calls: [], queue: asked };
          }
          while (live.step.queue.length) {
            const c = live.step.queue[0];
            const chip = chipFor(live, c);
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
            recordCall(live, chip, c, result, image);
          }
          endStep(live);
        }
      } catch (e) {
        if (e.name !== "AbortError") live.error = "Connexion interrompue : " + e.message;
        else if (!live.content) live.error = "Arrêté.";
        closeStep(live, e.name === "AbortError" ? "Arrêté par l'utilisateur." : "Non exécuté : connexion interrompue.");
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
          if ((live.steps || []).length >= maxRounds()) {
            live.error = `Arrêté après ${maxRounds()} questions.`;
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
        const chip = chipFor(live, c);
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
        recordCall(live, chip, c, result);
        endStep(live);
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

    // ── les cartes et fichiers produits (useChatCards.js) ──────────────
    const {
      toolCtx, cardAct, cardView, keptView, keptNotes, keptAct, cardComponent,
      cardPlacement, cardPending, folderCtx, sheet, sheetOpen, extOf, filePreview,
      previewCopied, copyPreview, downloadOut, openOut, abandonWaiting,
    } = useChatCards({
      current, folders, folderTick, modelEntry, streaming, status,
      onPin: () => { pinned = true; }, respond,
    });

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

    // ── contexte : la jauge, le résumé (useChatCompact.js) ──────────────
    const {
      autoCompact, ctxOpen, context, ctxParts, sentFrom, contextNear,
      contextTitle, tooLong, compact, compactNow, uncompact, retryCompacted,
      commandHints, useCommand,
    } = useChatCompact({
      API, current, genMode, modelEntry, requested, streaming, status, draft,
      input, uid, startRun, clearController: () => { controller = null; },
      respond, onUnauthorized: () => emit("unauthorized"),
    });

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
      const prompt = imagePrompt(conv, !!source || !!conv.imageAlone);
      conv.messages.push(msg);
      conv.updated = Date.now();
      const live = conv.messages[conv.messages.length - 1];
      live.prompt = prompt;
      if (!ask || !ask.content.trim()) {
        live.error = "Décrire l'image à générer.";
        done(conv, live, controller = new AbortController());
        return;
      }
      await drawImages(conv, live, source, Number(conv.imageCount) || 1);
    }
    // GÉNÉRER ENCORE : `count` images de plus, jointes à la même réponse,
    // avec le prompt qu'elle a envoyé (m.prompt, tel quel : rien du fil
    // depuis) — et l'image retouchée par sa question, si c'en était une.
    const moreCount = ref(1);
    async function generateMore(m, count = moreCount.value) {
      const c = current.value;
      if (streaming.value || !c || !m.image || !m.prompt) return;
      const i = c.messages.indexOf(m);
      const ask = c.messages[i - 1];
      const source = ask && ask.role === "user" && (ask.attachments || []).find((a) => a.kind === "image" && a.data);
      m.error = "";
      // La durée s'ajoute à celle de la réponse (done la recalcule).
      m.started = Date.now() - (m.duration || 0) * 1000;
      await drawImages(c, m, source, count);
    }
    // Les images d'une réponse, une à une (n = 1 chacune : plus d'un
    // serveur ignore `n`), affichées à mesure ; `live.want` : le compte
    // attendu, pour l'indicateur du fil.
    async function drawImages(conv, live, source, count) {
      const ctrl = startRun();
      const size = imageSizes.value.includes(conv.imageSize) ? conv.imageSize
        : (chat.value.image_size || imageSizes.value[0]);
      // Les passes, seulement si le modèle en propose et qu'un choix est fait.
      const steps = stepsOf(live.model || conv.model).includes(conv.imageSteps) ? conv.imageSteps : 0;
      // Le LoRA, de même (force 1, celle des fiches) ; génération et retouche.
      const lora = lorasOf(live.model || conv.model).includes(conv.imageLora) ? conv.imageLora : "";
      const before = live.attachments.length;
      live.want = before + Math.min(Math.max(count, 1), 4);
      try {
        while (live.attachments.length < live.want && !ctrl.signal.aborted) {
          const got = await oneImage(conv, { model: live.model || conv.model, prompt: live.prompt, size, n: 1,
            ...(steps ? { steps } : {}),
            ...(lora ? { loras: [{ name: lora, scale: 1 }] } : {}),
            ...(source ? { image: await dataUrl(source.data) } : {}) }, ctrl.signal, () => emit("unauthorized"));
          const { revised, ...image } = got;
          live.attachments.push(image);
          if (revised && !live.content) live.content = "*" + revised + "*";
        }
      } catch (e) {
        live.error = e.name === "AbortError" ? (live.attachments.length > before ? "" : "Arrêté.") : e.message;
      } finally {
        delete live.want;
        done(conv, live, ctrl);
      }
    }

    // ── voix, transcription et images (useChatVoice.js, useChatImages.js) ─
    const {
      voiceName, voiceSupported, playing, listen, voiceChoices, studioOpen,
      newVoice, voiceSaved, voiceLang, justSpoken, onlyOne, speakReply,
      transcribeReply, rec, toggleRecord,
    } = useChatVoice({ current, pending, status, voiceMode, voiceOf, asrMode, uid, startRun, done });
    const {
      zoom, zoomIndex, zoomGo, upscaleOpt, upscaleFactor, upscaleStrength,
      strengthText, upscale, retouch,
    } = useChatImages({
      chat, current, assets, pending, streaming, input, uid, startRun, done,
      onUnauthorized: () => emit("unauthorized"),
    });
    // Échap : ferme l'image agrandie ; sinon, une génération en cours
    // s'arrête — sauf si la touche a déjà servi (modification annulée,
    // feuille de questions, champ d'un menu).
    VueUse.onKeyStroke("Escape", (e) => {
      if (zoom.value) { zoom.value = null; return; }
      if (studioOpen.value) { studioOpen.value = false; return; }
      if (filePreview.value) { filePreview.value = null; return; }
      if (e.defaultPrevented || !inputShown()) return;
      if (streaming.value) { e.preventDefault(); stop(); }
    });

    // Le champ de saisie reprend le focus quand l'onglet du navigateur le
    // reprend, ou que la vue Chat réapparaît — sans le voler à un autre
    // champ, un menu, la feuille de questions ou un artifact, ni à une
    // sélection de texte ; jamais sur un écran étroit (le clavier).
    const inputShown = () => !!(input.value && input.value.offsetParent);
    function refocus() {
      const el = input.value;
      if (!el || narrow.value || !inputShown() || document.visibilityState !== "visible") return;
      const a = document.activeElement;
      if (a && a !== document.body && a !== el && (a.matches("input, textarea, select, iframe, [contenteditable]")
          || a.closest(".menu, .ask-sheet, .sheet-inner, .artifact-side, .chat-settings, [role=dialog]"))) return;
      if (String(window.getSelection ? window.getSelection() : "").trim()) return;
      el.focus({ preventScroll: true });
    }
    VueUse.useEventListener(window, "focus", refocus);
    VueUse.useEventListener(document, "visibilitychange", () => { if (document.visibilityState === "visible") refocus(); });
    const inputVisible = VueUse.useElementVisibility(input);
    watch(inputVisible, (v) => { if (v) nextTick(refocus); });

    function stop() { if (controller) controller.abort(); }

    // Une réponse coupée (limite de tokens, flux interrompu) : le modèle
    // est prié de reprendre où il s'est arrêté, par un message à la suite.
    function continueReply() {
      const c = current.value;
      if (streaming.value || serverBusy.value || !c) return;
      const last = c.messages[c.messages.length - 1];
      if (last) last.cut = "";
      abandonWaiting(c);
      ask(c, { id: uid(), content: "Continue exactement là où ta réponse s'est arrêtée, sans répéter ce qui précède.",
               attachments: [] });
    }
    // Une réponse interrompue par un rechargement en plein tour : la boucle
    // reprend sur le même message (ses tours d'outils clos, prepareConv).
    function resumeInterrupted(m) {
      const c = current.value;
      if (streaming.value || !c || c.messages[c.messages.length - 1] !== m) return;
      m.interrupted = false;
      m.error = "";
      respond(c, m);
    }

    // La dernière réponse est remplacée, la question reste.
    function regenerate() {
      const c = current.value;
      if (streaming.value || serverBusy.value || !c) return;
      const last = c.messages[c.messages.length - 1];
      if (last && last.role === "assistant") c.messages.pop();
      if (c.messages.length) respond(c);
    }

    // Modifier une question : elle revient dans la zone de saisie, et la
    // conversation reprend d'elle.
    // Modifier une question (« Modifier », ou flèche du haut dans un champ
    // vide) : elle revient dans la zone de saisie ; rien n'est retiré de la
    // conversation avant l'ENVOI, qui la reprend d'elle (send). Échap, ou
    // un champ vidé, annule.
    const editing = ref(null);       // l'indice du message modifié
    function edit(i) {
      const c = current.value;
      if (streaming.value || serverBusy.value || !c || !c.messages[i] || c.messages[i].role !== "user") return;
      const m = c.messages[i];
      editing.value = i;
      draft.value = m.content;
      pending.value = [...(m.attachments || [])].filter((a) => a.data);
      nextTick(() => {
        const el = input.value;
        if (el) { el.focus(); autosize(); el.setSelectionRange(el.value.length, el.value.length); }
      });
    }
    function cancelEdit() {
      if (editing.value === null) return;
      editing.value = null;
      draft.value = "";
      pending.value = [];
    }
    const lastUser = () => {
      const msgs = (current.value && current.value.messages) || [];
      for (let i = msgs.length - 1; i >= 0; i--) if (msgs[i].role === "user") return i;
      return -1;
    };
    // Le champ vidé pendant une modification : elle est abandonnée.
    watch(draft, (v, old) => { if (editing.value !== null && !v.trim() && (old || "").trim()) cancelEdit(); });
    watch(() => current.value && current.value.id, () => { editing.value = null; });

    function onKey(e) {
      // Flèche du haut dans un champ vide : le dernier message, à modifier.
      if (e.key === "ArrowUp" && !e.shiftKey && !e.altKey && !e.ctrlKey && !e.metaKey && !e.isComposing
          && !draft.value && !pending.value.length && !streaming.value && editing.value === null) {
        const i = lastUser();
        if (i >= 0) { e.preventDefault(); edit(i); }
        return;
      }
      if (e.key === "Escape" && editing.value !== null) {
        e.preventDefault();
        cancelEdit();
        return;
      }
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
    // Le Markdown d'un texte du message `m`, gardé sous `key` (un champ, un
    // segment de la chronologie) tant que ce texte ne change pas.
    function md(m, key, src) {
      const raw = toRaw(m);
      let entry = cache.get(raw);
      if (!entry) cache.set(raw, entry = {});
      src = src || "";
      if (!entry[key] || entry[key].src !== src) entry[key] = { src, html: Markdown.render(src) };
      return entry[key].html;
    }
    const html = (m, field = "content") => md(m, field, m[field]);

    // Le CORPS d'une réponse : son texte, et les cartes durables qui s'y
    // placent (un graphique interactif : celles à `slot` et à composant).
    // Le modèle en place une en écrivant, sur une ligne seule,
    // ![titre](chart:<slot>) ; une carte qu'il ne place pas va en tête.
    const SLOT_MARK = /^[ \t]*!\[[^\]\n]*\]\((?:chart|card):([\w.-]+)\)[ \t]*$/gm;
    const slotted = (m) => (m.kept || []).filter((k) => k.slot && tools.keptComponent(k));
    // `src` : le texte d'un segment de la chronologie (le `k`-ième, qui
    // commence à `start` dans m.content) ; une carte n'est placée qu'à son
    // PREMIER marqueur du message ; celle que le modèle ne place pas va en
    // tête du dernier segment (`last`).
    function body(m, src = m.content, k = 0, last = true, start = 0) {
      const cards = slotted(m);
      if (!cards.length) return [{ html: md(m, "c" + k, src) }];
      const raw = toRaw(m);
      let entry = cache.get(raw);
      if (!entry) cache.set(raw, entry = {});
      src = src || "";
      const key = src + "\u0000" + cards.map((c) => c.id).join(",") + "\u0000" + last + "\u0000" + start
        + "\u0000" + (m.content || "").length;
      const slot = "body" + k;
      if (entry[slot] && entry[slot].key === key) return entry[slot].parts;
      const bySlot = new Map(cards.map((c) => [c.slot, c]));
      // Le premier marqueur de chaque carte, dans tout le message.
      const first = new Map();
      for (const hit of (m.content || "").matchAll(SLOT_MARK)) {
        const c = bySlot.get(hit[1]);
        if (c && !first.has(c.id)) first.set(c.id, hit.index);
      }
      const parts = [];
      let at = 0;
      for (const hit of src.matchAll(SLOT_MARK)) {
        const c = bySlot.get(hit[1]);
        if (!c || first.get(c.id) !== start + hit.index) continue;
        parts.push({ html: Markdown.render(src.slice(at, hit.index)) }, { k: c });
        at = hit.index + hit[0].length;
      }
      parts.push({ html: Markdown.render(src.slice(at)) });
      const loose = last ? cards.filter((c) => !first.has(c.id)).map((c) => ({ k: c })) : [];
      const out = [...loose, ...parts.filter((p) => p.k || p.html.trim())];
      entry[slot] = { key, parts: out };
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
      // Une image rendue dans le texte (lien d'un outil hébergé) : agrandie.
      if (e.target.tagName === "IMG" && e.target.closest(".md") && !e.target.closest("a")) {
        const src = e.target.getAttribute("src");
        const a = assets.value.find((x) => x.data === src);
        if (a) { zoom.value = a; return; }
      }
      // Une image d'un autre site (Markdown) : chargée seulement au clic,
      // jamais d'office — sa requête pourrait exfiltrer des données.
      const load = e.target.closest("button.md-img-load");
      if (load) {
        const img = document.createElement("img");
        img.src = load.dataset.src || "";
        img.alt = load.dataset.alt || "";
        img.referrerPolicy = "no-referrer";
        load.replaceWith(img);
        return;
      }
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
    // Un segment de la chronologie (turn.timeline, `t`) réfléchit encore :
    // le dernier — le tour en cours —, pendant le flux, tant qu'il n'a rien
    // écrit et qu'aucun outil de la réponse ne tourne (sa puce le dit déjà ;
    // celle d'un outil hébergé est au segment d'avant).
    const busyTool = (m) => (m.tools || []).some((c) => c.status === "running" || c.status === "writing");
    function thinking(m, i, t, k) {
      const seg = t.segments[k];
      return streaming.value && i === current.value.messages.length - 1
        && k === t.segments.length - 1 && !seg.content.trim() && !busyTool(m);
    }
    function preview(m, i, t, k) {
      if (!thinking(m, i, t, k)) return { text: candidate(t.segments[k].reasoning), n: "fin" };
      const s = shown[m.id];
      // L'aperçu d'un tour précédent ne passe pas au tour suivant.
      return s && s.seg === t.segments.length ? s : { text: "", n: 0 };
    }
    // Le pas de l'aperçu, tant qu'une réponse réfléchit.
    let pace = null;
    watch(streaming, (on) => {
      clearInterval(pace);
      if (!on) { for (const k in shown) delete shown[k]; return; }
      pace = setInterval(() => {
        const msgs = current.value ? current.value.messages : [];
        const m = msgs[msgs.length - 1];
        if (!m || m.role !== "assistant") return;
        const segs = timeline(m).segments, seg = segs.at(-1);
        if (seg.content.trim() || !seg.reasoning) return;
        const text = candidate(seg.reasoning);
        const s = shown[m.id];
        if (text && (!s || s.text !== text || s.seg !== segs.length)) {
          shown[m.id] = { text, n: (s ? s.n : 0) + 1, seg: segs.length };
        }
      }, REASON_PACE);
    });
    onUnmounted(() => clearInterval(pace));
    // Le panneau quitté (déconnexion) : la réponse en cours s'arrête, sans
    // laisser S.state.streaming levé.
    onUnmounted(() => { if (controller) controller.abort(); S.state.streaming = false; });

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

    // Une réponse du proxy restée « en cours » au-delà de SERVER_STALE : le
    // proxy a redémarré pendant le tour, elle ne viendra plus. Jugé à la
    // lecture (prepareConv) et pendant l'attente (la relecture de 3 s).
    function expireServerTurns(c) {
      for (const m of c.messages || []) {
        if (m.serverTurn && Date.now() - (m.started || 0) > SERVER_STALE) {
          delete m.serverTurn;
          m.error = "Réponse du webhook interrompue (proxy redémarré pendant le tour ?).";
          m.duration = 0.001;
        }
      }
    }
    // Une conversation lue du proxy (à l'ouverture, ou plus tard depuis le
    // menu) : ses réglages manquants.
    function prepareConv(c) {
      if (!c.model) c.model = defaultModel();
      if (!c.imageSize) c.imageSize = chat.value.image_size;
      if (!c.imageCount) c.imageCount = 1;
      if (!c.imageSteps) c.imageSteps = 0;
      if (!c.imageLora) c.imageLora = "";
      if (c.imageAlone === undefined) c.imageAlone = false;
      if (!c.reasoning) c.reasoning = "auto";
      // Une carte du navigateur en cours au rechargement : ses boutons
      // reviennent (le flux de caméra, lui, est perdu).
      for (const m of c.messages || []) if (m.waiting && m.waiting.kind === "device") m.waiting.busy = false;
      expireServerTurns(c);
      // Une réponse enregistrée EN PLEIN TOUR (page rechargée ou fermée
      // pendant le flux) : ses puces en cours passent en erreur, son tour
      // d'outils ouvert est clos, et elle se signale avec « Reprendre ».
      // Pas pendant un flux de CET onglet (l'enregistrement d'un autre),
      // ni pour une réponse que le proxy écrit (serverTurn).
      const last = (c.messages || [])[c.messages.length - 1];
      if (last && last.role === "assistant" && !last.waiting && !last.serverTurn
          && !(streaming.value && current.value === c)) {
        const midway = (last.step && !last.waiting)
          || (last.tools || []).some((t) => t.status === "running" || t.status === "writing")
          || (last.started && !last.duration && !last.error && !last.image && !last.voice && !last.asr);
        if (midway) {
          for (const t of last.tools || []) {
            if (t.status === "running" || t.status === "writing") t.status = "error";
          }
          closeStep(last, "Non exécuté : la page a été rechargée pendant la réponse.");
          last.interrupted = true;
        }
      }
    }
    S.setPrepare(prepareConv);

    // Les webhooks du compte écrivent dans l'historique sans cette page
    // (un message reçu, sa réponse) : la liste et la conversation ouverte
    // suivent, de temps en temps et au retour sur l'onglet (store.sync).
    // Une réponse du proxy en cours dans la conversation affichée : relue
    // toutes les 3 s, jusqu'à ce qu'elle arrive — ou qu'elle soit périmée.
    if (props.session.webhooks) {
      const sync = () => { if (document.visibilityState === "visible") S.sync(); };
      VueUse.useIntervalFn(sync, 20000);
      VueUse.useIntervalFn(() => {
        if (!serverBusy.value) return;
        expireServerTurns(current.value);
        sync();
      }, 3000);
      VueUse.useEventListener(document, "visibilitychange", sync);
    }

    onMounted(async () => {
      // Le catalogue et l'historique en même temps : l'un n'attend pas l'autre.
      const models = loadModels();
      await S.open(props.session.client);
      if (!S.state.convs.length) S.newConv();
      await models;
      for (const c of S.state.convs) if (!c.stub && !c.model) c.model = defaultModel();
      nextTick(() => {
        if (thread.value) thread.value.scrollTop = thread.value.scrollHeight;
        autosize();
      });
    });

    return { continueReply, resumeInterrupted, chat, toolGroups, available, current, currentId, pendingConv, loadError, retryLoad,
             ownImage, filePreview, openOut, previewCopied, copyPreview, models, modelGroups, modelsError, modelsLoading, modelsTitle, reloadModels, caps, blind, unsent, imageMode, imageSizes, imageSteps, imageLoras, upscaleOpt, upscaleFactor, upscaleStrength, strengthText, upscale,
             voiceMode, asrMode, rec, toggleRecord, genMode, voiceChoices, newVoice, studioOpen, voiceSaved, voiceLang, justSpoken, onlyOne, toggleIncognito, zoom, retouch, exportMd: S.exportMd, draft, pending, streaming, status,
             settingsOpen,
             storageWarning, conflict, resolveConflict, dragging, narrow, memoryOpen, memoryStamp, toggleMemory, threadComp, composer, canSend,
             toggleTool, pickFiles, pickFolderFiles, closeFolder, folderReady, folderCount, attachOpen,
             toolsOpen, hostedShown, devicesShown, hostedOn, activeTools, deviceOn, toggleDevice,
             promptOpen, hiddenPrompt, hostedCost, familyCost, toolsCost, showPrompt,
             canPick, pickFolder, generateMore, moreCount, assets, assetsOpen, assetKey, toggleAssets, locateAsset, zoomIndex, zoomGo, artifactsOpen, artifactId, openArtifact, toggleArtifacts,
             linuxOpen, linuxOffered, toggleLinux,
             notifyOn, toggleNotify, notifySupported: notify.supported(),
             voiceName, voiceSupported, playing, listen,
             sheet, sheetOpen, cardAct, cardView, keptView, keptAct, keptNotes, body, keptComponent, keptBelow, cardComponent, cardPlacement, cardPending, folderCtx,
             download, downloadOut, extOf,
             dropZone, onPaste, send, stop, queue, resume, unqueue, regenerate, edit, onKey, html,
             copyText, onThreadClick, onScroll, onImageLoad, argSummary, preview, thinking, busyTool, timeline, md, candidateOf: candidate, num, size, when,
             host, context, contextTitle, ctxParts, sentFrom, autoCompact, ctxOpen, compactNow, uncompact,
             retryCompacted, tooLong, editing, cancelEdit, commandHints, useCommand, kTokens: Compact.kTokens, estimate: Compact.estimate };
  },
};

export default ChatPanel;
</script>

<template>
  <div class="panel chat-off" v-if="!chat.enabled">
    <div class="empty">
      <div class="big">Le chat n'est pas ouvert sur ce proxy</div>
      <div>Ajouter <code>[ui]</code> <code>chat = true</code> dans
        <code>config.toml</code> puis redémarrer. Les requêtes du chat sont
        comptées à la clé connectée, comme celles de l'API.</div>
    </div>
  </div>
  <div class="panel chat" v-else
       :class="{ 'with-memory': (memoryOpen && $props.session.memory) || (artifactsOpen && current && (current.artifacts || []).length)
                   || (linuxOpen && current) || (assetsOpen && assets.length),
                 'side-wide': artifactsOpen || linuxOpen || (assetsOpen && assets.length), incognito: current && current.incognito }">
    <section class="chat-main chat-loading" v-if="!current" :aria-busy="String(!loadError)">
      <header class="chat-head">
        <h2>{{ pendingConv ? pendingConv.title : "Conversations" }}</h2>
      </header>
      <div class="thread">
        <div class="empty" v-if="loadError">
          <div>{{ loadError }}</div>
          <div><button type="button" class="btn ghost" @click="retryLoad">Réessayer</button></div>
        </div>
        <div class="loading-thread" v-else role="status">
          <div class="loading-line"><span class="spinner" aria-hidden="true"></span>
            {{ pendingConv ? "Chargement de la conversation…" : "Chargement des conversations…" }}</div>
          <div class="skeleton-msg user" aria-hidden="true"></div>
          <div class="skeleton-msg" aria-hidden="true"><i></i><i></i><i></i></div>
          <div class="skeleton-msg user short" aria-hidden="true"></div>
        </div>
      </div>
    </section>
    <section class="chat-main" v-if="current" ref="dropZone">
      <chat-header :current="current" :models="models" :model-groups="modelGroups"
                   :models-loading="modelsLoading" :models-title="modelsTitle" :streaming="streaming"
                   :artifacts-open="artifactsOpen" :assets-count="assets.length" :assets-open="assetsOpen"
                   :linux-offered="linuxOffered" :linux-open="linuxOpen"
                   :memory-enabled="!!$props.session.memory" :memory-open="memoryOpen"
                   :settings-open="settingsOpen"
                   @update:model="current.model = $event"
                   @reload-models="reloadModels" @toggle-incognito="toggleIncognito"
                   @toggle-artifacts="toggleArtifacts()" @toggle-assets="toggleAssets()"
                   @toggle-linux="toggleLinux()" @toggle-memory="toggleMemory()"
                   @toggle-settings="settingsOpen = !settingsOpen"></chat-header>

      <chat-settings v-if="settingsOpen" :current="current"
                     v-model:prompt-open="promptOpen" :hidden-prompt="hiddenPrompt"
                     :notify-supported="notifySupported" :notify-on="notifyOn"
                     @update:system="current.system = $event"
                     @update:temperature="current.temperature = $event"
                     @update:show-reasoning="current.showReasoning = $event"
                     @toggle-notify="toggleNotify" @export="exportMd"></chat-settings>
      <p class="chat-incognito" v-if="current.incognito">Incognito : rien n'est gardé, ni dans
        ce navigateur ni par le proxy (ni mémoire, ni cache), et la conversation disparaît dès
        que vous la quittez ou rechargez la page.</p>
      <p class="chat-warn" v-if="modelsError">{{ modelsError }}</p>
      <p class="chat-warn" v-if="storageWarning">{{ storageWarning }}</p>
      <p class="chat-warn" v-if="conflict" role="alert">« {{ conflict.title }} » a été modifiée dans un autre onglet
        ou sur un autre appareil ; ses changements ici ne sont plus enregistrés.
        <button type="button" class="link" @click="resolveConflict('reload')">Reprendre l'autre version</button> ·
        <button type="button" class="link" @click="resolveConflict('overwrite')">Garder celle-ci</button></p>

      <chat-thread ref="threadComp" :current="current" :sent-from="sentFrom" :streaming="streaming"
                   :image-mode="imageMode" :voice-mode="voiceMode" :asr-mode="asrMode"
                   :just-spoken="justSpoken" :voice-supported="voiceSupported" :voice-name="voiceName"
                   :playing="playing" :upscale-opt="upscaleOpt"
                   v-model:upscale-factor="upscaleFactor" v-model:upscale-strength="upscaleStrength"
                   v-model:more-count="moreCount" :kept-notes="keptNotes"
                   :html="html" :md="md" :body="body" :candidate-of="candidateOf"
                   :thinking="thinking" :preview="preview" :busy-tool="busyTool"
                   :arg-summary="argSummary" :ext-of="extOf" :host="host"
                   :too-long="tooLong" :strength-text="strengthText"
                   :kept-component="keptComponent" :kept-below="keptBelow" :kept-view="keptView"
                   :card-placement="cardPlacement" :card-pending="cardPending"
                   :card-component="cardComponent" :card-view="cardView" :folder-ctx="folderCtx"
                   @scroll="onScroll" @thread-click="onThreadClick" @only-one="onlyOne"
                   @zoom="zoom = $event" @image-load="onImageLoad" @open-sheet="sheetOpen = true"
                   @card-act="(m, a, p) => cardAct(m, a, p)" @kept-act="(m, k, a) => keptAct(m, k, a)"
                   @open-out="openOut" @uncompact="uncompact" @edit="edit"
                   @resume-interrupted="resumeInterrupted" @continue-reply="continueReply"
                   @retry-compacted="retryCompacted" @copy-text="(t, el) => copyText(t, el)"
                   @listen="listen" @retouch="retouch" @upscale="upscale"
                   @generate-more="generateMore" @regenerate="regenerate"></chat-thread>

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

      <chat-composer ref="composer" :current="current" :chat="chat" :caps="caps"
                     v-model:draft="draft" :queue="queue" :pending="pending"
                     :streaming="streaming" :dragging="dragging" :narrow="narrow"
                     :editing="editing" :command-hints="commandHints" :can-send="canSend"
                     :status="status" :blind="blind" :unsent="unsent"
                     :image-mode="imageMode" :voice-mode="voiceMode" :asr-mode="asrMode" :gen-mode="genMode"
                     :image-sizes="imageSizes" :image-steps="imageSteps" :image-loras="imageLoras"
                     :voice-choices="voiceChoices" :rec="rec"
                     :folder-ready="folderReady" :folder-count="folderCount" :can-pick="canPick"
                     v-model:attach-open="attachOpen" v-model:tools-open="toolsOpen"
                     :hosted-shown="hostedShown" :devices-shown="devicesShown" :hosted-on="hostedOn"
                     :active-tools="activeTools" :device-on="deviceOn"
                     :hosted-cost="hostedCost" :family-cost="familyCost" :tools-cost="toolsCost"
                     :context="context" :context-title="contextTitle" :ctx-parts="ctxParts"
                     v-model:ctx-open="ctxOpen" v-model:auto-compact="autoCompact"
                     @send="send" @stop="stop" @resume="resume" @unqueue="unqueue"
                     @remove-queue="queue.splice($event, 1)" @remove-pending="pending.splice($event, 1)"
                     @pick-files="pickFiles" @pick-folder="pickFolder" @pick-folder-files="pickFolderFiles"
                     @close-folder="closeFolder" @cancel-edit="cancelEdit" @use-command="useCommand"
                     @keydown="onKey" @paste="onPaste" @toggle-record="toggleRecord"
                     @toggle-tool="toggleTool" @toggle-device="toggleDevice" @show-prompt="showPrompt"
                     @new-voice="newVoice" @compact-now="compactNow"
                     @set-conv="(k, v) => (current[k] = v)"></chat-composer>
    </section>
    <memory-panel v-if="memoryOpen && $props.session.memory" compact
                  :stamp="memoryStamp" @close="toggleMemory(false)"></memory-panel>
    <artifact-panel v-else-if="artifactsOpen && current && (current.artifacts || []).length"
                    :artifacts="current.artifacts" :selected="artifactId"
                    @select="artifactId = $event" @close="toggleArtifacts(false)"></artifact-panel>
    <asset-panel v-else-if="assetsOpen && assets.length" :images="assets" :selected="assetKey" :busy="streaming"
                 @select="assetKey = $event" @retouch="retouch" @locate="locateAsset" @close="toggleAssets(false)"></asset-panel>
    <linux-panel v-else-if="linuxOpen && current" :conv="current" @close="toggleLinux(false)"></linux-panel>
    <chat-modals :current="current" :zoom="zoom" :zoom-index="zoomIndex" :assets-count="assets.length"
                 :streaming="streaming" :upscale-opt="upscaleOpt"
                 v-model:upscale-factor="upscaleFactor" v-model:upscale-strength="upscaleStrength"
                 :strength-text="strengthText" :studio-open="studioOpen" :voice-lang="voiceLang"
                 :file-preview="filePreview" :preview-copied="previewCopied"
                 @close-zoom="zoom = null" @zoom-go="zoomGo" @retouch="retouch" @upscale="upscale"
                 @voice-saved="voiceSaved" @close-studio="studioOpen = false"
                 @copy-preview="copyPreview" @download-out="downloadOut" @close-preview="filePreview = null"></chat-modals>
  </div>
</template>
