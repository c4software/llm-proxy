/*
  La section « Mémoire » du tableau de bord : ce que l'outil `memory` a
  gardé pour la clé CONNECTÉE (celle du cookie de la page), à lire,
  corriger, renommer ou effacer. Un composant à part, enregistré par
  dashboard.js sous <memory-panel> : il a ses propres appels et ne touche
  pas aux statistiques.

  Routes (même cookie que /ui/usage) :
    GET    /ui/memory               la liste et l'occupation
    GET    /ui/memory/file?path=    un fichier
    PUT    /ui/memory/file          {path, content} : crée ou remplace
    DELETE /ui/memory/file?path=    un fichier
    POST   /ui/memory/rename        {old_path, new_path}
  Mémoire désactivée sur le proxy (404) : la section ne s'affiche pas.

  Rien ne se rafraîchit tout seul : une modification en cours ne doit
  jamais être écrasée par la liste. Elle se relit à l'ouverture de la
  section, après chaque action, et par « Actualiser ».

  Deux présentations, le même contenu : la section repliable des
  statistiques, et (`compact`) le panneau latéral du chat. Là, `stamp`
  change quand le modèle vient d'appeler l'outil `memory` : la liste se
  relit, et le fichier ouvert aussi s'il n'est pas en cours de
  modification.
*/
window.MemoryPanel = {
  props: { compact: { type: Boolean, default: false },
           stamp: { type: Number, default: 0 } },
  emits: ["close"],
  setup(props) {
    const { ref, computed, onMounted, watch } = Vue;
    const enabled = ref(null);       // null : pas encore su
    const files = ref([]);
    const usage = ref(null);
    const selected = ref(null);      // chemin du fichier ouvert, null = nouveau
    const path = ref("/memories/");
    const content = ref("");
    const original = ref("");        // contenu tel que lu, pour « modifié »
    const status = ref("");
    const error = ref(false);
    const confirming = ref(false);
    const busy = ref(false);

    const dirty = computed(() => content.value !== original.value
      || (selected.value !== null && path.value !== selected.value));
    const renaming = computed(() => selected.value !== null
      && path.value.trim() !== selected.value);

    function say(text, isError = false) {
      status.value = text;
      error.value = isError;
    }

    async function call(method, url, body) {
      const r = await fetch(url, {
        method,
        headers: { accept: "application/json",
                   ...(body ? { "content-type": "application/json" } : {}) },
        body: body ? JSON.stringify(body) : undefined,
      });
      let json = null;
      try { json = await r.json(); } catch { /* corps vide */ }
      if (!r.ok) {
        const err = new Error((json && json.error && json.error.message) || `HTTP ${r.status}`);
        err.status = r.status;
        throw err;
      }
      return json;
    }

    async function load() {
      try {
        const j = await call("GET", "/ui/memory");
        files.value = j.data || [];
        usage.value = j.usage || null;
        enabled.value = true;
      } catch (e) {
        if (e.status === 404) enabled.value = false;
        else say(`Lecture impossible : ${e.message}`, true);
      }
    }

    async function open(p) {
      if (busy.value) return;
      try {
        const j = await call("GET", "/ui/memory/file?path=" + encodeURIComponent(p));
        selected.value = j.path;
        path.value = j.path;
        content.value = original.value = j.content;
        confirming.value = false;
        say("");
      } catch (e) {
        say(`Ouverture impossible : ${e.message}`, true);
        load();
      }
    }

    function fresh() {
      selected.value = null;
      path.value = "/memories/";
      content.value = original.value = "";
      confirming.value = false;
      say("");
    }

    async function save() {
      busy.value = true;
      try {
        let target = path.value.trim();
        if (renaming.value) {
          const r = await call("POST", "/ui/memory/rename",
                               { old_path: selected.value, new_path: target });
          target = r.path;
        }
        const w = await call("PUT", "/ui/memory/file", { path: target, content: content.value });
        selected.value = w.path;
        path.value = w.path;
        original.value = content.value;
        say(w.created ? "Fichier créé." : "Enregistré.");
        await load();
      } catch (e) {
        say(e.message, true);
      } finally {
        busy.value = false;
      }
    }

    async function remove() {
      if (!confirming.value) { confirming.value = true; return; }
      busy.value = true;
      try {
        await call("DELETE", "/ui/memory/file?path=" + encodeURIComponent(selected.value));
        const gone = selected.value;
        fresh();
        say(`${gone} supprimé.`);
        await load();
      } catch (e) {
        say(e.message, true);
      } finally {
        busy.value = false;
        confirming.value = false;
      }
    }

    function onToggle(e) { if (e.target.open) load(); }

    const num = (n) => Number(n || 0).toLocaleString("fr-FR");
    const when = (ts) => new Date(ts * 1000).toLocaleString("fr-FR",
      { dateStyle: "short", timeStyle: "short" });
    const short = (p) => p.replace(/^\/memories\//, "");

    onMounted(load);
    watch(() => props.stamp, async () => {
      await load();
      if (selected.value !== null && !dirty.value) open(selected.value);
    });
    return { enabled, files, usage, selected, path, content, status, error,
             confirming, busy, dirty, renaming, open, fresh, save, remove,
             load, onToggle, num, when, short };
  },
  template: `
  <details class="panel howto memory" v-if="enabled && !compact" @toggle="onToggle">
    <summary>
      <h2>Mémoire</h2>
      <span class="meta">ce que les modèles ont retenu pour vous
        <template v-if="usage"> · {{ num(usage.files) }} fichier{{ usage.files > 1 ? "s" : "" }},
          {{ num(usage.chars) }} car. sur {{ num(usage.max_chars) }}</template></span>
    </summary>
    <div class="memory-body">
      <aside class="memory-list">
        <div class="memory-actions">
          <button type="button" class="btn" @click="fresh">Nouveau fichier</button>
          <button type="button" class="btn ghost" @click="load">Actualiser</button>
        </div>
        <ul v-if="files.length">
          <li v-for="f in files" :key="f.path">
            <button type="button" :aria-current="String(f.path === selected)"
                    @click="open(f.path)">
              <span class="name">{{ short(f.path) }}</span>
              <span class="dim">{{ num(f.chars) }} car. · {{ when(f.updated) }}</span>
            </button>
          </li>
        </ul>
        <p v-else class="note">Rien n'est encore retenu. Un modèle y écrit par
          l'outil <code>memory</code> (interrupteur « Mémoire » du chat, serveur
          MCP <code>/mcp</code>, ou <code>/v1/tools/memory</code>) ; on peut
          aussi créer un fichier ici.</p>
      </aside>
      <section class="memory-editor">
        <label class="field">
          <span>Chemin</span>
          <input v-model="path" spellcheck="false" placeholder="/memories/notes.md">
        </label>
        <label class="field grow">
          <span>Contenu</span>
          <textarea v-model="content" spellcheck="false"
                    placeholder="Texte libre, en général du Markdown."></textarea>
        </label>
        <div class="memory-actions">
          <button type="button" class="btn" :disabled="busy || (!dirty && selected !== null)"
                  @click="save">{{ renaming ? "Renommer et enregistrer" : "Enregistrer" }}</button>
          <button type="button" class="btn danger" v-if="selected !== null"
                  :disabled="busy" @click="remove">{{ confirming ? "Confirmer la suppression" : "Supprimer" }}</button>
          <span v-if="dirty" class="dim">modifié</span>
          <span class="status" :class="{ err: error }" role="status">{{ status }}</span>
        </div>
      </section>
    </div>
  </details>
  <section class="memory memory-side" v-else-if="enabled && compact">
    <header class="chat-head">
      <h2>Mémoire</h2>
      <span class="dim" v-if="usage">{{ num(usage.files) }} fichier{{ usage.files > 1 ? "s" : "" }}</span>
      <button type="button" class="icon" title="Fermer" @click="$emit('close')">×</button>
    </header>
    <div class="memory-body">
      <aside class="memory-list">
        <div class="memory-actions">
          <button type="button" class="btn" @click="fresh">Nouveau fichier</button>
          <button type="button" class="btn ghost" @click="load">Actualiser</button>
        </div>
        <ul v-if="files.length">
          <li v-for="f in files" :key="f.path">
            <button type="button" :aria-current="String(f.path === selected)"
                    @click="open(f.path)">
              <span class="name">{{ short(f.path) }}</span>
              <span class="dim">{{ num(f.chars) }} car. · {{ when(f.updated) }}</span>
            </button>
          </li>
        </ul>
        <p v-else class="note">Rien n'est encore retenu. Un modèle y écrit par
          l'outil <code>memory</code> (interrupteur « Mémoire » du chat, serveur
          MCP <code>/mcp</code>, ou <code>/v1/tools/memory</code>) ; on peut
          aussi créer un fichier ici.</p>
      </aside>
      <section class="memory-editor">
        <label class="field">
          <span>Chemin</span>
          <input v-model="path" spellcheck="false" placeholder="/memories/notes.md">
        </label>
        <label class="field grow">
          <span>Contenu</span>
          <textarea v-model="content" spellcheck="false"
                    placeholder="Texte libre, en général du Markdown."></textarea>
        </label>
        <div class="memory-actions">
          <button type="button" class="btn" :disabled="busy || (!dirty && selected !== null)"
                  @click="save">{{ renaming ? "Renommer et enregistrer" : "Enregistrer" }}</button>
          <button type="button" class="btn danger" v-if="selected !== null"
                  :disabled="busy" @click="remove">{{ confirming ? "Confirmer la suppression" : "Supprimer" }}</button>
          <span v-if="dirty" class="dim">modifié</span>
          <span class="status" :class="{ err: error }" role="status">{{ status }}</span>
        </div>
      </section>
    </div>
  </section>`,
};
