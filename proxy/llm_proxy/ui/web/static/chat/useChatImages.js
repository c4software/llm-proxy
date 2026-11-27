// @ts-check
// Composable lightbox, retouche et agrandissement d'images dans le chat.
import { ref, computed, watch, nextTick } from "vue";
import { onKeyStroke } from "@vueuse/core";
import { dataUrl, ownImage } from "./history.js";
import { oneImage, naturalSize, upscaleSize } from "./images.js";

export function useChatImages({ chat, current, assets, pending, streaming, input, uid, startRun, done, onUnauthorized }) {
  const zoom = ref(null);
  const zoomIndex = computed(() => (zoom.value ? assets.value.findIndex((a) => a.data === zoom.value.data) : -1));

  function zoomGo(step) {
    const list = assets.value;
    if (zoomIndex.value < 0 || list.length < 2) return;
    zoom.value = list[(zoomIndex.value + step + list.length) % list.length];
  }

  onKeyStroke(["ArrowLeft", "ArrowRight"], (e) => {
    if (!zoom.value || e.defaultPrevented) return;
    e.preventDefault();
    zoomGo(e.key === "ArrowLeft" ? -1 : 1);
  });

  const upscaleOpt = computed(() => chat.value.image_upscale || null);
  const upscaleFactor = ref(2);
  const upscaleStrength = ref(null);

  watch(upscaleOpt, (up) => {
    const all = (up && up.strengths) || [];
    if (!all.some((s) => s.value === upscaleStrength.value)) {
      upscaleStrength.value = all.length ? all[0].value : null;
    }
  }, { immediate: true });

  const strengthText = (s) => `${s.label} (${String(s.value).replace(".", ",")})`;

  async function upscale(a, factor = upscaleFactor.value) {
    const up = upscaleOpt.value, c = current.value;
    if (!up || !c || streaming.value || !ownImage(a.data)) return;
    zoom.value = null;
    const msg = { id: uid(), role: "assistant", content: "", reasoning: "",
                  tools: [], sources: [], attachments: [], image: true, prompt: "",
                  upscale: true, model: up.model, usage: null, error: "",
                  started: Date.now(), duration: 0 };
    c.messages.push(msg);
    c.updated = Date.now();
    const live = c.messages[c.messages.length - 1];
    const ctrl = startRun();
    live.want = 1;
    try {
      const image = await dataUrl(a.data);
      const { w, h } = await naturalSize(image);
      const size = upscaleSize(w, h, factor, up.max_pixels);
      if (!size) throw new Error(`Déjà à la taille maximale (${w} × ${h}).`);
      live.content = `*Agrandie ×${factor} : ${w} × ${h} → ${size.replace("x", " × ")}, redessinée par ${up.model}*`;
      const body = { model: up.model, prompt: up.prompt, size, n: 1, image };
      const force = upscaleStrength.value;
      if (force != null && force < 1) body.strength = force;
      const got = await oneImage(c, body, ctrl.signal, onUnauthorized);
      const { revised, ...img } = got;
      live.attachments.push(img);
    } catch (e) {
      live.error = e.name === "AbortError" ? "Arrêté." : e.message;
    } finally {
      delete live.want;
      done(c, live, ctrl);
    }
  }

  function retouch(a) {
    pending.value = [...pending.value.filter((p) => p.kind !== "image"),
      { kind: "image", name: a.name, type: a.type, size: a.size, data: a.data }];
    zoom.value = null;
    nextTick(() => { if (input.value) input.value.focus(); });
  }

  return {
    zoom, zoomIndex, zoomGo, upscaleOpt, upscaleFactor, upscaleStrength,
    strengthText, upscale, retouch,
  };
}
