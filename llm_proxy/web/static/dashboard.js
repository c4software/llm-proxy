/*
  L'application du tableau de bord. Elle ne contient que de l'état et des
  valeurs dérivées : le rendu est déclaratif, dans le gabarit de la page
  (templates/index.html). Rien ici ne touche au DOM.

  Elle ne consomme que l'Usage API du proxy (/ui/usage =
  /v1/organization/usage/completions servi sous /ui, pour que le cookie de
  la page suffise) — exactement ce que verrait un client tiers.

  UN SEUL appel par rafraîchissement : les seaux de la période, groupés
  par modèle. Tout le reste — totaux, ligne par modèle, courbe — s'en
  déduit, parce que chaque grandeur exposée s'additionne (requêtes,
  tokens, erreurs, somme des latences) ou se maximise (latence max). La
  borne de départ est ALIGNÉE sur la largeur des seaux : les chiffres du
  tableau portent alors exactement sur ce que montre la courbe.

  Un second appel, léger, sert uniquement à savoir jusqu'où remonte
  l'historique — au chargement et au changement de période, jamais dans
  la boucle. La vue « Tout » en a besoin pour choisir son découpage.
  Un troisième, UNE fois au chargement, lit /healthz pour le panneau
  « Brancher un client » (auth, surface Anthropic, modèles connus) — ce
  n'est pas de l'usage, ça ne bouge pas, ça ne se rafraîchit pas.

  Rien n'est demandé tant que l'onglet est masqué.

  Le rafraîchissement ne clignote pas : Vue rapproche les listes par leur
  clé et ne réécrit que ce qui a changé. Les nœuds survivants gardent donc
  leurs transitions, le survol et la sélection de texte.
*/
const { createApp, ref, computed, onMounted, onUnmounted } = Vue;

