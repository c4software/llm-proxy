"""
Tableau de bord HTML du proxy : une page + un fragment rafraîchi par HTMX,
tous deux construits à partir de stats.snapshot() — la même source que
GET /v1/stats, donc rien à resynchroniser.

HTMX est servi depuis static/htmx.min.js (vendorisé) : le tableau de bord
fonctionne sans accès Internet et sans CDN tiers.

Ce module ne fait que du texte : aucune connaissance de FastAPI.
"""

import html
import time

import stats

# Rafraîchissement du fragment (secondes) — hx-trigger="every Ns".
REFRESH_SECONDS = 5


def _n(value) -> str:
    """Entier à la française : espace fine insécable comme séparateur."""
    try:
        return f"{int(value):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "0"


def _dur(seconds: float) -> str:
    s = int(max(seconds, 0))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        return f"{s // 3600} h {(s % 3600) // 60:02d}"
    return f"{s // 86400} j {(s % 86400) // 3600} h"


def _ago(ts: float) -> str:
    if not ts:
        return "—"
    delta = time.time() - ts
    return "à l'instant" if delta < 5 else f"il y a {_dur(delta)}"


def _ms(seconds: float) -> str:
    if seconds >= 1:
        return f"{seconds:.1f} s"
    return f"{int(seconds * 1000)} ms"


def _esc(value) -> str:
    return html.escape(str(value), quote=True)


