/*
  Configuration → Usage : l'usage et les limites du compte, tels que les rend
  /ui/usage/agent (ui/agent_usage.py), l'enregistrement que lit aussi le
  panneau d'agents d'Omarchy par /v1/usage/agent. Les limites (part
  équitable, forfait mensuel de ses backends), les tokens des sept derniers
  jours et par modèle ; de quoi déposer l'enregistrement chez Omarchy (un
  service et un minuteur systemd de l'utilisateur). Relu toutes les
  REFRESH_MS.
*/
import { call } from "../lib/http.js";
import { num } from "../lib/format.js";
import { useCopy } from "../lib/files.js";
import { KEYFILE, KEY_SETUP } from "./shared.js";

const { ref, computed, onMounted, onUnmounted } = Vue;

const REFRESH_MS = 30_000;
const TZ = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
const UNIT = "llm-proxy-usage";

const day = (d) => new Date(d + "T12:00:00").toLocaleDateString("fr-FR", { weekday: "short", day: "numeric" });
/** Une part (0 à 1) en pourcentage ; deux décimales sous 1 %. */
function percent(p) {
  if (p > 0 && p < 0.0001) return "< 0,01 %";
  return p.toLocaleString("fr-FR", { style: "percent", maximumFractionDigits: p < 0.01 ? 2 : 0 });
}

/** Le service et le minuteur systemd qui déposent l'enregistrement chez Omarchy. */
function omarchyFiles() {
  // Dans une unité systemd, « % » ouvre un spécificateur : le fuseau va tel
  // quel (lettres, « / », « _ », « - »), son « + » (Etc/GMT+1) en %%2B.
  const url = `${location.origin}/v1/usage/agent?tz=${TZ.replace(/\+/g, "%%2B")}`;
  // $$ : un $ pour le shell, systemd en ferait sinon une de ses variables.
  // La clé passe à curl par son entrée (-H @-), jamais en argument : elle ne
  // se lit pas dans la liste des processus.
  const run = `d="$$HOME/.local/state/omarchy/agents/usage"; mkdir -p "$$d" && `
    + `printf "Authorization: Bearer %%s" "$$(cat ${KEYFILE.replace("~", () => "$$HOME")})" `
    + `| curl -fsS -H @- "${url}" -o "$$d/.llm-proxy.tmp" && mv "$$d/.llm-proxy.tmp" "$$d/llm-proxy.json"`;
  return [
    { name: `~/.config/systemd/user/${UNIT}.service`,
      content: `[Unit]\nDescription=Usage du proxy LLM pour le panneau d'agents d'Omarchy\n\n`
        + `[Service]\nType=oneshot\nExecStart=/bin/sh -c '${run}'\n` },
    { name: `~/.config/systemd/user/${UNIT}.timer`,
      content: `[Unit]\nDescription=Usage du proxy LLM pour le panneau d'agents d'Omarchy\n\n`
        + `[Timer]\nOnBootSec=1min\nOnUnitActiveSec=2min\n\n[Install]\nWantedBy=timers.target\n` },
    { name: "Mise en route",
      content: KEY_SETUP + `systemctl --user daemon-reload\nsystemctl --user enable --now ${UNIT}.timer\n` },
  ];
}

export const UsagePart = {
  setup() {
    const rec = ref(null);
    const error = ref("");
    let timer = null;

    async function load() {
      try {
        rec.value = await call("GET", "/ui/usage/agent?tz=" + encodeURIComponent(TZ));
        error.value = "";
      } catch (e) { error.value = e.message; }
    }
    onMounted(() => { load(); timer = setInterval(load, REFRESH_MS); });
    onUnmounted(() => clearInterval(timer));

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
      return d && !isNaN(d) ? ` · réinitialisation le ${d.toLocaleDateString("fr-FR")}` : "";
    };
    const files = omarchyFiles();
    const { copied, copy } = useCopy();
    return { rec, error, days, models, resets, percent, num, files, copied, copy };
  },
  template: `
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
      <section class="panel config-file">
        <details class="config-collapse">
          <summary>Panneau d'agents d'Omarchy</summary>
          <div v-for="f in files" :key="f.name">
            <header class="panel-head"><h3>{{ f.name }}</h3>
              <span class="config-actions">
                <button type="button" class="btn ghost" @click="copy(f)">{{ copied === f.name ? "Copié" : "Copier" }}</button>
              </span></header>
            <pre>{{ f.content }}</pre>
          </div>
        </details>
      </section>
    </div>
  `,
};