createApp({
  setup() {
    const API = "/ui/usage";
    const REFRESH = 5000;
    const STORE = "llm-proxy-window";
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

    // ── état ────────────────────────────────────────────────────────────
    const current = ref(byId[localStorage.getItem(STORE)] ? localStorage
      .getItem(STORE) : "all");
    const buckets = ref([]);      // seaux de la période, groupés par modèle
    const bucketWidth = ref("1d");
    const since = ref(0);         // plus ancien enregistrement connu
    const loaded = ref(false);
    // Horloge : fait vieillir les « il y a 3 min » sans aucune requête.
    const now = ref(Date.now() / 1000);
    // Armé au seul changement de période : c'est lui qui déclenche la
    // cascade des barres (voir .bars.entering dans la feuille de style).
    const entering = ref(false);
    // /healthz, lu une fois : surface Anthropic, auth, modèles connus.
    // null tant qu'il n'a pas répondu.
    const health = ref(null);
    const anthropic = computed(() => health.value
      ? !!(health.value.anthropic || {}).enabled : null);

    // ── formatage ───────────────────────────────────────────────────────
    // Entier à la française : espace comme séparateur de milliers.
    const num = (v) => String(Math.round(v) || 0)
      .replace(/\B(?=(\d{3})+(?!\d))/g, " ");
    const ms = (s) => (s >= 1 ? s.toFixed(1) + " s" : Math.round(s * 1000) + " ms");
    const ago = (ts) => {
      const s = Math.max(0, Math.round(now.value - ts));
      if (s < 5) return "à l'instant";
      if (s < 60) return "il y a " + s + " s";
      if (s < 3600) return "il y a " + Math.floor(s / 60) + " min";
      if (s < 86400) return "il y a " + Math.floor(s / 3600) + " h";
      return "il y a " + Math.floor(s / 86400) + " j";
    };
    const dur = (s) => {
      s = Math.max(0, Math.round(s));
      if (s < 60) return s + " s";
      if (s < 3600) return Math.floor(s / 60) + " min";
      if (s < 86400) return Math.floor(s / 3600) + " h " +
        String(Math.floor((s % 3600) / 60)).padStart(2, "0");
      return Math.floor(s / 86400) + " j";
    };
    const moment = (ts) => new Date(ts * 1000).toLocaleString("fr-FR", {
      day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
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
              anthropic: 0, prompt: 0, completion: 0, latencySum: 0,
              maxLatency: 0, last: 0,
            });
          }
          m.requests += r.num_model_requests;
          m.errors += r.num_errors;
          m.streamed += r.num_streamed_requests;
          m.estimated += r.num_estimated_requests;
          m.anthropic += r.num_anthropic_requests || 0;
          m.prompt += r.input_tokens;
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
      completion: t.completion + m.completion,
      tokens: t.tokens + m.tokens,
    }), { requests: 0, errors: 0, streamed: 0, anthropic: 0, prompt: 0,
          completion: 0, tokens: 0 }));

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

    const count = (b) => b.results.reduce((s, r) => s + r.num_model_requests, 0);
    const peak = computed(() => buckets.value.reduce(
      (m, b) => Math.max(m, count(b)), 0));

    // Chaque barre est EMPILÉE par modèle : un segment par modèle ayant
    // servi dans le seau, dans l'ordre (et la couleur) de la liste des
    // modèles — le bas de la pile est toujours le même modèle d'un seau
    // à l'autre, l'œil suit une couche sans la chercher.
    const bars = computed(() => {
      const palette = new Map(models.value.map((m) => [m.id, m.color]));
      return buckets.value.map((b) => {
        const requests = count(b);
        const byModel = new Map(b.results.map((r) => [r.model, r]));
        const segments = models.value
          .filter((m) => byModel.has(m.id))
          .map((m) => {
            const r = byModel.get(m.id);
            return {
              id: m.id, color: palette.get(m.id),
              requests: r.num_model_requests,
              tokens: r.input_tokens + r.output_tokens,
              // Part du seau : les segments se partagent la hauteur
              // de la barre, qui porte seule l'échelle.
              share: requests ? (100 * r.num_model_requests) / requests : 0,
            };
          });
        return {
          start_time: b.start_time,
          requests,
          tokens: segments.reduce((s, x) => s + x.tokens, 0),
          segments,
          // 2 % de socle : un seau non vide mais minuscule doit rester
          // visible, et un seau vide doit rester vide.
          height: requests && peak.value
            ? Math.max((100 * requests) / peak.value, 2) : 0,
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

    // ── «Brancher un client» : des commandes prêtes à coller ──────────
    // Tout est dérivé : l'URL de CETTE page, l'auth et les modèles vus
    // par /healthz, le modèle le plus actif de la période en exemple.
    const origin = window.location.origin;
    const authRequired = computed(() => !!(health.value || {}).auth_required);
    const apiKey = computed(() => authRequired.value ? "<clé du proxy>" : "unused");
    const exampleModel = computed(() => {
      const top = [...models.value].sort((a, b) => b.requests - a.requests)[0];
      if (top) return top.id;
      const seen = Object.values((health.value || {}).backends || {})
        .flatMap((b) => b.last_seen_models || []);
      return seen[0] || "albert/deepseek-v4-flash";
    });
    // Un modèle d'un AUTRE backend que l'exemple, s'il y en a un : c'est
    // l'usage typique du petit modèle rapide (local, sans quota).
    const smallModel = computed(() => {
      const main = exampleModel.value.split("/")[0];
      const seen = Object.entries((health.value || {}).backends || {})
        .filter(([name]) => name !== main)
        .flatMap(([, b]) => b.last_seen_models || []);
      return seen[0] || "bigchuck/qwen3-8b";
    });
    const snippets = computed(() => {
      const key = apiKey.value, model = exampleModel.value;
      const smallModel_ = smallModel.value;
      const auth = authRequired.value;
      return {
        curl: `curl -s ${origin}/v1/chat/completions \\
  -H "Content-Type: application/json"${auth ? ' \\\n  -H "Authorization: Bearer ' + key + '"' : ""} \\
  -d '{"model":"${model}","messages":[{"role":"user","content":"Bonjour"}]}'`,
        python: `from openai import OpenAI
client = OpenAI(base_url="${origin}/v1", api_key="${key}")
r = client.chat.completions.create(
    model="${model}",
    messages=[{"role": "user", "content": "Bonjour"}],
)
print(r.choices[0].message.content)`,
        models: `curl -s ${origin}/v1/models${auth ? ' -H "Authorization: Bearer ' + key + '"' : ""} | jq '.data[].id'`,
        claude: `export ANTHROPIC_BASE_URL=${origin}
export ANTHROPIC_API_KEY=${key}
export ANTHROPIC_MODEL=${model}
# tâches d'arrière-plan (titres, résumés…) sur un backend local, au choix :
# export ANTHROPIC_SMALL_FAST_MODEL=${smallModel_}
claude`,
      };
    });
    const shortcuts = computed(() => windows.map((w) => w.key).join(" / "));
    const bucketLabel = computed(() => bucketWidth.value === "1h"
      ? "1 heure" : "1 jour");

    // ── lecture de l'Usage API ──────────────────────────────────────────
    const ask = (params) => {
      const q = Object.entries(params)
        .filter(([, v]) => v !== null && v !== undefined)
        .map(([k, v]) => k + "=" + encodeURIComponent(v)).join("&");
      return fetch(API + "?" + q, { headers: { accept: "application/json" } })
        .then((r) => (r.ok ? r.json() : Promise.reject(r.status)));
    };

    // Jusqu'où remonte l'historique. Un seul seau, sans regroupement :
    // sa borne basse est la plus ancienne ligne de la plage.
    async function refreshSince() {
      const span = byId[current.value].span;
      const page = await ask({
        start_time: span ? Math.floor(Date.now() / 1000) - span : 0,
        bucket_width: "all", limit: 1,
      });
      const bucket = (page.data || [])[0];
      since.value = bucket && bucket.results.length ? bucket.start_time : 0;
    }

    async function poll() {
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
        const page = await ask({
          start_time: start, bucket_width: width, "group_by[]": "model",
          limit: Math.min(Math.ceil((stamp - start) / seconds) + 1, 180),
        });
        if (win !== current.value) return;   // période changée entre-temps
        bucketWidth.value = width;
        buckets.value = page.data || [];
        loaded.value = true;
        // Base vide au départ, puis du trafic arrive : on rattrape.
        if (!since.value && totals.value.requests) refreshSince();
      } catch (e) {
        /* proxy injoignable : on retentera dans 5 s */
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

    // Raccourcis A / W / D. Ignorés dès qu'un modificateur est enfoncé ou
    // qu'un champ a le focus : un raccourci ne doit jamais voler une frappe.
    function onKey(e) {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      if (/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName)) return;
      const hit = windows.find((w) => w.key === String(e.key || "").toUpperCase());
      if (hit) { e.preventDefault(); select(hit.id); }
    }

    // Onglet masqué : plus aucune requête. Personne ne regarde, et au
    // retour on rafraîchit immédiatement plutôt que d'afficher des
    // chiffres vieux de plusieurs minutes.
    function onVisibility() {
      if (!document.hidden) poll();
    }

    // /healthz est toujours accessible sans clé : le badge ne dépend
    // pas de l'auth de la page.
    function loadHealth() {
      fetch("/healthz", { headers: { accept: "application/json" } })
        .then((r) => (r.ok ? r.json() : null))
        .then((h) => { health.value = h; })
        .catch(() => { health.value = null; });
    }

    let timers = [];
    onMounted(() => {
      poll();
      loadHealth();
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

    return { windows, current, models, totals, bars, axis, peak, since, now,
             loaded, entering, anthropic, authRequired, exampleModel,
             snippets, origin, note, shortcuts, bucketLabel, successRate,
             backendSummary, num, ms, ago, dur, moment, tickLabel, select };
  },
}).mount("#app");
