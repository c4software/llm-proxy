// @ts-check
/*
  `ask_user` : le modèle pose de 1 à 4 questions à choix, et sa réponse
  ATTEND la vôtre. Chaque question a ses options (choix unique ou
  multiple, chacune avec sa description) et toujours « Autre… », en texte
  libre ; un onglet par question (`header`). La carte s'affiche dans la
  FEUILLE du bas (AskSheet) ; elle vit dans le message (m.waiting) et
  survit à un rechargement. Écrire un nouveau message au lieu de répondre
  clôt la question, sans réponse.

  Plusieurs questions : un onglet par question, puis un RÉCAPITULATIF
  (chaque question et sa réponse, en une ligne : un clic y revient) ; le
  bouton principal fait « Suivant » dès que la question a sa réponse,
  « Envoyer les réponses » sur le récapitulatif.

  Le clavier, dans la feuille : 1…9 choisit, ←/→ change d'onglet, Entrée
  fait le bouton principal (Suivant, Envoyer), Échap réduit.
*/
import { TOOL, FAMILY, PageTool, defineFamily, failure } from "../contract.js";

const { ref, computed, watch, nextTick, onMounted } = Vue;

// Une option : {label, description} — un modèle qui écrit une simple
// chaîne est accepté aussi.
/** @param {any} o */
function option(o) {
  if (o && typeof o === "object") {
    return { label: String(o.label || "").trim(), description: String(o.description || "").trim() };
  }
  return { label: String(o == null ? "" : o).trim(), description: "" };
}

/** @param {any} q */
const answered = (q) => q.picked.length > 0 || (q.other || "").trim() !== "";

// La feuille : les onglets, la question montrée, ses options.
// La réponse d'une question, en une ligne (le récapitulatif).
/** @param {any} q */
const answerOf = (q) => [...q.picked, ...((q.other || "").trim() ? [`« ${q.other.trim()} »`] : [])].join(", ");

