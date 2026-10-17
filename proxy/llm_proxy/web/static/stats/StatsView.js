/*
  Les STATISTIQUES : l'usage de la période, lu sur l'Usage API du proxy
  (/ui/usage = /v1/organization/usage/completions servi sous /ui, pour
  que le cookie de la page suffise) — exactement ce que verrait un client
  tiers.

  UN SEUL appel par rafraîchissement : les seaux de la période, groupés
  par modèle. Tout le reste — totaux, ligne par modèle, courbe — s'en
  déduit, et la courbe se lit indifféremment en requêtes ou en tokens :
  la même réponse porte les deux grandeurs, changer de mesure ne
  redemande rien, parce que chaque grandeur exposée s'additionne (requêtes,
  tokens, erreurs, somme des latences) ou se maximise (latence max). La
  borne de départ est ALIGNÉE sur la largeur des seaux : les chiffres du
  tableau portent alors exactement sur ce que montre la courbe.

  Un second appel, léger, sert uniquement à savoir jusqu'où remonte
  l'historique — au chargement et au changement de période, jamais dans
  la boucle. La vue « Tout » en a besoin pour choisir son découpage.

  La section « Outils » a son propre appel, à la route sœur
  (/ui/usage/tools = /v1/organization/usage/tools) : UN seau sur la même
  période, groupé par outil et par route d'appel. Elle disparaît quand
  aucun outil hébergé n'a servi dans la fenêtre. Le cache web et les
  serveurs MCP viennent de `health` (/ui/health, lu par app.js).

  Rien n'est demandé tant que la vue n'est pas affichée (`active`) ou que
  l'onglet est masqué. Le rafraîchissement ne clignote pas : Vue rapproche
  les listes par leur clé et ne réécrit que ce qui a changé.
*/
import { ask } from "../lib/http.js";
import { num, ms, dur, ago, moment } from "../lib/format.js";

const { ref, computed, watch, onMounted, onUnmounted } = Vue;

