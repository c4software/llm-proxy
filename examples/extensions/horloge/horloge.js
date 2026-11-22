// L'exécution, dans la page, des outils du NAVIGATEUR de l'extension
// `horloge` (horloge.py les déclare) : le proxy sert ce module, la page
// l'importe et appelle sa fonction par défaut avec `ext` (PageTool,
// defineFamily, failure…). Elle rend {tools, families?} : un outil par nom
// déclaré ; une famille seulement pour un comportement (available,
// enabled, state) — sinon, celle que déclare le .py suffit.
export default function (ext) {
  class LocalTime extends ext.PageTool {
    constructor() { super({ name: "local_time" }); }

    run() {
      const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
      return `${new Date().toLocaleString("fr-FR", { dateStyle: "full", timeStyle: "medium" })} (${zone})`;
    }
  }
  return { tools: [new LocalTime()] };
}
