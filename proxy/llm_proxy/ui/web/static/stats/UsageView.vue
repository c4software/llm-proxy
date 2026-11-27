<script>
// @ts-check
/*
  La vue USAGE : l'usage et les limites du client de la page, tels que les
  rend /ui/usage/agent (ui/agent_usage.py), l'enregistrement que lit aussi
  le panneau d'agents d'Omarchy par /v1/usage/agent (son service :
  Configuration → Clients). Les limites (part équitable, forfait mensuel de
  ses backends), les tokens des sept derniers jours et par modèle. À la
  différence des Statistiques, toujours le seul client de la page (son
  compte, admin compris ; tout pour un proxy ouvert).

  Relu toutes les REFRESH_MS, seulement quand la vue est affichée
  (`active`) et l'onglet visible.
*/
import * as Vue from "vue";
import { call } from "../lib/http.js";
import { num, TZ } from "../lib/format.js";

const { ref, computed, watch, onMounted, onUnmounted } = Vue;

const REFRESH_MS = 30_000;

const day = (d) => new Date(d + "T12:00:00").toLocaleDateString("fr-FR", { weekday: "short", day: "numeric" });
/** Une part (0 à 1) en pourcentage ; deux décimales sous 1 %. */
function percent(p) {
  if (p > 0 && p < 0.0001) return "< 0,01 %";
  return p.toLocaleString("fr-FR", { style: "percent", maximumFractionDigits: p < 0.01 ? 2 : 0 });
}

const UsageView = {
  props: {
    active: { type: Boolean, default: false },   // la vue affichée
  },
  emits: ["unauthorized"],
  setup(props, { emit }) {
    const rec = ref(null);
    const error = ref("");
    let timer = null;
    let busy = false;

    async function load() {
      if (!props.active || document.hidden || busy) return;
      busy = true;
      try {
        rec.value = await call("GET", "/ui/usage/agent?tz=" + encodeURIComponent(TZ));
        error.value = "";
      } catch (e) {
        if (e.status === 401) emit("unauthorized");
        else error.value = e.message;
      } finally { busy = false; }
    }
    watch(() => props.active, (on) => { if (on) load(); });
    const onVisible = () => { if (!document.hidden) load(); };
    onMounted(() => {
      load();
      timer = setInterval(load, REFRESH_MS);
      document.addEventListener("visibilitychange", onVisible);
    });
    onUnmounted(() => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    });

    const days = computed(() => {
      const list = rec.value ? rec.value.recentDays : [];
      const top = Math.max(1, ...list.map((d) => d.messageCount));
      return list.map((d) => ({ ...d, label: day(d.date), share: d.messageCount / top }));
    });
    const models = computed(() => Object.entries(rec.value ? rec.value.modelUsage : {})
      .map(([id, u]) => ({ id, ...u, total: u.inputTokens + u.outputTokens + u.cacheReadInputTokens }))
      .sort((a, b) => b.total - a.total));
    const resets = (iso) => {
      const d = iso ? new Date(iso) : null;
      return d && !isNaN(d.getTime()) ? ` · réinitialisation le ${d.toLocaleDateString("fr-FR")}` : "";
    };
    return { rec, error, days, models, resets, percent, num };
  },
};

export default UsageView;
</script>

<template>
    <div class="config-keys usage">
      <p class="err" v-if="error" role="status">{{ error }}</p>
      <section class="panel" v-if="rec && rec.limits.length">
        <div class="panel-head"><h2>Limites</h2></div>
        <div class="quota-rows"><div class="quota-row">
          <div class="quota-line" v-for="(l, i) in rec.limits" :key="i">
            <span class="quota-label">{{ l.label }}</span>
            <span class="quota-bar" role="meter" :aria-label="l.label"
                  :aria-valuenow="Math.round(l.percent * 100)" aria-valuemin="0" aria-valuemax="100">
              <i :style="{ width: (l.percent * 100) + '%' }" :class="{ high: l.percent >= 0.8 }"></i></span>
            <span class="quota-text long">{{ percent(l.percent) }}{{ resets(l.resetsAt) }}</span>
          </div>
        </div></div>
      </section>
      <section class="panel" v-if="rec">
        <div class="panel-head"><h2>7 derniers jours</h2>
          <span class="dim">aujourd'hui : {{ num(rec.todayTotalTokens) }} tokens · {{ num(rec.todayPrompts) }} requêtes</span></div>
        <div class="quota-rows"><div class="quota-row">
          <div class="quota-line" v-for="d in days" :key="d.date">
            <span class="quota-label">{{ d.label }}</span>
            <span class="quota-bar"><i :style="{ width: (d.share * 100) + '%' }"></i></span>
            <span class="quota-text">{{ num(d.messageCount) }} tokens</span>
          </div>
        </div></div>
      </section>
      <section class="panel" v-if="models.length">
        <div class="panel-head"><h2>Par modèle</h2>
          <span class="dim">{{ rec.activeDays }} jour(s) d'activité</span></div>
        <div class="scroll"><table>
          <thead><tr><th>Modèle</th><th>Entrée</th><th>Cache</th><th>Sortie</th></tr></thead>
          <tbody><tr v-for="m in models" :key="m.id">
            <td><code>{{ m.id }}</code></td><td>{{ num(m.inputTokens) }}</td>
            <td>{{ num(m.cacheReadInputTokens) }}</td><td>{{ num(m.outputTokens) }}</td></tr></tbody>
        </table></div>
      </section>
    </div>
</template>
