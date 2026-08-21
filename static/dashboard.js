(function () {
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
  // Le temps qui passe est affaire de navigateur : aucune requête, aucun
  // redessin serveur pour faire vieillir un « il y a 3 min ».
  function tick() {
    var now = Date.now() / 1000;
    document.querySelectorAll("[data-ts]").forEach(function (el) {
      el.textContent = ago(now - parseFloat(el.dataset.ts));
    });
    document.querySelectorAll("[data-since]").forEach(function (el) {
      el.textContent = dur(now - parseFloat(el.dataset.since));
    });
  }
  setInterval(tick, 1000);
  document.addEventListener("htmx:load", tick);
  document.addEventListener("DOMContentLoaded", tick);
  // Surligne brièvement les seules valeurs que le serveur vient d'envoyer.
  document.addEventListener("htmx:oobAfterSwap", function (e) {
    var el = e.detail.target;
    if (!el || !el.classList.contains("v")) return;
    el.classList.remove("fresh");
    void el.offsetWidth;
    el.classList.add("fresh");
  });
})();