CSS = """
:root {
  color-scheme: light dark;
  --bg: #faf9f5; --panel: #fffefb; --ink: #191919; --muted: #6b6963;
  --line: #e6e2d8; --accent: #d97757; --accent-soft: #f4e2da;
  --ok: #3f7d58; --err: #b4402f; --track: #efece3;
  --radius: 14px;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #1a1917; --panel: #211f1d; --ink: #f2efe8; --muted: #9c968b;
    --line: #322f2b; --accent: #e08b6d; --accent-soft: #3a2b24;
    --ok: #79b58f; --err: #e08573; --track: #2b2825;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 15px/1.55 ui-sans-serif, -apple-system, "Segoe UI", Inter, system-ui, sans-serif;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 1180px; margin: 0 auto; padding: 48px 28px 72px; }
header { margin-bottom: 34px; }
.brand {
  display: inline-flex; align-items: center; gap: 9px;
  font-size: 13px; letter-spacing: .13em; text-transform: uppercase;
  color: var(--muted); margin-bottom: 18px;
}
.dot {
  width: 9px; height: 9px; border-radius: 50%; background: var(--accent);
  box-shadow: 0 0 0 4px var(--accent-soft);
}
h1 {
  font: 400 40px/1.15 ui-serif, Georgia, "Times New Roman", serif;
  letter-spacing: -.02em; margin: 0 0 10px;
}
.sub { margin: 0; color: var(--muted); max-width: 62ch; }
.cards {
  display: grid; gap: 14px; margin-bottom: 26px;
  grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
}
.card {
  background: var(--panel); border: 1px solid var(--line);
  border-radius: var(--radius); padding: 18px 20px;
}
.card .label {
  font-size: 11.5px; letter-spacing: .11em; text-transform: uppercase;
  color: var(--muted);
}
.card .value {
  font: 400 30px/1.2 ui-serif, Georgia, serif; margin-top: 8px;
  font-variant-numeric: tabular-nums; letter-spacing: -.01em;
}
.card .hint { font-size: 12.5px; color: var(--muted); margin-top: 4px; }
.panel {
  background: var(--panel); border: 1px solid var(--line);
  border-radius: var(--radius); overflow: hidden;
}
.panel-head {
  display: flex; align-items: baseline; justify-content: space-between;
  gap: 16px; padding: 18px 20px 14px; border-bottom: 1px solid var(--line);
}
.panel-head h2 {
  font: 400 19px/1.2 ui-serif, Georgia, serif; margin: 0;
}
.panel-head .meta { font-size: 12.5px; color: var(--muted); }
.scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th {
  text-align: right; font-weight: 500; font-size: 11.5px;
  letter-spacing: .09em; text-transform: uppercase; color: var(--muted);
  padding: 12px 14px; white-space: nowrap; border-bottom: 1px solid var(--line);
}
th:first-child, td:first-child { text-align: left; padding-left: 20px; }
th:last-child, td:last-child { padding-right: 20px; }
td {
  padding: 13px 14px; text-align: right; white-space: nowrap;
  border-bottom: 1px solid var(--line); font-variant-numeric: tabular-nums;
}
tr:last-child td { border-bottom: 0; }
tbody tr:hover td { background: color-mix(in srgb, var(--accent-soft) 45%, transparent); }
.model { display: flex; flex-direction: column; gap: 3px; }
.model .id {
  font-family: ui-monospace, SFMono-Regular, "JetBrains Mono", monospace;
  font-size: 13.5px;
}
.model .backend { font-size: 12px; color: var(--muted); }
.bar { height: 4px; border-radius: 3px; background: var(--track); margin-top: 7px; }
.bar > i { display: block; height: 100%; border-radius: 3px; background: var(--accent); }
.tag {
  display: inline-block; font-size: 11px; letter-spacing: .04em;
  padding: 3px 8px; border-radius: 999px; border: 1px solid var(--line);
  color: var(--muted);
}
.tag.exact { color: var(--ok); border-color: color-mix(in srgb, var(--ok) 35%, transparent); }
.tag.est { color: var(--accent); border-color: color-mix(in srgb, var(--accent) 40%, transparent); }
.err { color: var(--err); }
.dim { color: var(--muted); }
.empty { padding: 56px 20px; text-align: center; color: var(--muted); }
.empty .big {
  font: 400 22px/1.3 ui-serif, Georgia, serif; color: var(--ink);
  margin-bottom: 8px;
}
code {
  font-family: ui-monospace, SFMono-Regular, monospace; font-size: 13px;
  background: var(--track); padding: 2px 6px; border-radius: 6px;
}
footer { margin-top: 26px; font-size: 12.5px; color: var(--muted); }
footer a { color: inherit; }
.live { display: inline-flex; align-items: center; gap: 7px; }
.live i {
  width: 7px; height: 7px; border-radius: 50%; background: var(--ok);
  animation: pulse 2.4s ease-in-out infinite;
}
@keyframes pulse { 0%,100% { opacity: 1 } 50% { opacity: .25 } }
#stats.htmx-swapping { opacity: .55; transition: opacity .12s ease; }
"""


def page(refresh: int = REFRESH_SECONDS) -> str:
    """Page complète. Le corps du tableau vient du fragment, chargé puis
    rafraîchi par HTMX — la page elle-même n'est jamais rechargée."""
    return f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>llm-proxy — statistiques</title>
<style>{CSS}</style>
<script src="/ui/htmx.min.js" defer></script>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand"><span class="dot"></span> llm-proxy</div>
    <h1>Statistiques d'usage</h1>
    <p class="sub">Compteurs tenus en mémoire depuis le démarrage du proxy.
    Un modèle n'apparaît qu'à partir de sa première génération ; les mêmes
    chiffres sont disponibles en JSON sur <code>/v1/stats</code>.</p>
  </header>
  <main id="stats"
        hx-get="/ui/stats"
        hx-trigger="load, every {refresh}s"
        hx-swap="innerHTML swap:120ms">
    <div class="empty">Chargement…</div>
  </main>
  <footer>Rafraîchi toutes les {refresh} secondes · les compteurs
  repartent de zéro à chaque redémarrage du proxy.</footer>