export const StatsView = {
  props: {
    session: { type: Object, required: true },
    health: { type: Object, default: null },
    active: { type: Boolean, default: false },   // la vue affichée
  },
  emits: ["unauthorized"],
  setup(props, { emit }) {
    const API = "/ui/usage";
    const API_TOOLS = "/ui/usage/tools";
    const REFRESH = 5000;
    const STORE = "llm-proxy-window";
    const STORE_METRIC = "llm-proxy-metric";
    // Tons chauds lisibles sur les deux thèmes, cyclés au-delà de six modèles.
    const COLORS = ["#d97757", "#e0a458", "#7d9b76", "#6b8fa3", "#a37ba0",
                    "#c08552"];
    // Les trois périodes, dans l'ordre du sélecteur. `span` en secondes,
    // null = depuis le premier enregistrement connu.
    const windows = [
      { id: "day", label: "Jour", span: 86400, key: "D",
        note: "24 dernières heures" },
      { id: "week", label: "Semaine", span: 604800, key: "W",
        note: "7 derniers jours" },
      { id: "all", label: "Tout", span: null, key: "A",
        note: "tout l'historique conservé" },
    ];
    const byId = Object.fromEntries(windows.map((w) => [w.id, w]));
    // Les deux lectures du même trafic. `pick` extrait la grandeur d'une
    // ligne de résultat : c'est le SEUL endroit qui sait ce que « mesure »
    // veut dire — barres, pic, infobulle et axe en découlent.
    const metrics = [
      { id: "requests", label: "Requêtes", key: "R", unit: "req",
        pick: (r) => r.num_model_requests },
      { id: "tokens", label: "Tokens", key: "T", unit: "tokens",
        pick: (r) => r.input_tokens + r.output_tokens },
    ];
    const metricById = Object.fromEntries(metrics.map((m) => [m.id, m]));

    // ── état ────────────────────────────────────────────────────────────
    const current = ref(byId[localStorage.getItem(STORE)] ? localStorage
      .getItem(STORE) : "all");
    // Mesure portée par la courbe : requêtes servies ou tokens échangés.
    // Les deux viennent de la même réponse — basculer ne relance rien.
    const metric = ref(metricById[localStorage.getItem(STORE_METRIC)]
      ? localStorage.getItem(STORE_METRIC) : "requests");
    const buckets = ref([]);      // seaux de la période, groupés par modèle
    const bucketWidth = ref("1d");
    const since = ref(0);         // plus ancien enregistrement connu
    // Exécutions d'outils hébergés de la période : les lignes de résultat
    // d'un seau unique, une par couple (outil, route d'appel).
    const toolResults = ref([]);
    const loaded = ref(false);
    // Horloge : fait vieillir les « il y a 3 min » sans aucune requête.
    const now = ref(Date.now() / 1000);
    // Armé au seul changement de période : c'est lui qui déclenche la
    // cascade des barres (voir .bars.entering dans la feuille de style).
    const entering = ref(false);
    const health = computed(() => props.health);
    // Serveurs MCP de la configuration (/healthz) : joints ou non, et
    // combien d'outils chacun apporte.
    const mcpServers = computed(() =>
      (((health.value || {}).tools || {}).mcp || []).filter((s) => s.enabled));
    // Cache web des outils (tools/webcache.py), lu dans /healthz : des
    // compteurs en mémoire vive, DEPUIS LE DÉMARRAGE du proxy — pas sur la
    // période choisie, au contraire du tableau des exécutions.
    const webCache = computed(() => {
      const c = ((health.value || {}).tools || {}).web_cache;
      if (!c) return null;
      const asked = (c.hits || 0) + (c.misses || 0);
      const mo = (c.bytes || 0) / 1e6;
      return {
        on: c.ttl > 0, asked, hits: c.hits || 0, entries: c.entries || 0,
        share: asked ? Math.round((100 * (c.hits || 0)) / asked) : 0,
        size: mo >= 1 ? mo.toFixed(1).replace(".", ",") + " Mo"
          : Math.round((c.bytes || 0) / 1e3) + " Ko",
        ttl: c.ttl >= 120 ? Math.round(c.ttl / 60) + " min"
          : Math.round(c.ttl) + " s",
      };
    });

    // ── formatage ─────────────────────────────────────────────────────────
    const tickLabel = (b) => bucketWidth.value === "1h"
      ? String(new Date(b.start_time * 1000).getHours()).padStart(2, "0") + " h"
      : new Date(b.start_time * 1000).toLocaleDateString("fr-FR",
          { day: "numeric", month: "short" });

    // ── valeurs dérivées ────────────────────────────────────────────────
    // Un modèle par clé, recomposé à partir de tous les seaux. Toutes ces
    // grandeurs s'agrègent SANS PERTE : le total est celui qu'aurait rendu
    // un seau unique couvrant la même plage.
    const models = computed(() => {
      const acc = new Map();
      for (const b of buckets.value) {
        for (const r of b.results) {
          let m = acc.get(r.model);
          if (!m) {
            acc.set(r.model, m = {
              id: r.model, requests: 0, errors: 0, streamed: 0, estimated: 0,
              anthropic: 0, prompt: 0, cached: 0, completion: 0,
              latencySum: 0, maxLatency: 0, last: 0,
            });
          }
          m.requests += r.num_model_requests;
          m.errors += r.num_errors;
          m.streamed += r.num_streamed_requests;
          m.estimated += r.num_estimated_requests;
          m.anthropic += r.num_anthropic_requests || 0;
          m.prompt += r.input_tokens;
          m.cached += r.input_cached_tokens || 0;
          m.completion += r.output_tokens;
          m.latencySum += r.total_latency_seconds;
          m.maxLatency = Math.max(m.maxLatency, r.max_latency_seconds);
          m.last = Math.max(m.last, r.last_request_time);
        }
      }
      // Ordre ALPHABÉTIQUE : le seul qui ne dépende ni du trafic ni de la
      // période. Trier par ordre d'apparition rangeait les modèles
      // différemment dans chaque vue — les lignes se réordonnaient en
      // changeant de période, les segments de la barre se croisaient au
      // lieu de glisser, et un même modèle changeait de couleur puisque
      // celle-ci suit le rang.
      const list = [...acc.values()].sort((a, b) => a.id.localeCompare(b.id));
      const grand = list.reduce((s, m) => s + m.prompt + m.completion, 0);
      return list.map((m, i) => {
        const tokens = m.prompt + m.completion;
        const exact = m.requests - m.estimated;
        return {
          ...m,
          // Le backend n'est pas un champ de l'API : c'est le préfixe du
          // modèle, qui EST le discriminant de routage du proxy.
          backend: String(m.id || "").split("/")[0] || "—",
          color: COLORS[i % COLORS.length],
          tokens,
          avg: m.requests ? tokens / m.requests : 0,
          avgLatency: m.requests ? m.latencySum / m.requests : 0,
          exact,
          exactPct: m.requests ? Math.round((100 * exact) / m.requests) : 0,
          // Part de l'entrée servie depuis le cache de préfixe du backend
          // (quand il le dit) : c'est ce qui fait qu'un prompt système de
          // 20 k tokens ne coûte pas 20 k tokens de calcul à chaque tour.
          cachePct: m.prompt ? Math.round((100 * m.cached) / m.prompt) : 0,
          share: grand ? (100 * tokens) / grand : 0,
        };
      });
    });

    const totals = computed(() => models.value.reduce((t, m) => ({
      requests: t.requests + m.requests,
      errors: t.errors + m.errors,
      streamed: t.streamed + m.streamed,
      anthropic: t.anthropic + m.anthropic,
      prompt: t.prompt + m.prompt,
      cached: t.cached + m.cached,
      completion: t.completion + m.completion,
      tokens: t.tokens + m.tokens,
    }), { requests: 0, errors: 0, streamed: 0, anthropic: 0, prompt: 0,
          cached: 0, completion: 0, tokens: 0 }));

    const successRate = computed(() => {
      const t = totals.value;
      return t.requests
        ? ((100 * (t.requests - t.errors)) / t.requests).toFixed(1) + "%" : "—";
    });

    const backendSummary = computed(() => {
      const per = {};
      models.value.forEach((m) => {
        per[m.backend] = (per[m.backend] || 0) + m.requests;
      });
      const names = Object.keys(per).sort();
      return names.length
        ? names.map((n) => n + " : " + num(per[n]) + " req").join(" · ")
        : "aucun trafic";
    });

    // Un outil hébergé par ligne, recomposé depuis ses lignes par route
    // d'appel : tout s'additionne ou se maximise, comme pour les modèles.
    // La durée moyenne porte sur les appels réellement lancés — un refus
    // par limite d'appels n'exécute rien et ne dure rien.
    const tools = computed(() => {
      const acc = new Map();
      for (const r of toolResults.value) {
        let t = acc.get(r.tool);
        if (!t) {
          acc.set(r.tool, t = {
            id: r.tool, calls: 0, errors: 0, limited: 0, durationSum: 0,
            maxDuration: 0, chars: 0, last: 0, routes: [],
          });
        }
        t.calls += r.num_requests;
        t.errors += r.num_errors;
        t.limited += r.num_limited;
        t.durationSum += r.total_duration_seconds;
        t.maxDuration = Math.max(t.maxDuration, r.max_duration_seconds);
        t.chars += r.result_chars;
        t.last = Math.max(t.last, r.last_request_time);
        t.routes.push({ endpoint: r.endpoint, calls: r.num_requests });
      }
      return [...acc.values()]
        .sort((a, b) => a.id.localeCompare(b.id))
        .map((t) => {
          const ran = t.calls - t.limited;
          return {
            ...t,
            avgDuration: ran ? t.durationSum / ran : 0,
            // Répartition par route d'appel, en une ligne de texte.
            routes: t.routes
              .sort((a, b) => a.endpoint.localeCompare(b.endpoint))
              .map((r) => r.endpoint + " : " + num(r.calls)).join(" · "),
          };
        });
    });
    const toolCalls = computed(() =>
      tools.value.reduce((s, t) => s + t.calls, 0));

    // Valeur d'un seau DANS LA MESURE COURANTE : c'est elle qui donne
    // l'échelle de la courbe et le partage de chaque barre.
    const pick = computed(() => metricById[metric.value].pick);
    const count = (b) => b.results.reduce((s, r) => s + pick.value(r), 0);
    const peak = computed(() => buckets.value.reduce(
      (m, b) => Math.max(m, count(b)), 0));
    const metricUnit = computed(() => metricById[metric.value].unit);
    const metricLabel = computed(() =>
      metricById[metric.value].label.toLowerCase());

    // Chaque barre est EMPILÉE par modèle : un segment par modèle ayant
    // servi dans le seau, dans l'ordre (et la couleur) de la liste des
    // modèles — le bas de la pile est toujours le même modèle d'un seau
    // à l'autre, l'œil suit une couche sans la chercher. Hauteur et
    // partage suivent la mesure choisie ; l'infobulle, elle, montre
    // toujours les deux grandeurs.
    const bars = computed(() => {
      const palette = new Map(models.value.map((m) => [m.id, m.color]));
      const take = pick.value;
      return buckets.value.map((b) => {
        const value = count(b);
        const byModel = new Map(b.results.map((r) => [r.model, r]));
        const segments = models.value
          .filter((m) => byModel.has(m.id))
          .map((m) => {
            const r = byModel.get(m.id);
            return {
              id: m.id, color: palette.get(m.id),
              requests: r.num_model_requests,
              tokens: r.input_tokens + r.output_tokens,
              value: take(r),
              // Part du seau : les segments se partagent la hauteur
              // de la barre, qui porte seule l'échelle.
              share: value ? (100 * take(r)) / value : 0,
            };
          });
        return {
          start_time: b.start_time,
          value,
          requests: segments.reduce((s, x) => s + x.requests, 0),
          tokens: segments.reduce((s, x) => s + x.tokens, 0),
          segments,
          // 2 % de socle : un seau non vide mais minuscule doit rester
          // visible, et un seau vide doit rester vide.
          height: value && peak.value
            ? Math.max((100 * value) / peak.value, 2) : 0,
        };
      });
    });

    // Quatre repères suffisent : l'axe situe, il n'énumère pas.
    const axis = computed(() => {
      const list = bars.value;
      if (!list.length) return [];
      const step = Math.max(Math.floor(list.length / 3), 1);
      const picks = [];
      for (let i = 0; i < list.length; i += step) picks.push(list[i]);
      if (picks[picks.length - 1] !== list[list.length - 1]) {
        picks.push(list[list.length - 1]);
      }
      return picks;
    });

    const note = computed(() => byId[current.value].note);

    const shortcuts = computed(() => windows.map((w) => w.key).join(" / ") +
      " · " + metrics.map((m) => m.key).join(" / "));
    const bucketLabel = computed(() => bucketWidth.value === "1h"
      ? "1 heure" : "1 jour");

    // ── lecture de l'Usage API ──────────────────────────────────────────
    // Jusqu'où remonte l'historique. Un seul seau, sans regroupement :
    // sa borne basse est la plus ancienne ligne de la plage.
    async function refreshSince() {
      const span = byId[current.value].span;
      const page = await ask(API, {
        start_time: span ? Math.floor(Date.now() / 1000) - span : 0,
        bucket_width: "all", limit: 1,
      });
      const bucket = (page.data || [])[0];
      since.value = bucket && bucket.results.length ? bucket.start_time : 0;
    }

    async function poll() {
      if (!props.active) return;
      const win = current.value;
      const span = byId[win].span;
      const stamp = Math.floor(Date.now() / 1000);
      try {
        // La vue « Tout » ne sait pas d'avance quelle étendue couvrir.
        if (!span && !since.value) await refreshSince();
        if (win !== current.value) return;
        const range = span || Math.max(stamp - (since.value || stamp), 3600);
        const width = range <= 2 * 86400 ? "1h" : "1d";
        const seconds = width === "1h" ? 3600 : 86400;
        // Borne ALIGNÉE sur la largeur des seaux. Sans cela le premier
        // seau commencerait avant la plage demandée et les totaux
        // porteraient sur un peu plus que ce que la courbe montre.
        const start = Math.floor((stamp - range) / seconds) * seconds;
        // Les deux lectures partent ensemble. Celle des outils couvre la
        // même période en UN seau ; en vue « Tout » elle part de zéro,
        // l'étendue de l'historique étant celle des REQUÊTES — un outil
        // appelé directement (/v1/tools) peut les précéder. Si elle
        // échoue, le reste de la page n'en dépend pas.
        const [page, toolPage] = await Promise.all([
          ask(API, {
            start_time: start, bucket_width: width, "group_by[]": "model",
            limit: Math.min(Math.ceil((stamp - start) / seconds) + 1, 180),
          }),
          ask(API_TOOLS, {
            start_time: span ? start : 0, bucket_width: "all",
            "group_by[]": "tool,endpoint", limit: 1,
          }).catch(() => null),
        ]);
        if (win !== current.value) return;   // période changée entre-temps
        if (toolPage) {
          toolResults.value = ((toolPage.data || [])[0] || {}).results || [];
        }
        bucketWidth.value = width;
        buckets.value = page.data || [];
        loaded.value = true;
        // Base vide au départ, puis du trafic arrive : on rattrape.
        if (!since.value && totals.value.requests) refreshSince();
      } catch (e) {
        // Clé révoquée ou cookie expiré : retour à l'écran d'entrée.
        if (e === 401) emit("unauthorized");
        /* sinon proxy injoignable : on retentera dans 5 s */
      }
    }

    function select(id) {
      if (!byId[id] || id === current.value) return;
      current.value = id;
      localStorage.setItem(STORE, id);
      since.value = 0;   // recalculé pour la nouvelle période
      // Le découpage change : les barres ne représentent plus les mêmes
      // seaux, les faire glisser d'une valeur à l'autre n'aurait pas de
      // sens. Elles remontent de zéro, en cascade.
      entering.value = true;
      poll().then(() => setTimeout(
        () => { entering.value = false; }, 600 + bars.value.length * 14));
    }

    // La mesure ne change ni les seaux ni l'échelle de temps : les mêmes
    // barres glissent vers leur nouvelle hauteur (transition CSS), sans
    // cascade et sans nouvelle requête.
    function selectMetric(id) {
      if (!metricById[id] || id === metric.value) return;
      metric.value = id;
      localStorage.setItem(STORE_METRIC, id);
    }

    // Raccourcis A / W / D, R / T. Ignorés dès qu'un modificateur est enfoncé ou
    // qu'un champ a le focus : un raccourci ne doit jamais voler une frappe.
    function onKey(e) {
      if (e.ctrlKey || e.metaKey || e.altKey || !props.active) return;
      if (/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName)) return;
      const key = String(e.key || "").toUpperCase();
      const hit = windows.find((w) => w.key === key);
      if (hit) { e.preventDefault(); select(hit.id); return; }
      const met = metrics.find((m) => m.key === key);
      if (met) { e.preventDefault(); selectMetric(met.id); }
    }

    // Onglet masqué : plus aucune requête. Au retour, on rafraîchit
    // immédiatement plutôt que d'afficher des chiffres vieux de plusieurs
    // minutes.
    function onVisibility() { if (!document.hidden) poll(); }

    // La vue revient à l'écran : rafraîchie aussitôt.
    watch(() => props.active, (on) => { if (on) poll(); });

    let timers = [];
    onMounted(() => {
      poll();
      timers = [
        setInterval(() => { if (!document.hidden) poll(); }, REFRESH),
        setInterval(() => { now.value = Date.now() / 1000; }, 1000),
      ];
      window.addEventListener("keydown", onKey);
      document.addEventListener("visibilitychange", onVisibility);
    });
    onUnmounted(() => {
      timers.forEach(clearInterval);
      window.removeEventListener("keydown", onKey);
      document.removeEventListener("visibilitychange", onVisibility);
    });

    return { windows, current, metrics, metric, metricUnit, metricLabel,
             models, totals, bars, axis, peak, since, now, tools, toolCalls,
             loaded, entering, webCache, mcpServers,
             note, shortcuts, bucketLabel, successRate,
             backendSummary, num, ms, dur, moment, tickLabel, select, selectMetric,
             ago: (ts) => ago(ts, now.value) };
  },
  template: `
  <div class="stats">
  <div class="toolbar">
    <div class="range" role="group" aria-label="Période">
      <button v-for="w in windows" :key="w.id" type="button"
              :aria-pressed="String(w.id === current)"
              @click="select(w.id)">{{ w.label }}</button>
    </div>
    <!-- La MESURE portée par la courbe. Les deux grandeurs sont déjà dans
         la réponse : basculer ne redemande rien au proxy. -->
    <div class="range" role="group" aria-label="Mesure">
      <button v-for="m in metrics" :key="m.id" type="button"
              :aria-pressed="String(m.id === metric)"
              @click="selectMetric(m.id)">{{ m.label }}</button>
    </div>
    <span class="range-note">{{ note }}<template v-if="session.account && session.account.role === 'admin'">
      · tous les comptes</template><template v-else-if="session.auth_required">
      · vos requêtes seulement</template> · raccourcis {{ shortcuts }}</span>
  </div>

  <div class="cards">
    <div class="card">
      <div class="label">Requêtes</div>
      <div class="value">{{ num(totals.requests) }}</div>
      <div class="hint">
        <template v-if="totals.requests">{{ successRate }} en succès · </template>
        {{ num(totals.streamed) }} en flux<template v-if="totals.anthropic">
        · {{ num(totals.anthropic) }} via Claude Code</template>
      </div>
    </div>
    <div class="card">
      <div class="label">Tokens</div>
      <div class="value">{{ num(totals.tokens) }}</div>
      <div class="hint">{{ num(totals.prompt) }} entrée ·
        {{ num(totals.completion) }} sortie<template v-if="totals.cached">
        · {{ Math.round(100 * totals.cached / totals.prompt) || "<1" }}% de
        l'entrée en cache</template></div>
    </div>
    <div class="card">
      <div class="label">Modèles actifs</div>
      <div class="value">{{ num(models.length) }}</div>
      <div class="hint">{{ backendSummary }}</div>
    </div>
    <div class="card">
      <div class="label">Erreurs</div>
      <div class="value">{{ num(totals.errors) }}</div>
      <div class="hint">
        <template v-if="since">mesures depuis {{ dur(now - since) }}</template>
        <template v-else>aucune mesure enregistrée</template>
      </div>
    </div>
  </div>

  <!-- UNE mesure à la fois — requêtes OU tokens, au choix du sélecteur :
       un seul axe. Chaque barre est empilée par modèle, aux couleurs de la
       répartition ci-dessous. L'autre grandeur vit dans l'infobulle,
       jamais sur un second axe. -->
  <div class="panel chart-panel" v-if="bars.length">
    <div class="panel-head">
      <h2>Trafic</h2>
      <span class="meta">{{ bars.length }} × {{ bucketLabel }} ·
        en {{ metricLabel }} · pic {{ num(peak) }} {{ metricUnit }} ·
        empilé par modèle</span>
    </div>
    <div class="chart">
      <div class="bars" :class="{ entering }">
        <i v-for="(b, i) in bars" :key="b.start_time"
           :class="{ zero: !b.value, left: i < bars.length / 2, right: i >= bars.length / 2 }"
           :style="{ '--i': i }">
          <b :style="{ height: b.height.toFixed(2) + '%' }">
            <s v-for="seg in b.segments" :key="seg.id"
               :style="{ height: seg.share.toFixed(2) + '%', background: seg.color }"></s>
          </b>
          <span class="tip">
            <span class="when">{{ moment(b.start_time) }}</span>
            <span class="row" v-for="seg in b.segments" :key="seg.id">
              <span><em :style="{ background: seg.color }"></em>{{ seg.id }}</span>
              <b>{{ num(seg.value) }}</b>
            </span>
            <!-- Le total de la mesure affichée d'abord, l'autre juste
                 en dessous : l'infobulle reste la vue complète du seau. -->
            <template v-if="metric === 'tokens'">
              <span class="row total"><span>Tokens</span><b>{{ num(b.tokens) }}</b></span>
              <span class="row"><span>Requêtes</span><b>{{ num(b.requests) }}</b></span>
            </template>
            <template v-else>
              <span class="row total"><span>Requêtes</span><b>{{ num(b.requests) }}</b></span>
              <span class="row"><span>Tokens</span><b>{{ num(b.tokens) }}</b></span>
            </template>
          </span>
        </i>
      </div>
      <div class="axis">
        <span v-for="b in axis" :key="b.start_time">{{ tickLabel(b) }}</span>
      </div>
    </div>
  </div>

  <div class="panel share" v-if="totals.tokens">
    <div class="label">Répartition des tokens</div>
    <div class="share-bar">
      <i v-for="m in models" :key="m.id"
         :style="{ width: m.share.toFixed(2) + '%', background: m.color }"
         :title="m.id + ' — ' + num(m.tokens) + ' tokens'"></i>
    </div>
  </div>

  <div class="panel">
    <div class="panel-head">
      <h2>Par modèle</h2>
      <span class="meta live"><i></i>par ordre alphabétique</span>
    </div>
    <div class="scroll">
      <table>
        <thead><tr>
          <th>Modèle</th><th>Requêtes</th><th>Erreurs</th><th>Entrée</th>
          <th>Sortie</th><th>Total</th><th>Part</th><th>Moy./req</th>
          <th>Cache</th>
          <th>Lat. moy.</th><th>Lat. max</th>
          <th>Comptage</th><th>Dernière</th>
        </tr></thead>
        <tbody>
          <tr v-for="m in models" :key="m.id">
            <td>
              <div class="model"><b :style="{ background: m.color }"></b><span>
                <span class="id">{{ m.id }}</span><br>
                <span class="backend">{{ num(m.streamed) }} en flux<template v-if="m.anthropic">
                  · {{ num(m.anthropic) }} Claude Code</template></span>
              </span></div>
            </td>
            <td>{{ num(m.requests) }}</td>
            <td>
              <span v-if="m.errors" class="err">{{ num(m.errors) }}</span>
              <span v-else class="dim">0</span>
            </td>
            <td>{{ num(m.prompt) }}</td>
            <td>{{ num(m.completion) }}</td>
            <td><strong>{{ num(m.tokens) }}</strong></td>
            <!-- Sa part des tokens de la période : la barre « Répartition
                 des tokens » au-dessus, chiffrée (sans légende à part). -->
            <td>{{ m.share > 0 && m.share < 1 ? "<1" : Math.round(m.share) }}%</td>
            <td>{{ num(m.avg) }}</td>
            <!-- Cache de préfixe du backend : part de l'entrée qu'il n'a
                 pas eu à recalculer. «—» quand il ne le dit pas. -->
            <td :title="m.cached ? num(m.cached) + ' tokens d\\'entrée lus en cache' : 'le backend ne remonte pas de cached_tokens'">
              <span v-if="m.cached">{{ m.cachePct }}%</span>
              <span v-else class="dim">—</span>
            </td>
            <td>{{ ms(m.avgLatency) }}</td>
            <td class="dim">{{ ms(m.maxLatency) }}</td>
            <!-- Provenance des tokens : \`usage\` de l'upstream, ou
                 estimation. Le détail chiffré passe en infobulle — la
                 colonne doit tenir sans pousser le tableau au défilement. -->
            <td>
              <span v-if="!m.estimated" class="tag exact">exact</span>
              <span v-else-if="!m.exact" class="tag est">estimé</span>
              <span v-else class="tag est"
                    :title="num(m.exact) + ' exactes · ' + num(m.estimated) + ' estimées'">
                {{ m.exactPct }}% exact</span>
            </td>
            <td class="dim">{{ ago(m.last) }}</td>
          </tr>
          <tr v-if="!models.length && loaded">
            <td colspan="13"><div class="empty">
              <div class="big">Aucune génération sur cette période</div>
              <div v-if="since">Les mesures remontent à {{ dur(now - since) }} ;
                essayez une période plus large.</div>
              <div v-else>Les modèles apparaîtront ici dès leur première
                requête.</div>
            </div></td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- Les outils que le proxy exécute lui-même (recherche web, lecture
       de page), sur la même période. Absente tant
       qu'aucun n'a servi dans la fenêtre. Des mesures seulement : ce qui
       a été cherché ou lu n'est pas dans les statistiques. -->
  <div class="panel tools" v-if="tools.length || (webCache && webCache.asked) || mcpServers.length">
    <div class="panel-head">
      <h2>Outils</h2>
      <span class="meta">{{ num(toolCalls) }} exécution{{ toolCalls > 1 ? "s" : "" }}
        d'outils hébergés · {{ note }}</span>
    </div>
    <div class="scroll" v-if="tools.length">
      <table>
        <thead><tr>
          <th>Outil</th><th>Appels</th><th>Erreurs</th><th>Refus</th>
          <th>Durée moy.</th><th>Durée max</th><th>Rendu</th>
          <th>Dernier</th>
        </tr></thead>
        <tbody>
          <tr v-for="t in tools" :key="t.id">
            <td>
              <div class="model"><span>
                <span class="id">{{ t.id }}</span><br>
                <!-- Par où l'outil a été appelé : les boucles du proxy
                     (/v1/responses, /v1/messages) ou l'appel direct. -->
                <span class="backend">{{ t.routes }}</span>
              </span></div>
            </td>
            <td>{{ num(t.calls) }}</td>
            <td title="le résultat rendu au modèle était un texte « Error: … »">
              <span v-if="t.errors" class="err">{{ num(t.errors) }}</span>
              <span v-else class="dim">0</span>
            </td>
            <!-- Refus par limite d'appels : rien n'a été exécuté, ce
                 n'est pas une panne de l'outil. -->
            <td title="appels refusés par la limite d'appels par réponse : rien n'a été exécuté">
              <span v-if="t.limited">{{ num(t.limited) }}</span>
              <span v-else class="dim">0</span>
            </td>
            <td>{{ ms(t.avgDuration) }}</td>
            <td class="dim">{{ ms(t.maxDuration) }}</td>
            <td title="caractères rendus au modèle, tous résultats confondus">
              {{ num(t.chars) }} car.</td>
            <td class="dim">{{ ago(t.last) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <!-- Cache web : ce que les outils n'ont pas eu à redemander au web.
         Compteurs en mémoire vive, depuis le démarrage du proxy — pas sur
         la période du tableau. -->
    <p class="note webcache" v-if="webCache">
      <template v-if="webCache.on">
        <b>Cache web</b> — {{ num(webCache.hits) }} demande{{ webCache.hits > 1 ? "s" : "" }}
        sur {{ num(webCache.asked) }} servie{{ webCache.hits > 1 ? "s" : "" }}
        sans retourner sur le web ({{ webCache.share }} %) depuis le
        démarrage du proxy · {{ num(webCache.entries) }} page{{ webCache.entries > 1 ? "s" : "" }}
        ou recherche{{ webCache.entries > 1 ? "s" : "" }} en cache
        ({{ webCache.size }}), gardée{{ webCache.entries > 1 ? "s" : "" }} {{ webCache.ttl }}
      </template>
      <template v-else><b>Cache web</b> désactivé
        (<code>[tools] web_cache_ttl = 0</code>)</template>
    </p>
    <!-- Serveurs MCP : l'état de la dernière découverte, à l'instant. -->
    <p class="note webcache" v-if="mcpServers.length">
      <b>Serveurs MCP</b> —
      <template v-for="(s, i) in mcpServers" :key="s.name">
        <template v-if="i"> · </template>
        <span :title="s.url + (s.error ? ' — ' + s.error : '')">{{ s.name }} :
          <template v-if="s.up">{{ s.tools.length }} outil{{ s.tools.length > 1 ? "s" : "" }}</template>
          <template v-else-if="s.up === false">injoignable</template>
          <template v-else>pas encore joint</template></span>
      </template>
    </p>
  </div>

  <footer>Rafraîchissement toutes les 5 s.</footer>
  </div>
  `,
};