// La feuille : un onglet par question et, s'il y en a plusieurs, un
// RÉCAPITULATIF en dernier. Le bouton principal fait « Suivant » dès que
// la question a sa réponse (un choix unique y passe tout seul), puis
// « Envoyer les réponses » sur le récapitulatif.
const AskSheet = {
  props: ["w", "busy", "ctx"],
  emits: ["act", "minimize"],
  setup(props, { emit }) {
    const tab = ref(0);
    const root = ref(null);
    const qs = computed(() => props.w.questions);
    const multi = computed(() => qs.value.length > 1);
    const tabs = computed(() => qs.value.length + (multi.value ? 1 : 0));
    const recap = computed(() => multi.value && tab.value === qs.value.length);
    const q = computed(() => (recap.value ? null : qs.value[Math.min(tab.value, qs.value.length - 1)] || null));
    const ready = computed(() => qs.value.every(answered));
    const count = computed(() => qs.value.filter(answered).length);
    function focus(selector) {
      nextTick(() => {
        const el = root.value && root.value.querySelector(selector);
        if (el) { el.focus({ preventScroll: true }); el.scrollIntoView({ block: "nearest" }); }
      });
    }
    function show(k) {
      tab.value = (k + tabs.value) % tabs.value;
      focus(recap.value ? ".sheet-submit" : ".ask-opt");
    }
    // La suite : la question suivante sans réponse, sinon le récapitulatif
    // (ou, pour une question seule, le bouton d'envoi).
    function advance() {
      if (!multi.value) { focus(".sheet-submit"); return; }
      for (let k = tab.value + 1; k < qs.value.length; k++) {
        if (!answered(qs.value[k])) { show(k); return; }
      }
      show(qs.value.length);
    }
    function pick(x, label) {
      if (props.busy) return;
      if (x.multiple) {
        x.picked = x.picked.includes(label) ? x.picked.filter((o) => o !== label) : [...x.picked, label];
        return;
      }
      x.picked = [label];
      x.otherOn = false;
      x.other = "";
      advance();
    }
    function pickOther(x) {
      if (props.busy) return;
      if (x.multiple && (x.otherOn || x.other)) { x.otherOn = false; x.other = ""; return; }
      if (!x.multiple) x.picked = [];
      x.otherOn = true;
      focus(".ask-other");
    }
    const send = () => emit("act", "answer");
    // Le bouton principal : Suivant (une question répondue), ou Envoyer.
    const primary = computed(() => {
      if (!multi.value) return { label: "Envoyer la réponse", disabled: !ready.value, next: false };
      if (recap.value) return { label: "Envoyer les réponses", disabled: !ready.value, next: false };
      return { label: "Suivant", disabled: !q.value || !answered(q.value), next: true };
    });
    function onPrimary() {
      if (props.busy || primary.value.disabled) return;
      if (primary.value.next) show(tab.value + 1);
      else send();
    }
    function onKey(e) {
      if (e.key === "Escape") { emit("minimize"); return; }
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const typing = e.target.tagName === "INPUT";
      if (e.key === "Enter" && (typing || recap.value || e.target === root.value)) {
        e.preventDefault();
        onPrimary();
        return;
      }
      if (typing) return;
      if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
        e.preventDefault();
        show(tab.value + (e.key === "ArrowRight" ? 1 : -1));
        return;
      }
      const x = q.value;
      if (x && /^[1-9]$/.test(e.key)) {
        const n = Number(e.key) - 1, opts = x.options.map(option);
        if (n < opts.length) { e.preventDefault(); pick(x, opts[n].label); }
        else if (n === opts.length) { e.preventDefault(); pickOther(x); }
      }
    }
    // Une nouvelle question : le premier onglet, la première option.
    watch(() => props.w.id, () => { tab.value = 0; focus(".ask-opt"); });
    onMounted(() => focus(".ask-opt"));
    return { root, tab, qs, multi, recap, q, ready, count, show, pick, pickOther, primary, onPrimary,
             onKey, answered, option, answerOf };
  },
  template: `
    <div class="sheet-inner" ref="root" tabindex="-1" @keydown="onKey">
      <header class="sheet-head">
        <div class="sheet-tabs" role="tablist">
          <button type="button" role="tab" v-for="(x, k) in qs" :key="k"
                  :aria-selected="String(tab === k)" :class="{ done: answered(x) }"
                  @click="show(k)" :title="x.question">
            <svg class="ico" viewBox="0 0 24 24" aria-hidden="true" v-if="answered(x)"><path d="M5 12l5 5 9-10"/></svg>
            {{ x.header || "Question " + (k + 1) }}</button>
          <button type="button" role="tab" v-if="multi" :aria-selected="String(recap)" :class="{ done: ready }"
                  @click="show(qs.length)" title="Toutes les réponses, avant de les envoyer">Récapitulatif</button>
        </div>
        <button type="button" class="sheet-x" @click="$emit('minimize')"
                title="Réduire pour relire le fil ou écrire autre chose (Échap)" aria-label="Réduire">
          <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg></button>
      </header>
      <div class="sheet-body" role="tabpanel" v-if="recap">
        <ul class="ask-recap">
          <li v-for="(x, k) in qs" :key="k">
            <button type="button" class="ask-recap-row" :class="{ missing: !answered(x) }" @click="show(k)"
                    :title="x.question + ' — cliquer pour changer'">
              <b>{{ x.header || "Question " + (k + 1) }}</b>
              <span class="ask-recap-answer">{{ answered(x) ? answerOf(x) : "sans réponse — choisir" }}</span>
              <span class="ask-recap-q dim">{{ x.question }}</span>
            </button>
          </li>
        </ul>
      </div>
      <div class="sheet-body" role="tabpanel" v-else-if="q">
        <p class="ask-text">{{ q.question }}<span class="dim" v-if="q.multiple"> · plusieurs choix</span></p>
        <div class="ask-list" :role="q.multiple ? 'group' : 'radiogroup'">
          <button type="button" class="ask-opt" v-for="(o, n) in q.options.map(option)" :key="o.label"
                  :role="q.multiple ? 'checkbox' : 'radio'" :aria-checked="String(q.picked.includes(o.label))"
                  :class="{ box: q.multiple }" :disabled="busy" @click="pick(q, o.label)">
            <span class="mark" aria-hidden="true"></span>
            <span class="opt-text"><b>{{ o.label }}</b><small v-if="o.description">{{ o.description }}</small></span>
            <kbd v-if="n < 9">{{ n + 1 }}</kbd>
          </button>
          <button type="button" class="ask-opt" :role="q.multiple ? 'checkbox' : 'radio'"
                  :aria-checked="String(!!(q.otherOn || q.other))" :class="{ box: q.multiple }"
                  :disabled="busy" @click="pickOther(q)">
            <span class="mark" aria-hidden="true"></span>
            <span class="opt-text"><b>Autre</b><small>une réponse libre</small></span>
            <kbd v-if="q.options.length < 9">{{ q.options.length + 1 }}</kbd>
          </button>
          <input class="ask-other" v-if="q.otherOn || q.other" v-model="q.other"
                 :disabled="busy" placeholder="Votre réponse…">
        </div>
      </div>
      <footer class="sheet-foot">
        <button type="button" class="btn sheet-submit" :disabled="busy || primary.disabled" @click="onPrimary">{{ primary.label }}
          <span class="count" v-if="multi">{{ count }}/{{ qs.length }}</span></button>
        <button type="button" class="btn ghost" :disabled="busy" @click="$emit('act', 'skip')">Passer</button>
      </footer>
    </div>`,
};

