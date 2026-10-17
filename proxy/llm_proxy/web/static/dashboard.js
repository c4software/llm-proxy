/*
  L'application du tableau de bord. Elle ne contient que de l'état et des
  valeurs dérivées : le rendu est déclaratif, dans le gabarit de la page
  (templates/index.html). Rien ici ne touche au DOM.

  Elle ne consomme que l'Usage API du proxy (/ui/usage =
  /v1/organization/usage/completions servi sous /ui, pour que le cookie de
  la page suffise) — exactement ce que verrait un client tiers.

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
  Un troisième, au chargement puis toutes les 30 s, lit /healthz : le
  cache web et les serveurs MCP de la section Outils, et l'état du
  déploiement que l'onglet Configuration reprend — ce n'est pas de
  l'usage, ça ne suit pas la période.

  La section « Outils » a son propre appel, à la route sœur
  (/ui/usage/tools = /v1/organization/usage/tools) : UN seau sur la même
  période, groupé par outil et par route d'appel. Elle ne montre que des
  totaux, il n'y a donc rien à découper ; et elle disparaît quand aucun
  outil hébergé n'a servi dans la fenêtre.

  Rien n'est demandé tant que l'onglet est masqué.

  SESSION ET VUES. La page lit d'abord /ui/session : auth active sans clé
  valide (401), elle montre l'écran d'entrée — la clé saisie part à
  /ui/login, qui ouvre une session signée (cookie HttpOnly) ; avec les
  comptes OIDC (/ui/auth dit «oidc»), un bouton mène au fournisseur
  (/ui/auth/login), et un compte en attente ou bloqué ne voit que son
  état. Une fois entrée,
  la personne ne voit que SES requêtes (le proxy filtre /ui/usage sur sa
  clé). Trois vues, menu latéral (tiroir sur mobile) : « Statistiques » (tout ce qui
  suit), « Chat » (chat.js, <chat-panel>) et « Configuration »
  (config.js, <config-panel> : générateurs de configuration, clients et
  proxy), mémorisées dans l'URL (#chat, #config) et le navigateur. Les statistiques ne se rafraîchissent pas tant que le
  chat est affiché ; le chat, lui, reste monté — une réponse en cours
  continue pendant qu'on regarde les chiffres.

  Le rafraîchissement ne clignote pas : Vue rapproche les listes par leur
  clé et ne réécrit que ce qui a changé. Les nœuds survivants gardent donc
  leurs transitions, le survol et la sélection de texte.
*/
const { createApp, ref, computed, onMounted, onUnmounted } = Vue;

