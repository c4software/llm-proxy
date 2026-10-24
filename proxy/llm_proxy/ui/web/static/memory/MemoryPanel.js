/*
  La section « Mémoire » : ce que l'outil `memory` a gardé pour la clé
  CONNECTÉE (celle du cookie de la page), à lire, corriger, renommer ou
  effacer. Enregistrée par app.js sous <memory-panel>.

  Routes (même cookie que /ui/usage) :
    GET    /ui/memory               la liste et l'occupation
    GET    /ui/memory/file?path=    un fichier
    PUT    /ui/memory/file          {path, content, expires} : crée ou remplace
    DELETE /ui/memory/file?path=    un fichier
    POST   /ui/memory/rename        {old_path, new_path}
  Mémoire désactivée sur le proxy (404) : la section ne s'affiche pas.

  Durée de vie : un fichier est persistant, ou disparaît à son échéance
  (`expires`, en secondes ; choisie par le modèle, ou ici). La liste la
  montre, l'éditeur la change : « Persistant » ou une date.

  Rien ne se rafraîchit tout seul : une modification en cours ne doit
  jamais être écrasée par la liste. Elle se relit à l'ouverture, après
  chaque action, et par « Actualiser ».

  Deux présentations : la partie « Mémoire » de la configuration
  (`expanded` : ouverte d'emblée), et le panneau latéral du chat
  (`compact`), où `stamp` change quand le modèle vient d'appeler l'outil
  `memory` : la liste se relit, et le fichier ouvert aussi s'il n'est pas
  en cours de modification.
*/
import { call } from "../lib/http.js";
import { num, stamp as when } from "../lib/format.js";

export const MemoryPanel = {
  props: { compact: { type: Boolean, default: false },
           expanded: { type: Boolean, default: false },
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
    const until = ref("");           // échéance (datetime-local), "" = persistant
    const originalUntil = ref("");
    const status = ref("");
    const error = ref(false);
    const confirming = ref(false);
    const busy = ref(false);

    const dirty = computed(() => content.value !== original.value
      || until.value !== originalUntil.value
      || (selected.value !== null && path.value !== selected.value));
    // Le choix « Persistant » / « Jusqu'au » : passer à une date propose
    // dans une semaine.
    const lifetime = computed({
      get: () => until.value ? "until" : "persistent",
      set: (v) => { until.value = v === "until" ? local(Date.now() / 1000 + 7 * 86400) : ""; },
    });
    const renaming = computed(() => selected.value !== null
      && path.value.trim() !== selected.value);

    // Secondes ↔ la valeur d'un <input type="datetime-local"> (heure locale).
    function local(ts) {
      if (!ts) return "";
      const d = new Date(ts * 1000);
      return new Date(d - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
    }
    const seconds = (v) => v ? new Date(v).getTime() / 1000 : null;

    function say(text, isError = false) {
      status.value = text;
      error.value = isError;
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
        until.value = originalUntil.value = local(j.expires);
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
      until.value = originalUntil.value = "";
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
        const w = await call("PUT", "/ui/memory/file", { path: target, content: content.value,
                                                          expires: seconds(until.value) });
        selected.value = w.path;
        path.value = w.path;
        original.value = content.value;
        until.value = originalUntil.value = local(w.expires);
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

    const short = (p) => p.replace(/^\/memories\//, "");

    onMounted(load);
    watch(() => props.stamp, async () => {
      await load();
      if (selected.value !== null && !dirty.value) open(selected.value);
    });
    return { enabled, files, usage, selected, path, content, until, lifetime,
             status, error, confirming, busy, dirty, renaming, open, fresh,
             save, remove, load, onToggle, num, when, short };
  },
  template: `
  <details class="panel howto memory" v-if="enabled && !compact" :open="expanded" @toggle="onToggle">
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
              <span class="dim expiry" v-if="f.expires">expire le {{ when(f.expires) }}</span>
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
        <div class="field">
          <span>Durée de vie</span>
          <div class="memory-lifetime">
            <select v-model="lifetime" aria-label="Durée de vie">
              <option value="persistent">Persistant</option>
              <option value="until">Jusqu'au</option>
            </select>
            <input v-if="until" type="datetime-local" v-model="until" required
                   aria-label="Échéance">
          </div>
        </div>
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
              <span class="dim expiry" v-if="f.expires">expire le {{ when(f.expires) }}</span>
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
        <div class="field">
          <span>Durée de vie</span>
          <div class="memory-lifetime">
            <select v-model="lifetime" aria-label="Durée de vie">
              <option value="persistent">Persistant</option>
              <option value="until">Jusqu'au</option>
            </select>
            <input v-if="until" type="datetime-local" v-model="until" required
                   aria-label="Échéance">
          </div>
        </div>
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
