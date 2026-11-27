<script>
// @ts-check
/*
  Configuration → Quotas (admin) : le limiteur de chaque backend à quotas
  (/ui/quotas, backend_routes.py). Par bucket — un routeur du compte, une
  règle du backend, les en-têtes de l'amont, une famille, un générique —
  une barre par fenêtre (minute, jour) : la part prise du plafond
  (requêtes ou tokens, la plus haute), et, la fenêtre presque pleine, le
  délai avant que sa plus ancienne requête la quitte (une place se
  libère). Le restant annoncé par le compte (Albert) ou par les en-têtes
  des réponses (Mistral, OpenAI, Groq, Anthropic), les 429 reçus de
  l'amont ; le forfait mensuel du compte (Mistral : API, Vibe & Studio).
  Seul ce qui est reçu ou défini s'affiche : une fenêtre sans plafond, un
  bucket sans fenêtre ni restant, un backend sans rien de tout cela sont
  omis. Relu toutes les REFRESH_MS ; « Relire les limites » interroge le
  compte tout de suite.
*/
import * as Vue from "vue";
import { call } from "../lib/http.js";

const { ref, onMounted, onUnmounted } = Vue;

const REFRESH_MS = 10_000;
const SOURCES = { compte: "compte", backend: "règle", "en-têtes": "en-têtes", famille: "famille",
                  "générique": "générique" };

function short(n) {
  if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + " M";
  if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + " k";
  return String(n);
}
function delay(s) {
  if (!s) return "";
  if (s < 60) return `${Math.ceil(s)} s`;
  const min = Math.ceil(s / 60);
  if (min < 60) return `${min} min`;
  return min % 60 ? `${Math.floor(min / 60)} h ${min % 60} min` : `${min / 60} h`;
}
/** Une fenêtre : sa part prise (0 à 1, la plus haute des deux), son texte. */
function gauge(w) {
  const parts = [];
  let used = 0;
  if (w.max_requests) { used = Math.max(used, w.requests / w.max_requests); parts.push(`${w.requests}/${short(w.max_requests)} req`); }
  if (w.max_tokens) { used = Math.max(used, w.tokens / w.max_tokens); parts.push(`${short(w.tokens)}/${short(w.max_tokens)} tok`); }
  return { used: Math.min(used, 1), limited: parts.length > 0, text: parts.join(" · ") };
}
const BUDGETS = { api: ["API", "API"], vibe: ["Vibe", "Vibe & Studio"] };
function money(amount, currency) {
  if (currency) {
    try { return new Intl.NumberFormat(undefined, { style: "currency", currency }).format(amount); }
    catch { return `${amount.toFixed(2)} ${currency}`; }
  }
  return amount.toFixed(2);
}
/** Un forfait : consommé / alloué, la date de réinitialisation, le paiement à l'usage. */
function budgetText(f) {
  const parts = [`${money(f.initial * f.used_percent / 100, f.currency)} / ${money(f.initial, f.currency)}`];
  const reset = f.reset_at ? new Date(f.reset_at) : null;
  if (reset && !isNaN(reset.getTime())) parts.push(`réinitialisation le ${reset.toLocaleDateString()}`);
  if (f.payg) parts.push("puis paiement à l'usage");
  return parts.join(" · ");
}
const WINDOWS = [["minute", "minute"], ["jour", "day"]];
/** Les fenêtres plafonnées d'un bucket : [libellé, fenêtre]. */
const windows = (q) => WINDOWS.filter(([, k]) => gauge(q[k]).limited).map(([label, k]) => [label, q[k]]);
/** Un backend, ses seuls buckets reçus ou définis ; null s'il n'a rien à montrer. */
function shown(b) {
  const buckets = b.buckets.filter((q) => windows(q).length || left(q.remaining));
  return buckets.length || b.budget.length || b.upstream_429 ? { ...b, buckets } : null;
}
function left(remaining) {
  const keys = Object.keys(remaining || {});
  return keys.length ? keys.map((k) => `${short(remaining[k])} ${k}`).join(" · ") : "";
}

