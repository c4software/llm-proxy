// @ts-check
/*
  La carte d'un GRAPHIQUE produit par chart() (run_javascript) : le vrai
  graphique Chart.js, interactif — survol, légende cliquable —, rejoué
  dans la page depuis sa configuration (gardée dans le message : la carte
  revient après un rechargement). Chart.js est celui du bac
  (vendor/chart.js), chargé au premier graphique.
*/
const { ref, onMounted, onUnmounted } = Vue;

/** @type {Promise<any> | null} */
let loading = null;
function loadChart() {
  if (!loading) {
    loading = import(new URL("/ui/static/vendor/chart.js", location.href).href).then((m) => {
      m.Chart.register(...m.registerables);
      return m.Chart;
    });
    loading.catch(() => { loading = null; });
  }
  return loading;
}

// Les couleurs de la page (thème clair ou sombre) pour les axes et le texte.
function theme() {
  const css = getComputedStyle(document.documentElement);
  const v = (name, fallback) => css.getPropertyValue(name).trim() || fallback;
  return { text: v("--muted", "#666"), line: v("--line", "#ddd"), panel: v("--panel", "#fff") };
}

export const ChartCard = {
  props: ["k"],
  setup(props) {
    const root = ref(null);
    const canvas = ref(null);
    const error = ref("");
    const { isSupported: canFull, isFullscreen: full, toggle: toggleFull } = VueUse.useFullscreen(root);
    let chart = null;

    onMounted(async () => {
      try {
        const Chart = await loadChart();
        const t = theme();
        // Une copie : Chart.js écrit dans sa configuration, celle du message
        // reste telle quelle.
        const config = JSON.parse(JSON.stringify(props.k.chart));
        config.options = { ...(config.options || {}), responsive: true, maintainAspectRatio: true,
                           aspectRatio: (props.k.width || 800) / (props.k.height || 500) };
        Chart.defaults.color = t.text;
        Chart.defaults.borderColor = t.line;
        chart = new Chart(canvas.value, config);
      } catch (e) {
        error.value = "Graphique indisponible : " + e.message;
      }
    });
    onUnmounted(() => { if (chart) chart.destroy(); });

    // Le PNG du graphique tel qu'il est (séries masquées comprises), sur fond blanc.
    function png() {
      if (!chart) return;
      const src = chart.canvas;
      const out = document.createElement("canvas");
      out.width = src.width;
      out.height = src.height;
      const x = out.getContext("2d");
      x.fillStyle = "#fff";
      x.fillRect(0, 0, out.width, out.height);
      x.drawImage(src, 0, 0);
      const a = document.createElement("a");
      a.href = out.toDataURL("image/png");
      a.download = props.k.file || "graphique.png";
      a.click();
    }
    return { root, canvas, error, canFull, full, toggleFull, png };
  },
  template: `
    <figure class="tool-card chart-card" ref="root" :class="{ full }">
      <canvas ref="canvas" role="img" :aria-label="k.title"></canvas>
      <p class="dim" v-if="error">{{ error }}</p>
      <figcaption class="card-actions">
        <button type="button" class="btn ghost" @click="png">Télécharger en PNG</button>
        <button type="button" class="btn ghost" v-if="canFull" @click="toggleFull()">{{ full ? "Quitter le plein écran" : "Plein écran" }}</button>
        <span class="dim">survoler pour les valeurs ; cliquer la légende pour masquer une série</span>
      </figcaption>
    </figure>`,
};
