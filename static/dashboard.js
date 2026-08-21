/*
  Tout le tableau de bord tient ici : on lit le JSON de /ui/stats (le même
  que /v1/stats) toutes les 5 s et on remplit la page. Aucun HTML n'est
  produit côté serveur.

  Deux détails évitent le clignotement :
   - `revision` : tant qu'elle n'a pas bougé, aucune requête n'a été
     servie depuis le dernier rendu, on ne touche pas au DOM ;
   - une valeur n'est réécrite que si elle a changé (`set`), donc un
     redessin ne perturbe ni la sélection de texte ni le survol.

  Les « il y a 3 min » vieillissent tout seuls, à partir du timestamp
  gardé sur la cellule : le temps qui passe ne coûte aucune requête.
*/
(function () {
  var URL = "/ui/stats";
  var REFRESH = 5000;
  // Tons chauds lisibles sur les deux thèmes, cyclés au-delà de six modèles.
  var COLORS = ["#d97757", "#e0a458", "#7d9b76", "#6b8fa3", "#a37ba0",
                "#c08552"];
  var revision = null;

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
  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  function el(id) { return document.getElementById(id); }
  function set(node, html) {  // n'écrit que si ça change
    if (node && node.innerHTML !== html) node.innerHTML = html;
  }

  // Provenance des tokens : `usage` de l'upstream, ou estimation.
  function accounting(a) {
    if (!a.estimated_requests) return '<span class="tag exact">exact</span>';
    if (!a.exact_requests) return '<span class="tag est">estimé</span>';
    return '<span class="tag est">' + n(a.exact_requests) + " exact · " +
      n(a.estimated_requests) + " estimé</span>";
  }

  function cards(s) {
    var t = s.totals;
    var rate = t.requests
      ? (100 * t.requests_ok / t.requests).toFixed(1) + "%" : "—";
    var backends = Object.keys(s.backends).sort().map(function (name) {
      return name + " : " + n(s.backends[name].requests) + " req";
    }).join(" · ") || "aucun trafic";

    set(el("c-req"), n(t.requests));
    set(el("c-req-hint"), rate + " en succès · " + n(t.streamed_requests) +
      " en flux");
    set(el("c-tok"), n(t.total_tokens));
    set(el("c-tok-hint"), n(t.prompt_tokens) + " entrée · " +
      n(t.completion_tokens) + " sortie");
    set(el("c-mod"), n(t.models));
    set(el("c-mod-hint"), esc(backends));
    set(el("c-err"), n(t.requests_error));
    set(el("c-err-hint"), 'proxy en service depuis <span data-since="' +
      s.since + '"></span>');
  }

  function share(s) {
    var total = s.totals.total_tokens;
    el("share").hidden = !total;
    if (!total) return;
    var bar = "", legend = "";
    s.data.forEach(function (m, i) {
      var tokens = m.usage.total_tokens;
      var pct = 100 * tokens / total;
      var color = COLORS[i % COLORS.length];
      bar += '<i style="width:' + pct.toFixed(2) + "%;background:" + color +
        '" title="' + esc(m.id) + " — " + n(tokens) + ' tokens"></i>';
      legend += '<span><b style="background:' + color + '"></b><em>' +
        esc(m.id) + "</em>&nbsp;" + Math.round(pct) + "%</span>";
    });
    set(el("share-bar"), bar);
    set(el("share-legend"), legend);
  }

  function rows(s) {
    if (!s.data.length) {
      set(el("rows"), '<tr><td colspan="10"><div class="empty">' +
        '<div class="big">Aucune génération pour l\'instant</div>' +
        "<div>Le proxy tourne depuis " +
        '<span data-since="' + s.since + '"></span> ; les modèles ' +
        "apparaîtront ici dès leur première requête.</div></div></td></tr>");
      return;
    }
    set(el("rows"), s.data.map(function (m, i) {
      return "<tr>" +
        '<td><div class="model"><b style="background:' +
          COLORS[i % COLORS.length] + '"></b><span>' +
          '<span class="id">' + esc(m.id) + "</span><br>" +
          '<span class="backend">' + esc(m.backend) + " · " +
            n(m.streamed_requests) + " en flux</span></span></div></td>" +
        "<td>" + n(m.requests) + "</td>" +
        "<td>" + (m.requests_error
          ? '<span class="err">' + n(m.requests_error) + "</span>"
          : '<span class="dim">0</span>') + "</td>" +
        "<td>" + n(m.usage.prompt_tokens) + "</td>" +
        "<td>" + n(m.usage.completion_tokens) + "</td>" +
        "<td><strong>" + n(m.usage.total_tokens) + "</strong></td>" +
        "<td>" + n(m.avg_tokens_per_request) + "</td>" +
        "<td>" + ms(m.latency_seconds.p95) + "</td>" +
        "<td>" + accounting(m.tokens_accounting) + "</td>" +
        '<td class="dim"><span data-ts="' + m.last_request + '"></span></td>' +
        "</tr>";
    }).join(""));
  }

  function tick() {
    var now = Date.now() / 1000;
    document.querySelectorAll("[data-ts]").forEach(function (e) {
      e.textContent = ago(now - parseFloat(e.dataset.ts));
    });
    document.querySelectorAll("[data-since]").forEach(function (e) {
      e.textContent = dur(now - parseFloat(e.dataset.since));
    });
  }

  function poll() {
    fetch(URL, { headers: { accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (s) {
        if (!s || s.revision === revision) return;  // rien n'a bougé
        revision = s.revision;
        cards(s);
        share(s);
        rows(s);
        tick();
      })
      .catch(function () { /* proxy injoignable : on retentera dans 5 s */ });
  }

  poll();
  setInterval(poll, REFRESH);
  setInterval(tick, 1000);
})();
