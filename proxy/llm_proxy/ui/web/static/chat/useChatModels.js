// @ts-check
// Composable des modèles du chat : catalogue du proxy + navigateur, groupes
// du sélecteur, capacités, modes (image, voix, ASR), options d'images et
// modèle par défaut.
import { ref, computed, watch } from "vue";
import { useLocalStorage } from "@vueuse/core";
import { availableBrowserModels, browserModel, modelCaps } from "./browser.js";
import { catalog, catalogState, refreshCatalog } from "../lib/catalog.js";
import * as speech from "./voice/speech.js";
import { role as voiceRole } from "./voice/proxy.js";

const LAST_MODEL = "llm-proxy-chat-model";

// La liste du premier motif d'une table de [tools.image_generation] (`steps`,
// `loras`) qui couvre le nom du modèle ("*" = n'importe quoi) ; aucun motif :
// liste vide, pas de choix.
function listOf(table, model) {
  for (const [pat, list] of Object.entries(table || {})) {
    const re = new RegExp("^" + pat.split("*").map((p) => p.replace(/[.+?^${}()|[\]\\]/g, "\\$&")).join(".*") + "$");
    if (re.test(model || "")) return list;
  }
  return [];
}

export function useChatModels({ chat, current, pending, onUnauthorized }) {
  const lastModel = useLocalStorage(LAST_MODEL, "");
  const browserModels = ref([]);

  const models = computed(() => catalogState.data
    .filter((m) => m.chat || m.image || m.type === "text-to-speech" || m.type === "automatic-speech-recognition")
    .map((m) => (m.type === "text-to-speech" ? { ...m, voice: voiceRole(m.id) }
      : m.type === "automatic-speech-recognition" ? { ...m, asr: true } : m))
    .sort((a, b) => a.id.localeCompare(b.id)).concat(browserModels.value));

  const modelIds = computed(() => models.value.filter((m) => !m.image && !m.voice && !m.asr).map((m) => m.id));
  const modelsError = computed(() => catalogState.error && !catalogState.data.length
    ? "Catalogue illisible : " + catalogState.error : "");
  const modelsLoading = computed(() => catalogState.loading);
  const modelsTitle = computed(() => catalogState.loading ? "Lecture du catalogue…"
    : catalogState.error ? "Catalogue illisible (" + catalogState.error + ") — relire"
    : "Relire le catalogue" + (catalogState.at ? " (lu à " + new Date(catalogState.at)
      .toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" }) + ")" : ""));

  const modelEntry = (id) => models.value.find((m) => m.id === id) || browserModel(id);
  const caps = computed(() => modelCaps(current.value && modelEntry(current.value.model)));

  const modelGroups = computed(() => {
    const groups = new Map();
    for (const m of models.value) {
      const cut = m.id.indexOf("/");
      const from = m.group || (cut > 0 ? m.id.slice(0, cut) : "Autres");
      const label = m.image ? "Images · " + from : m.voice ? "Voix · " + from : m.asr ? "Transcription · " + from : from;
      if (!groups.has(label)) groups.set(label, { label, rank: m.asr ? 4 : m.voice ? 3 : m.image ? 2 : m.browser ? 1 : 0, models: [] });
      const name = m.name || (cut > 0 ? m.id.slice(cut + 1) : m.id);
      const notes = [m.vision && !m.image && "vision", m.voice && speech.ROLE_LABELS[m.voice],
                     m.availability && m.availability !== "available" && "à télécharger"];
      groups.get(label).models.push({ id: m.id, text: [name, ...notes.filter(Boolean)].join(" · ") });
    }
    return [...groups.values()].sort((a, b) => a.rank - b.rank);
  });

  const isImage = (id) => !!(models.value.find((m) => m.id === id) || {}).image;
  const imageMode = computed(() => !!current.value && isImage(current.value.model));
  const voiceOf = (id) => (models.value.find((m) => m.id === id) || {}).voice || "";
  const voiceMode = computed(() => (current.value ? voiceOf(current.value.model) : ""));
  const isAsr = (id) => !!(models.value.find((m) => m.id === id) || {}).asr;
  const asrMode = computed(() => !!current.value && isAsr(current.value.model));
  const genMode = computed(() => imageMode.value || !!voiceMode.value || asrMode.value);

  const imageSizes = computed(() => chat.value.image_sizes || ["512x512"]);
  const stepsOf = (model) => listOf(chat.value.image_steps, model);
  const imageSteps = computed(() => (current.value ? stepsOf(current.value.model) : []));
  const lorasOf = (model) => listOf(chat.value.image_loras, model);
  const imageLoras = computed(() => (current.value ? lorasOf(current.value.model) : []));

  function defaultModel() {
    const preset = chat.value.model;
    if (preset && modelIds.value.includes(preset)) return preset;
    const last = lastModel.value;
    if (last && modelIds.value.includes(last)) return last;
    const first = models.value.find((m) => !m.image && !m.voice && !m.asr && m.byDefault !== false);
    return (first && first.id) || last || "";
  }

  const blind = computed(() => {
    const c = current.value;
    const m = c && models.value.find((x) => x.id === c.model);
    return !!(m && !m.vision && !m.image && pending.value.some((a) => a.kind === "image"));
  });

  const unsent = computed(() => (imageMode.value ? pending.value.some((a) => a.kind !== "image")
    || pending.value.filter((a) => a.kind === "image").length > 1
    : voiceMode.value ? pending.value.length > 0
    : asrMode.value ? pending.value.some((a) => a.kind !== "audio")
    : pending.value.some((a) => a.kind === "audio")));

  async function loadModels() {
    availableBrowserModels().then((got) => { browserModels.value = got; });
    try {
      await catalog();
      if (current.value && !current.value.model) current.value.model = defaultModel();
    } catch (e) {
      if (e.status === 401) onUnauthorized();
    }
  }

  async function reloadModels() {
    availableBrowserModels().then((got) => { browserModels.value = got; });
    try { await refreshCatalog(); } catch (e) { if (e.status === 401) onUnauthorized(); }
  }

  watch(() => current.value && current.value.model, (m) => {
    if (m && !isImage(m) && !voiceOf(m) && !isAsr(m)) lastModel.value = m;
  });

  return {
    models, modelIds, modelsError, modelsLoading, modelsTitle, modelEntry,
    caps, modelGroups, isImage, imageMode, voiceOf, voiceMode, isAsr, asrMode,
    genMode, imageSizes, stepsOf, imageSteps, lorasOf, imageLoras, defaultModel,
    blind, unsent, loadModels, reloadModels,
  };
}