const QuotasPart = {
  setup() {
    const list = ref([]);
    const error = ref("");
    const loading = ref(false);
    const loaded = ref(false);
    let timer = null;

    async function load(refresh = false) {
      loading.value = refresh;
      try {
        const d = await call("GET", "/ui/quotas" + (refresh ? "?refresh=1" : ""));
        list.value = (d.backends || []).map(shown).filter(Boolean);
        error.value = "";
        loaded.value = true;
      } catch (e) { error.value = e.message; }
      loading.value = false;
    }
    onMounted(() => { load(); timer = setInterval(() => load(), REFRESH_MS); });
    onUnmounted(() => clearInterval(timer));
    const read = (t) => t ? new Date(t * 1000).toLocaleTimeString() : "";
    return { list, error, loading, loaded, load, gauge, windows, delay, left, read, SOURCES, BUDGETS, budgetText };
  },
};

export default QuotasPart;
</script>

<template>
    <div class="config-keys quotas">
      <p class="err" v-if="error" role="status">{{ error }}</p>
      <section class="panel">
        <div class="panel-head"><h2>Quotas</h2>
          <div class="item-actions">
            <button type="button" class="btn" :disabled="loading" @click="load(true)">Relire les limites</button></div></div>
        <p class="note" v-if="loaded && !error && !list.length">Aucune limite reçue ni définie.</p>
      </section>
      <section class="panel" v-for="b in list" :key="b.name + b.by">
        <div class="panel-head"><h2><code>{{ b.name }}/</code></h2>
          <span class="dim">{{ b.by || "config.toml" }}<template v-if="b.account_read"> · limites du compte lues à {{ read(b.account_read) }}</template></span></div>
        <p class="err" v-if="b.upstream_429" role="status">{{ b.upstream_429 }} réponse(s) 429 du backend malgré le
          limiteur : ses plafonds réels sont plus bas que ceux du proxy.</p>
        <div class="quota-rows" v-if="b.budget.length">
          <div class="quota-row">
            <div class="quota-name"><strong>Forfait mensuel</strong>
              <span class="dim" v-if="b.budget_read">lu à {{ read(b.budget_read) }}</span></div>
            <div class="quota-line" v-for="f in b.budget" :key="f.kind">
              <span class="quota-label" :title="(BUDGETS[f.kind] || [f.kind, f.kind])[1]">{{ (BUDGETS[f.kind] || [f.kind])[0] }}</span>
              <span class="quota-bar" role="meter" :aria-label="(BUDGETS[f.kind] || [f.kind, f.kind])[1]"
                    :aria-valuenow="Math.round(f.used_percent)" aria-valuemin="0" aria-valuemax="100">
                <i :style="{ width: f.used_percent + '%' }" :class="{ high: f.used_percent >= 80 }"></i></span>
              <span class="quota-text long">{{ budgetText(f) }}</span>
            </div>
          </div>
        </div>
        <div class="quota-rows">
          <div class="quota-row" v-for="q in b.buckets" :key="q.key">
            <div class="quota-name"><strong>{{ q.name }}</strong>
              <span class="tag">{{ SOURCES[q.source] || q.source }}</span>
              <span class="dim" v-if="left(q.remaining)">reste au compte : {{ left(q.remaining) }}</span></div>
            <div class="quota-line" v-for="[label, w] in windows(q)" :key="label">
              <span class="quota-label">{{ label }}</span>
              <span class="quota-bar" role="meter" :aria-label="label"
                    :aria-valuenow="Math.round(gauge(w).used * 100)" aria-valuemin="0" aria-valuemax="100">
                <i :style="{ width: (gauge(w).used * 100) + '%' }" :class="{ high: gauge(w).used >= 0.8 }"></i></span>
              <span class="quota-text">{{ gauge(w).text }}<template v-if="w.frees_in && gauge(w).used >= 0.8"> · place libre dans {{ delay(w.frees_in) }}</template></span>
            </div>
          </div>
        </div>
      </section>
    </div>
</template>
