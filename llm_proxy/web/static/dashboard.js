/*
  Tout le tableau de bord tient ici. Il ne consomme QUE l'Usage API du
  proxy (/ui/usage = /v1/organization/usage/completions servi sous /ui,
  pour que le cookie de la page suffise).

  Deux appels par rafraîchissement :
    1. bucket_width=all&group_by=model → UN seau couvrant la période :
       les totaux et la ligne par modèle. Un p95 ne se recompose pas à
       partir de seaux plus fins, d'où cette passe. En vue « Tout », ce
       seau révèle aussi la date du plus ancien enregistrement, qui sert
       de départ au second appel.
    2. bucket_width=1h|1d → la courbe du trafic, un point par seau.

  Trois détails évitent le clignotement : la réponse est comparée à la
  précédente ; une valeur n'est réécrite que si elle a changé (`set`) ;
  les barres ne sont reconstruites que si le découpage change, sinon
  seules leurs hauteurs bougent.
*/
(function () {
  var API = "/ui/usage";
  var REFRESH = 5000;
  var STORE = "llm-proxy-window";
  // Tons chauds lisibles sur les deux thèmes, cyclés au-delà de six modèles.
  var COLORS = ["#d97757", "#e0a458", "#7d9b76", "#6b8fa3", "#a37ba0",
                "#c08552"];

  // Les trois périodes, dans l'ordre du sélecteur. `span` en secondes,
  // null = depuis le premier enregistrement connu.
  var WINDOWS = {
    day:  { span: 86400, note: "24 dernières heures", key: "D" },
    week: { span: 604800, note: "7 derniers jours", key: "W" },
    all:  { span: null, note: "tout l'historique conservé", key: "A" }
  };
  var current = WINDOWS[localStorage.getItem(STORE)] ?
    localStorage.getItem(STORE) : "all";
  var lastPayload = null;   // pour ne redessiner que si ça a bougé
  var lastShape = null;     // découpage des barres du dernier rendu

  // ── formatage ─────────────────────────────────────────────────────────
  // Entier à la française : espace comme séparateur de milliers.
  function n(v) {
    return String(Math.round(v) || 0).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
  }
  function ms(s) {
    return s >= 1 ? s.toFixed(1) + " s" : Math.round(s * 1000) + " ms";
  }
  function ago(s) {
    s = Math.max(0, Math.round(s));
    if (s < 5) return "à l'instant";
    if (s < 60) return "il y a " + s + " s";
    if (s < 3600) return "il y a " + Math.floor(s / 60) + " min";
    if (s < 86400) return "il y a " + Math.floor(s / 3600) + " h";
    return "il y a " + Math.floor(s / 86400) + " j";
  }
  function dur(s) {
    s = Math.max(0, Math.round(s));
    if (s < 60) return s + " s";
    if (s < 3600) return Math.floor(s / 60) + " min";
    if (s < 86400) return Math.floor(s / 3600) + " h " +
      String(Math.floor((s % 3600) / 60)).padStart(2, "0");
    return Math.floor(s / 86400) + " j";
  }
  function clock(ts) {
    var d = new Date(ts * 1000);
    return String(d.getHours()).padStart(2, "0") + " h";
  }
  function day(ts) {
    return new Date(ts * 1000).toLocaleDateString("fr-FR",
      { day: "numeric", month: "short" });
  }
  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  function el(id) { return document.getElementById(id); }
  function set(node, html) {  // n'écrit que si ça change
    if (node && node.innerHTML !== html) node.innerHTML = html;
  }

  // ── lecture de l'Usage API ────────────────────────────────────────────
  function ask(params) {
    var q = Object.keys(params).filter(function (k) {
      return params[k] !== null && params[k] !== undefined;
    }).map(function (k) {
      return k + "=" + encodeURIComponent(params[k]);
    }).join("&");
    return fetch(API + "?" + q, { headers: { accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); });
  }

  // Le seau unique de la période : totaux et détail par modèle.
  function fetchRollup(win) {
    var span = WINDOWS[win].span;
    return ask({
      start_time: span ? Math.floor(Date.now() / 1000) - span : 0,
      bucket_width: "all",
      group_by: "model",
      limit: 1
    });
  }

  // La courbe : un seau d'une heure sur la journée, d'un jour au-delà.
  // Sur « Tout », le découpage suit l'étendue réelle des données.
  function fetchSeries(win, since) {
    var now = Math.floor(Date.now() / 1000);
    var span = WINDOWS[win].span || Math.max(now - since, 3600);
    var width = span <= 2 * 86400 ? "1h" : "1d";
    var seconds = width === "1h" ? 3600 : 86400;
    return ask({
      start_time: now - span,
      bucket_width: width,
      limit: Math.min(Math.ceil(span / seconds) + 1, 180)
    }).then(function (page) {
      return { width: width, page: page };
    });
  }

  // ── cartes ────────────────────────────────────────────────────────────
  function totals(results) {
    var t = { requests: 0, errors: 0, input: 0, output: 0, streamed: 0,
              estimated: 0, backends: {} };
    results.forEach(function (r) {
      t.requests += r.num_model_requests;
      t.errors += r.num_errors || 0;
      t.input += r.input_tokens;
      t.output += r.output_tokens;
      t.streamed += r.num_streamed_requests || 0;
      t.estimated += r.num_estimated_requests || 0;
      // Le backend n'est pas un champ de l'API : c'est le préfixe du
      // modèle, qui EST le discriminant de routage du proxy.
      var backend = String(r.model || "").split("/")[0] || "—";
      t.backends[backend] = (t.backends[backend] || 0) + r.num_model_requests;
    });
    return t;
  }

  function cards(t, results, since) {
    var tokens = t.input + t.output;
    var rate = t.requests
      ? (100 * (t.requests - t.errors) / t.requests).toFixed(1) + "%" : "—";
    var backends = Object.keys(t.backends).sort().map(function (name) {
      return name + " : " + n(t.backends[name]) + " req";
    }).join(" · ") || "aucun trafic";

    set(el("c-req"), n(t.requests));
    set(el("c-req-hint"), rate + " en succès · " + n(t.streamed) + " en flux");
    set(el("c-tok"), n(tokens));
    set(el("c-tok-hint"), n(t.input) + " entrée · " + n(t.output) + " sortie");
    set(el("c-mod"), n(results.length));
    set(el("c-mod-hint"), esc(backends));
    set(el("c-err"), n(t.errors));
    set(el("c-err-hint"), since
      ? 'mesures depuis <span data-since="' + since + '"></span>'
      : "aucune mesure enregistrée");
  }

  // ── répartition des tokens ────────────────────────────────────────────
  function share(results, t) {
    var total = t.input + t.output;
    el("share").hidden = !total;
    if (!total) return;
    var bar = "", legend = "";
    results.forEach(function (m, i) {
      var tokens = m.input_tokens + m.output_tokens;
      var pct = 100 * tokens / total;
      var color = COLORS[i % COLORS.length];
      bar += '<i style="width:' + pct.toFixed(2) + "%;background:" + color +
        '" title="' + esc(m.model) + " — " + n(tokens) + ' tokens"></i>';
      legend += '<span><b style="background:' + color + '"></b><em>' +
        esc(m.model) + "</em>&nbsp;" + Math.round(pct) + "%</span>";
    });
    set(el("share-bar"), bar);
    set(el("share-legend"), legend);
  }

  // ── courbe du trafic ──────────────────────────────────────────────────
  // Une seule mesure (les requêtes) : un seul axe, une seule couleur. Les
  // tokens sont dans l'infobulle — jamais sur un second axe.
  var buckets = [];

  function chart(series) {
    buckets = series.page.data || [];
    var panel = el("timeline");
    panel.hidden = !buckets.length;
    if (!buckets.length) { lastShape = null; return; }

    var peak = buckets.reduce(function (m, b) {
      return Math.max(m, count(b));
    }, 0);
    var host = el("bars");
    // Le découpage n'a pas changé → on ne refait pas le DOM, on laisse
    // les hauteurs glisser vers leur nouvelle valeur.
    var shape = series.width + ":" + buckets.length + ":" +
      (buckets[0] ? buckets[0].start_time : 0);
    var rebuild = shape !== lastShape;
    lastShape = shape;

    if (rebuild) {
      host.innerHTML = buckets.map(function (b, i) {
        return '<i style="--i:' + i + '"' + (count(b) ? "" : ' class="zero"') +
          '><b style="height:0"></b></i>';
      }).join("");
      // Les barres montent une fois, en cascade, puis la classe s'en va :
      // les rafraîchissements suivants n'ont plus de décalage à subir.
      host.classList.add("entering");
      setTimeout(function () { host.classList.remove("entering"); },
        280 + buckets.length * 14);
    }
    var bars = host.children;
    for (var i = 0; i < bars.length; i++) {
      var value = count(buckets[i]);
      // 2 px de socle : un seau non vide mais minuscule doit rester
      // visible, et un seau vide doit rester vide.
      var height = peak ? Math.max(100 * value / peak, value ? 2 : 0) : 0;
      bars[i].classList.toggle("zero", !value);
      var fill = bars[i].firstChild;
      var css = height.toFixed(2) + "%";
      if (fill.style.height !== css) fill.style.height = css;
    }
    requestAnimationFrame(function () { host.offsetHeight; });

    set(el("chart-meta"), buckets.length + " × " +
      (series.width === "1h" ? "1 heure" : "1 jour") + " · pic " +
      n(peak) + " req");
    axis(series.width);
  }

  function count(b) {
    return (b.results || []).reduce(function (s, r) {
      return s + r.num_model_requests;
    }, 0);
  }
  function tokensOf(b) {
    return (b.results || []).reduce(function (s, r) {
      return s + r.input_tokens + r.output_tokens;
    }, 0);
  }

  function axis(width) {
    // Quatre repères suffisent : l'axe situe, il n'énumère pas.
    var picks = [], step = Math.max(1, Math.floor(buckets.length / 3));
    for (var i = 0; i < buckets.length; i += step) picks.push(buckets[i]);
    if (picks[picks.length - 1] !== buckets[buckets.length - 1]) {
      picks.push(buckets[buckets.length - 1]);
    }
    set(el("axis"), picks.map(function (b) {
      return "<span>" + (width === "1h" ? clock(b.start_time)
        : day(b.start_time)) + "</span>";
    }).join(""));
  }

  function hover(event) {
    var slot = event.target.closest ? event.target.closest(".bars > i") : null;
    var tip = el("tip");
    if (!slot) { tip.hidden = true; return; }
    var i = Array.prototype.indexOf.call(slot.parentNode.children, slot);
    var b = buckets[i];
    if (!b) { tip.hidden = true; return; }
    var start = new Date(b.start_time * 1000);
    var when = start.toLocaleString("fr-FR", {
      day: "numeric", month: "short",
      hour: "2-digit", minute: "2-digit"
    });
    tip.innerHTML = '<div class="when">' + esc(when) + "</div>" +
      '<div class="row"><span>Requêtes</span><b>' + n(count(b)) + "</b></div>" +
      '<div class="row"><span>Tokens</span><b>' + n(tokensOf(b)) + "</b></div>";
    var box = slot.getBoundingClientRect();
    var host = el("timeline").getBoundingClientRect();
    tip.style.left = (box.left - host.left + box.width / 2) + "px";
    tip.style.top = (box.top - host.top - 10) + "px";
    tip.hidden = false;
  }

  // ── tableau par modèle ────────────────────────────────────────────────
  function rows(results, since) {
    if (!results.length) {
      set(el("rows"), '<tr><td colspan="10"><div class="empty">' +
        '<div class="big">Aucune génération sur cette période</div>' +
        "<div>" + (since
          ? "Les mesures remontent à <span data-since=\"" + since +
            "\"></span> ; essayez une période plus large."
          : "Les modèles apparaîtront ici dès leur première requête.") +
        "</div></div></td></tr>");
      return;
    }
    set(el("rows"), results.map(function (m, i) {
      var total = m.input_tokens + m.output_tokens;
      var exact = m.num_model_requests - (m.num_estimated_requests || 0);
      return "<tr>" +
        '<td><div class="model"><b style="background:' +
          COLORS[i % COLORS.length] + '"></b><span>' +
          '<span class="id">' + esc(m.model) + "</span><br>" +
          '<span class="backend">' +
            esc(String(m.model || "").split("/")[0]) + " · " +
            n(m.num_streamed_requests || 0) + " en flux</span></span></div></td>" +
        "<td>" + n(m.num_model_requests) + "</td>" +
        "<td>" + (m.num_errors
          ? '<span class="err">' + n(m.num_errors) + "</span>"
          : '<span class="dim">0</span>') + "</td>" +
        "<td>" + n(m.input_tokens) + "</td>" +
        "<td>" + n(m.output_tokens) + "</td>" +
        "<td><strong>" + n(total) + "</strong></td>" +
        "<td>" + n(m.num_model_requests ? total / m.num_model_requests : 0) +
          "</td>" +
        "<td>" + ms(m.p95_latency_seconds || 0) + "</td>" +
        "<td>" + accounting(exact, m.num_estimated_requests || 0) + "</td>" +
        '<td class="dim"><span data-ts="' + m.last_request_time +
          '"></span></td>' +
        "</tr>";
    }).join(""));
  }

  // Provenance des tokens : `usage` de l'upstream, ou estimation.
  function accounting(exact, estimated) {
    if (!estimated) return '<span class="tag exact">exact</span>';
    if (!exact) return '<span class="tag est">estimé</span>';
    return '<span class="tag est">' + n(exact) + " exact · " +
      n(estimated) + " estimé</span>";
  }

  // ── horloge relative ──────────────────────────────────────────────────
  function tick() {
    var now = Date.now() / 1000;
    document.querySelectorAll("[data-ts]").forEach(function (e) {
      e.textContent = ago(now - parseFloat(e.dataset.ts));
    });
    document.querySelectorAll("[data-since]").forEach(function (e) {
      e.textContent = dur(now - parseFloat(e.dataset.since));
    });
  }

  // ── sélecteur de période ──────────────────────────────────────────────
  function select(win, force) {
    if (!WINDOWS[win] || (win === current && !force)) return;
    current = win;
    localStorage.setItem(STORE, win);
    lastPayload = null;   // période différente : on redessine forcément
    lastShape = null;
    document.querySelectorAll("#range button").forEach(function (b) {
      b.setAttribute("aria-pressed", String(b.dataset.window === win));
    });
    set(el("range-note"), esc(WINDOWS[win].note) + " · raccourcis " +
      Object.keys(WINDOWS).map(function (k) {
        return WINDOWS[k].key;
      }).join(" / "));
    poll();
  }

  el("range").addEventListener("click", function (e) {
    if (e.target.dataset.window) select(e.target.dataset.window);
  });
  // Raccourcis A / W / D. Ignorés dès qu'un modificateur est enfoncé ou
  // qu'un champ a le focus : un raccourci ne doit jamais voler une frappe.
  document.addEventListener("keydown", function (e) {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    var node = document.activeElement;
    if (node && /^(INPUT|TEXTAREA|SELECT)$/.test(node.tagName)) return;
    var letter = String(e.key || "").toUpperCase();
    Object.keys(WINDOWS).forEach(function (win) {
      if (WINDOWS[win].key === letter) { e.preventDefault(); select(win); }
    });
  });
  el("bars").addEventListener("mousemove", hover);
  el("chart-body").addEventListener("mouseleave", function () {
    el("tip").hidden = true;
  });

  // ── boucle ────────────────────────────────────────────────────────────
  function poll() {
    var win = current;
    fetchRollup(win).then(function (page) {
      if (win !== current) return;          // période changée entre-temps
      var bucket = (page.data || [])[0] || { results: [], start_time: 0 };
      var results = (bucket.results || []).slice().sort(function (a, b) {
        // Ordre d'APPARITION, volontairement stable : le tableau ne voit
        // donc jamais ses lignes se réordonner sous le curseur.
        return a.first_request_time - b.first_request_time;
      });
      var since = results.length ? bucket.start_time : 0;
      if (!results.length) {
        // Base vide : pas de série à demander (sans premier
        // enregistrement, la fenêtre « Tout » remonterait à 1970).
        if (win + "empty" === lastPayload) return;
        lastPayload = win + "empty";
        lastShape = null;
        el("timeline").hidden = true;
        cards(totals([]), [], 0);
        share([], totals([]));
        rows([], 0);
        return;
      }
      return fetchSeries(win, bucket.start_time).then(function (series) {
        if (win !== current) return;
        var signature = win + JSON.stringify(results) +
          JSON.stringify(series.page.data);
        if (signature === lastPayload) return;   // rien n'a bougé
        lastPayload = signature;
        var t = totals(results);
        cards(t, results, since);
        share(results, t);
        chart(series);
        rows(results, since);
        tick();
      });
    }).catch(function () { /* proxy injoignable : on retentera dans 5 s */ });
  }

  select(current, true);
  setInterval(poll, REFRESH);
  setInterval(tick, 1000);
})();