createApp({
  setup() {
    const API = "/ui/usage";
    const API_TOOLS = "/ui/usage/tools";
    const REFRESH = 5000;
    const STORE = "llm-proxy-window";
    const STORE_METRIC = "llm-proxy-metric";
    const STORE_VIEW = "llm-proxy-view";
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

    // ── session et vues ─────────────────────────────────────────────────
    const views = [{ id: "stats", label: "Statistiques" }, { id: "chat", label: "Chat" },
                   { id: "config", label: "Configuration" }];
    const known = (id) => views.some((v) => v.id === id);
    const storedView = () => {
      if (known(location.hash.slice(1))) return location.hash.slice(1);
      try { const v = localStorage.getItem(STORE_VIEW); return known(v) ? v : "stats"; }
      catch { return "stats"; }
    };
    const view = ref(storedView());
    const session = ref(null);       // /ui/session ; null tant qu'inconnu
    const navOpen = ref(false);      // le menu latéral, sur mobile
    // Les conversations du chat (chat.js), listées dans le menu latéral.
    const chats = window.ChatStore;
    const chatState = chats.state;
    const chatList = chats.filtered;
    function openChat(id) { chats.select(id); setView("chat"); navOpen.value = false; }
    function newChat() { chats.newConv(); setView("chat"); navOpen.value = false; }
    const needLogin = ref(false);
    const loginKey = ref("");
    const loginError = ref("");
    const loginBusy = ref(false);
    // L'entrée : «oidc» (bouton vers le fournisseur) ou «key» (/ui/auth).
    const authMode = ref(null);
    const authLabel = ref("");
    // Un compte OIDC en attente ou bloqué : son état, et rien d'autre.
    const inactive = ref(null);

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
    // /healthz : cache web, serveurs MCP, et l'état du déploiement pour
    // l'onglet Configuration. null tant qu'il n'a pas répondu.
    const health = ref(null);
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
    const ask = (params, api = API) => {
      const q = Object.entries(params)
        .filter(([, v]) => v !== null && v !== undefined)
        .map(([k, v]) => k + "=" + encodeURIComponent(v)).join("&");
      return fetch(api + "?" + q, { headers: { accept: "application/json" } })
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
      if (needLogin.value || !session.value || view.value !== "stats") return;
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
          ask({
            start_time: start, bucket_width: width, "group_by[]": "model",
            limit: Math.min(Math.ceil((stamp - start) / seconds) + 1, 180),
          }),
          ask({
            start_time: span ? start : 0, bucket_width: "all",
            "group_by[]": "tool,endpoint", limit: 1,
          }, API_TOOLS).catch(() => null),
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
        if (e === 401) needLogin.value = true;
        /* sinon proxy injoignable : on retentera dans 5 s */
      }
    }

    async function loadSession() {
      try {
        const r = await fetch("/ui/session", { headers: { accept: "application/json" } });
        if (r.status === 401) { await loadAuth(); needLogin.value = true; session.value = null; return; }
        if (!r.ok) throw new Error("HTTP " + r.status);
        const s = await r.json();
        needLogin.value = false;
        if (s.account && !["admin", "user"].includes(s.account.role)) {
          inactive.value = s.account;
          session.value = null;
          return;
        }
        inactive.value = null;
        session.value = s;
      } catch (e) {
        // Proxy injoignable : réessayé dans 5 s, par la boucle.
        setTimeout(loadSession, REFRESH);
      }
    }

    async function loadAuth() {
      if (authMode.value) return;
      try {
        const a = await (await fetch("/ui/auth", { headers: { accept: "application/json" } })).json();
        authMode.value = a.mode || "key";
        authLabel.value = a.label || "Se connecter";
      } catch { authMode.value = "key"; }
    }

    async function login() {
      if (!loginKey.value.trim() || loginBusy.value) return;
      loginBusy.value = true;
      loginError.value = "";
      try {
        const r = await fetch("/ui/login", {
          method: "POST",
          headers: { "content-type": "application/json", accept: "application/json" },
          body: JSON.stringify({ key: loginKey.value.trim() }),
        });
        if (r.status === 401) { loginError.value = "Clé refusée."; return; }
        if (!r.ok) { loginError.value = "Le proxy a répondu " + r.status + "."; return; }
        loginKey.value = "";
        await loadSession();
        loadHealth();
        since.value = 0;
        poll();
      } catch (e) {
        loginError.value = "Proxy injoignable.";
      } finally {
        loginBusy.value = false;
      }
    }

    async function logout() {
      try {
        await fetch("/ui/logout", { method: "POST",
          headers: { "content-type": "application/json" }, body: "{}" });
      } catch { /* le cookie partira au prochain 401 */ }
      session.value = null;
      inactive.value = null;
      await loadAuth();
      buckets.value = [];
      toolResults.value = [];
      loaded.value = false;
      needLogin.value = true;
    }

    // Le chat dit qu'il a reçu un 401 : même chose qu'ici.
    function onUnauthorized() { needLogin.value = true; session.value = null; }

    function setView(id) {
      navOpen.value = false;
      if (id === view.value || !views.some((v) => v.id === id)) return;
      view.value = id;
      try { localStorage.setItem(STORE_VIEW, id); } catch { /* sans stockage */ }
      history.replaceState(null, "", location.pathname + location.search + "#" + id);
      if (id === "stats") poll();
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
      if (e.key === "Escape" && navOpen.value) { navOpen.value = false; return; }
      if (e.ctrlKey || e.metaKey || e.altKey || view.value !== "stats") return;
      if (/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName)) return;
      const key = String(e.key || "").toUpperCase();
      const hit = windows.find((w) => w.key === key);
      if (hit) { e.preventDefault(); select(hit.id); return; }
      const met = metrics.find((m) => m.key === key);
      if (met) { e.preventDefault(); selectMetric(met.id); }
    }

    // Onglet masqué : plus aucune requête. Personne ne regarde, et au
    // retour on rafraîchit immédiatement plutôt que d'afficher des
    // chiffres vieux de plusieurs minutes.
    function onVisibility() {
      if (!document.hidden) poll();
    }

    // L'état détaillé du déploiement : /ui/health, sous la session de la
    // page (/healthz sans clé ne dit plus que « ok »). Lu une fois la
    // session ouverte.
    function loadHealth() {
      fetch("/ui/health", { headers: { accept: "application/json" } })
        .then((r) => (r.ok ? r.json() : null))
        // Un échec passager garde le dernier état connu : sinon les
        // exemples et le cache web clignoteraient à chaque relecture.
        .then((h) => { if (h || !health.value) health.value = h; })
        .catch(() => {});
    }

    let timers = [];
    // L'application installable : son service worker (/ui/sw.js), qui ne
    // garde rien. Refusé hors HTTPS (sauf localhost) : sans effet alors.
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/ui/sw.js", { scope: "/ui" }).catch(() => {});
    }

    onMounted(async () => {
      // Le retour d'une connexion OIDC refusée : dit sur l'écran d'entrée,
      // puis retiré de la barre d'adresse. Une «?key=» d'un ancien favori
      // n'ouvre plus rien : retirée aussi, elle n'a rien à faire là.
      const params = new URLSearchParams(location.search);
      if (params.has("auth_error")) loginError.value = params.get("auth_error");
      if (params.has("key") || params.has("auth_error")) {
        history.replaceState(null, "", location.pathname + location.hash);
      }
      await loadSession();
      loadHealth();
      poll();
      timers = [
        setInterval(() => { if (!document.hidden) poll(); }, REFRESH),
        setInterval(() => { now.value = Date.now() / 1000; }, 1000),
        // /healthz relu de temps en temps : le cache web bouge.
        setInterval(() => { if (!document.hidden) loadHealth(); }, 6 * REFRESH),
      ];
      window.addEventListener("keydown", onKey);
      document.addEventListener("visibilitychange", onVisibility);
    });
    onUnmounted(() => {
      timers.forEach(clearInterval);
      window.removeEventListener("keydown", onKey);
      document.removeEventListener("visibilitychange", onVisibility);
    });

    return { views, view, setView, navOpen, chats, chatState, chatList, openChat,
             newChat, session, needLogin, loginKey, loginError,
             loginBusy, login, logout, onUnauthorized, authMode, authLabel,
             inactive, loadSession,
             windows, current, metrics, metric, metricUnit, metricLabel,
             models, totals, bars, axis, peak, since, now, tools, toolCalls,
             loaded, entering, webCache, mcpServers, health,
             note, shortcuts, bucketLabel, successRate,
             backendSummary, num, ms, ago, dur, moment, tickLabel, select,
             selectMetric };
  },
// La mémoire : un composant à part (memory.js), ses propres appels — dans la
// configuration et à côté du chat.
}).component("memory-panel", window.MemoryPanel)
  .component("chat-panel", window.ChatPanel)
  .component("config-panel", window.ConfigPanel).mount("#app");