</div>
</body>
</html>"""


def _row(model: dict, max_tokens: int) -> str:
    usage = model["usage"]
    total = usage["total_tokens"]
    share = int(100 * total / max_tokens) if max_tokens else 0
    acc = model["tokens_accounting"]
    if acc["estimated_requests"] == 0:
        badge = '<span class="tag exact">exact</span>'
    elif acc["exact_requests"] == 0:
        badge = '<span class="tag est">estimé</span>'
    else:
        badge = (f'<span class="tag est">{_n(acc["exact_requests"])} exact · '
                 f'{_n(acc["estimated_requests"])} estimé</span>')
    errors = (f'<span class="err">{_n(model["requests_error"])}</span>'
              if model["requests_error"] else '<span class="dim">0</span>')
    return f"""<tr>
  <td>
    <div class="model">
      <span class="id">{_esc(model["id"])}</span>
      <span class="backend">{_esc(model["backend"])} ·
        {_n(model["streamed_requests"])} en flux</span>
    </div>
    <div class="bar"><i style="width:{share}%"></i></div>
  </td>
  <td>{_n(model["requests"])}</td>
  <td>{errors}</td>
  <td>{_n(usage["prompt_tokens"])}</td>
  <td>{_n(usage["completion_tokens"])}</td>
  <td><strong>{_n(total)}</strong></td>
  <td>{_n(model["avg_tokens_per_request"])}</td>
  <td>{_ms(model["latency_seconds"]["p95"])}</td>
  <td>{badge}</td>
  <td class="dim">{_ago(model["last_request"])}</td>
</tr>"""


def fragment(snap: dict | None = None) -> str:
    """Bloc rafraîchi par HTMX : cartes de synthèse + tableau par modèle."""
    snap = snap if snap is not None else stats.snapshot()
    data = snap["data"]
    totals = snap["totals"]
    backends = snap["backends"]

    if not data:
        return f"""<div class="panel">
  <div class="empty">
    <div class="big">Aucune génération pour l'instant</div>
    <div>Le proxy tourne depuis {_dur(snap["uptime_seconds"])}.
    Les modèles apparaîtront ici dès leur première requête.</div>
  </div>
</div>"""

    ok = totals["requests_ok"]
    rate = f"{100 * ok / totals['requests']:.1f}" if totals["requests"] else "—"
    backends_line = " · ".join(
        f"{_esc(name)} : {_n(b['requests'])} req" for name, b in backends.items()
    )
    max_tokens = max(m["usage"]["total_tokens"] for m in data)
    rows = "\n".join(_row(m, max_tokens) for m in data)

    return f"""<div class="cards">
  <div class="card">
    <div class="label">Requêtes</div>
    <div class="value">{_n(totals["requests"])}</div>
    <div class="hint">{rate}% en succès ·
      {_n(totals["streamed_requests"])} en flux</div>
  </div>
  <div class="card">
    <div class="label">Tokens</div>
    <div class="value">{_n(totals["total_tokens"])}</div>
    <div class="hint">{_n(totals["prompt_tokens"])} entrée ·
      {_n(totals["completion_tokens"])} sortie</div>
  </div>
  <div class="card">
    <div class="label">Modèles actifs</div>
    <div class="value">{_n(totals["models"])}</div>
    <div class="hint">{backends_line or "—"}</div>
  </div>
  <div class="card">
    <div class="label">Erreurs</div>
    <div class="value">{_n(totals["requests_error"])}</div>
    <div class="hint">depuis {_dur(snap["uptime_seconds"])} de service</div>
  </div>
</div>

<div class="panel">
  <div class="panel-head">
    <h2>Par modèle</h2>
    <span class="meta live"><i></i>à jour {_esc(time.strftime("%H:%M:%S"))}</span>
  </div>
  <div class="scroll">
    <table>
      <thead>
        <tr>
          <th>Modèle</th><th>Requêtes</th><th>Erreurs</th>
          <th>Entrée</th><th>Sortie</th><th>Total</th>
          <th>Moy./req</th><th>p95</th><th>Comptage</th><th>Dernière</th>
        </tr>
      </thead>
      <tbody>
{rows}
      </tbody>
    </table>
  </div>
</div>"""
