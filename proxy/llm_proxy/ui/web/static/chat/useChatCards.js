// @ts-check
// Composable des cartes d'outils (en attente, feuille du bas, cartes durables)
// et des fichiers produits (aperçu, téléchargement).
import { ref, reactive, computed, watch } from "vue";
import * as tools from "./tools/index.js";
import { download, useCopy } from "../lib/files.js";
import { Markdown } from "../lib/markdown.js";
import { pushStep } from "./turn.js";

const PREVIEW_MAX = 500_000;

export function useChatCards({
  current, folders, folderTick, modelEntry, streaming, status, onPin, respond,
}) {
  function toolCtx(conv, live, signal = undefined) {
    return {
      conv, live, ws: folders.get(conv.id) || null,
      vision: !!(modelEntry(conv.model) || {}).vision, download, signal,
    };
  }

  function settle(m, result) {
    const c = current.value;
    if (!c || streaming.value || !m.waiting) return;
    m.waiting.result = result;
    onPin();
    respond(c, m);
  }

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
      (m.attachments || (m.attachments = [])).push({
        kind: "image", name: out.name || "image.jpg", data: out.image,
      });
      w.image = out.image;
    }
    settle(m, out.result);
  }

  const cardView = (w) => tools.view(w);
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

  const sheet = computed(() => {
    const ms = (current.value && current.value.messages) || [];
    const m = ms[ms.length - 1];
    return m && m.waiting && tools.placement(m.waiting) === "sheet" ? m : null;
  });
  const sheetOpen = ref(true);
  watch(() => sheet.value && sheet.value.waiting.id, (id) => { if (id) sheetOpen.value = true; });

  const extOf = (name) => ((String(name).match(/\.([a-z0-9]{1,6})$/i) || [])[1] || "txt").toUpperCase();
  const filePreview = ref(null);
  const { copied: previewCopied, copy: copyPreview } = useCopy();

  async function contentOut(f) {
    const content = await tools.fileContent(f);
    if (content === null || content === undefined) {
      status.value = `${f.name} : introuvable dans ce navigateur (supprimé, ou créé sur un autre).`;
    }
    return content;
  }

  async function downloadOut(f) {
    const content = await contentOut(f);
    if (content !== null && content !== undefined) download({ name: f.name, content }, f.type);
  }

  async function openOut(f) {
    const content = await contentOut(f);
    if (content === null || content === undefined) return;
    const binary = typeof content !== "string" && content.subarray(0, 8192).includes(0);
    if (binary) { download({ name: f.name, content }, f.type); return; }
    const text = typeof content === "string" ? content : new TextDecoder().decode(content);
    const shown = text.length > PREVIEW_MAX ? text.slice(0, PREVIEW_MAX) : text;
    const md = /\.(md|markdown)$/i.test(f.name);
    filePreview.value = {
      f, name: f.name, text, shown, cut: text.length - shown.length,
      html: md ? Markdown.render(shown) : "",
    };
  }

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
    pushStep(m, m.step);
    m.waiting = null;
  }

  return {
    toolCtx, cardAct, cardView, keptView, keptNotes, keptAct, cardComponent,
    cardPlacement, cardPending, folderCtx, sheet, sheetOpen, extOf, filePreview,
    previewCopied, copyPreview, downloadOut, openOut, abandonWaiting,
  };
}