class AskUser extends PageTool {
  constructor() {
    super({
      name: TOOL.ASK_USER,
      description: "Pose à l'utilisateur de 1 à 4 questions à choix, dans le chat, et attend "
        + "sa réponse avant de continuer. À utiliser quand une décision lui revient ou qu'une "
        + "précision manque pour bien répondre — pas pour des confirmations inutiles. Chaque "
        + "question a 2 à 6 options courtes, chacune avec une brève description de ce "
        + "qu'elle implique ; l'utilisateur peut aussi répondre librement.",
      params: {
        questions: { type: "array", minItems: 1, maxItems: 4, items: {
          type: "object", required: ["question", "header", "options"], properties: {
            question: { type: "string", description: "La question, complète." },
            header: { type: "string", description: "Un titre très court (1 à 3 mots), l'onglet de la question : « Budget », « Hébergement »." },
            options: { type: "array", minItems: 2, maxItems: 6, items: {
              type: "object", required: ["label"], properties: {
                label: { type: "string", description: "La réponse, courte." },
                description: { type: "string", description: "Ce qu'elle implique, en une phrase." },
              } } },
            multiple: { type: "boolean", description: "Plusieurs options peuvent être choisies." },
          } } },
      },
      required: ["questions"],
      placement: "sheet",
      component: AskSheet,
    });
  }

  /** @override */
  waits() { return true; }

  /** @override @param {any} args */
  prepare(args) {
    const qs = Array.isArray(args.questions) ? args.questions.slice(0, 4) : [];
    const questions = qs.filter((x) => x && typeof x.question === "string" && x.question.trim())
      .map((x, n) => ({
        question: x.question.trim(),
        header: String(x.header || "").trim().slice(0, 24) || `Question ${n + 1}`,
        options: (Array.isArray(x.options) ? x.options : []).map(option)
          .filter((o) => o.label).slice(0, 6),
        multiple: !!x.multiple, picked: [], other: "", otherOn: false,
      }));
    if (!questions.length) return { error: failure("`questions` attendu, de 1 à 4 questions avec leurs options.") };
    return { questions };
  }

  /** @override @param {import("../contract.js").Card} w @param {string} action */
  act(w, action) {
    if (action === "skip") return { result: "L'utilisateur a passé la question sans répondre." };
    if (!w.questions.every(answered)) return null;
    return { result: w.questions.map((q, n) => {
      const parts = [...q.picked];
      if ((q.other || "").trim()) parts.push("autre : « " + q.other.trim() + " »");
      return `${n + 1}. ${q.question}\n→ ${parts.length ? parts.join(" ; ") : "(sans réponse)"}`;
    }).join("\n") };
  }

  /** @override */
  notice() { return "Une question vous attend"; }

  /** @override @param {import("../contract.js").Card} w */
  pending(w) {
    const n = w.questions.length;
    return `${n > 1 ? n + " questions attendent" : "Une question attend"} votre réponse`;
  }

  /** @override @param {any} a */
  summary(a) {
    return Array.isArray(a.questions)
      ? a.questions.map((q) => (q && q.question) || "").filter(Boolean).join(" · ").slice(0, 140) : "";
  }
}

export const FAMILIES = [defineFamily({
  key: FAMILY.ASK, label: "Questions", hint: "des questions à choix, dans la feuille du bas", fixed: true,
  tools: [new AskUser()],
})];
