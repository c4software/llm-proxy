/*
  Configuration → Clés (comptes OIDC) : chacun crée ses clés (des uuid4,
  /ui/keys) pour ses clients — montrées UNE fois, à la création, avec la
  commande qui la pose sur le poste — et les révoque. Chaque clé a ses
  DROITS sur les outils hébergés (`tools` : null = tous, sinon la liste
  permise ; un serveur MCP entier par `mcp:<serveur>`), choisis à la
  création et modifiables ensuite (PATCH /ui/keys/<id>). Un admin voit
  aussi celles de tous, et rattache une clé du TOML à un compte. En tête :
  la part du partage équitable et sa consommation.
*/
import { call } from "../lib/http.js";
import { num, stamp as when } from "../lib/format.js";
import { useCopy } from "../lib/files.js";
import { KEYFILE, KEY_ENV, useAccount, useUsers } from "./shared.js";

const { ref, computed, watch, onMounted } = Vue;

// Le nom lisible des outils natifs ; un outil inconnu ici garde son nom.
const TOOL_LABELS = {
  web_search: "Recherche web", web_fetch: "Lecture de pages web",
  web_grep: "Recherche dans une page", ocr: "OCR (images, PDF)",
  transcribe: "Transcription audio", code_execution: "Exécution de code",
  image_generation: "Génération d'images", memory: "Mémoire persistante",
  webhook: "Webhooks sortants", repository: "Dépôts de code",
  pull_request: "Pull requests",
};

// La liste complète des droits d'une clé (l'info-bulle de la colonne).
export function rightsSummary(rights) {
  if (rights == null) return "tous";
  if (!rights.length) return "aucun";
  return rights.map((r) => TOOL_LABELS[r] || (r === "mcp" ? "tous les MCP" : r.replace(/^mcp:/, "MCP "))).join(", ");
}

// Ce qu'en dit la colonne « Outils » du tableau : court, la liste en
// info-bulle (rightsSummary).
export function rightsShort(rights) {
  if (rights == null || !rights.length || rights.length === 1) return rightsSummary(rights);
  return `${rights.length} outils`;
}

/*
  Le choix des droits : « tous les outils » (null), ou une case par outil
  natif et par serveur MCP — cochée, le serveur couvre aussi les outils
  qu'il découvrira ; sinon ses outils un par un. v-model : null | [noms].
  Un droit déjà donné mais que le proxy ne connaît plus reste affiché.
*/
const ToolRights = {
  props: { modelValue: { type: Array, default: null }, catalog: { type: Array, default: () => [] } },
  emits: ["update:modelValue"],
  setup(props, { emit }) {
    // La dernière valeur émise : deux cases changées avant que le parent
    // ne rende la nouvelle ne s'écrasent pas.
    const value = ref(props.modelValue);
    watch(() => props.modelValue, (v) => { value.value = v; });
    function set(v) { value.value = v; emit("update:modelValue", v); }
    const all = computed(() => value.value == null);
    const chosen = computed(() => new Set(value.value || []));
    const natives = computed(() => props.catalog.filter((t) => !t.server));
    const servers = computed(() => props.catalog.filter((t) => t.server));
    const unknown = computed(() => {
      const known = new Set(props.catalog.flatMap((t) => [t.name, ...(t.tools || []).map((x) => x.name)]));
      return (value.value || []).filter((r) => !known.has(r) && r !== "mcp");
    });
    function setAll(on) {
      // Passer à « choisis » part de ce qui est actif : rien ne se perd
      // en silence, on décoche ce qu'on retire.
      set(on ? null
        : props.catalog.filter((t) => t.server || t.enabled).map((t) => t.name));
    }
    function toggle(name, on) {
      const next = new Set(chosen.value);
      on ? next.add(name) : next.delete(name);
      set([...next].sort());
    }
    return { all, chosen, natives, servers, unknown, setAll, toggle, label: (n) => TOOL_LABELS[n] || n };
  },
  template: `
    <fieldset class="key-rights">
      <legend>Outils hébergés permis à cette clé</legend>
      <div class="key-rights-mode">
        <label class="check"><input type="radio" :checked="all" @change="setAll(true)"> tous</label>
        <label class="check"><input type="radio" :checked="!all" @change="setAll(false)"> seulement ceux-ci</label>
      </div>
      <div class="key-rights-list" v-if="!all">
        <label class="check" v-for="t in natives" :key="t.name" :class="{ dim: !t.enabled }"
               :title="t.enabled ? t.name : t.name + ' : inactif sur ce proxy'">
          <input type="checkbox" :checked="chosen.has(t.name)" @change="toggle(t.name, $event.target.checked)">
          {{ label(t.name) }}<span class="tag" v-if="!t.enabled">inactif</span></label>
        <div class="key-rights-server" v-for="s in servers" :key="s.name">
          <label class="check" title="Tous les outils du serveur, ceux qu'il découvrira compris">
            <input type="checkbox" :checked="chosen.has(s.name)" @change="toggle(s.name, $event.target.checked)">
            MCP <b>{{ s.server }}</b> (tout le serveur)</label>
          <label class="check sub" v-for="x in s.tools" :key="x.name">
            <input type="checkbox" :disabled="chosen.has(s.name)"
                   :checked="chosen.has(s.name) || chosen.has(x.name)" @change="toggle(x.name, $event.target.checked)">
            <code>{{ x.name }}</code></label>
        </div>
        <label class="check" v-for="r in unknown" :key="r" title="Plus connu du proxy">
          <input type="checkbox" checked @change="toggle(r, $event.target.checked)"> <code>{{ r }}</code>
          <span class="tag">inconnu</span></label>
        <p class="note" v-if="!chosen.size">Aucun outil : la clé ne sert qu'au modèle seul.</p>
      </div>
    </fieldset>
  `,
};

export const KeysPart = {
  components: { ToolRights },
  props: { session: { type: Object, required: true } },
  emits: ["goto"],
  setup(props) {
    const { account, isAdmin } = useAccount(props);
    const { users, loadUsers } = useUsers();
    const { copied, copy } = useCopy();
    const keys = ref([]);
    const tomlKeys = ref([]);
    const allKeys = ref(false);         // admin : les clés de tous
    const keyLabel = ref("");
    const fresh = ref(null);            // la clé tout juste créée, en clair
    const keyError = ref("");
    const armed = ref(null);            // révocation : deuxième clic attendu
    const keyTools = ref(null);         // droits de la clé à créer (null = tous)
    const catalog = ref([]);            // ce qu'on peut permettre (/ui/keys/tools)
    const editing = ref(null);          // { id, tools } : droits en cours de modification
    // Le partage équitable : la part du compte connecté et sa consommation
    // sur l'heure glissante (relues avec les clés).
    const fair = ref(props.session.fair_share || null);

    async function loadKeys() {
      if (!account.value) return;
      call("GET", "/ui/session").then((s) => { fair.value = s.fair_share || null; })
        .catch(() => {});
      try {
        const d = await call("GET", "/ui/keys" + (allKeys.value ? "?all=1" : ""));
        keys.value = d.data || [];
        tomlKeys.value = d.toml || [];
        keyError.value = "";
      } catch (e) { keyError.value = e.message; }
    }
    async function loadCatalog() {
      try { catalog.value = (await call("GET", "/ui/keys/tools")).data || []; }
      catch (e) { keyError.value = e.message; }
    }
    async function createKey() {
      keyError.value = "";
      try {
        fresh.value = await call("POST", "/ui/keys", { label: keyLabel.value, tools: keyTools.value });
        keyLabel.value = "";
        keyTools.value = null;
        await loadKeys();
      } catch (e) { keyError.value = e.message; }
    }
    function editRights(k) {
      editing.value = editing.value && editing.value.id === k.id ? null
        : { id: k.id, label: k.label, tools: k.tools == null ? null : [...k.tools] };
    }
    async function saveRights() {
      try {
        await call("PATCH", "/ui/keys/" + encodeURIComponent(editing.value.id), { tools: editing.value.tools });
        editing.value = null;
        keyError.value = "";
        await loadKeys();
      } catch (e) { keyError.value = e.message; }
    }
    async function revokeKey(k) {
      if (armed.value !== k.id) {
        armed.value = k.id;
        setTimeout(() => { if (armed.value === k.id) armed.value = null; }, 3000);
        return;
      }
      armed.value = null;
      try {
        await call("DELETE", "/ui/keys/" + encodeURIComponent(k.id));
        if (fresh.value && fresh.value.id === k.id) fresh.value = null;
        await loadKeys();
      } catch (e) { keyError.value = e.message; }
    }
    // Une clé du TOML rattachée à un compte (admin) : elle écrit dans sa
    // mémoire et suit son état. "" = détachée.
    async function bindToml(k, value) {
      try {
        await call("PUT", "/ui/keys/toml/" + encodeURIComponent(k.api_key_id),
          { user_id: value === "" ? null : Number(value) });
        keyError.value = "";
      } catch (e) { keyError.value = e.message; }
      await loadKeys();
    }
    watch(allKeys, (on) => { loadKeys(); if (on) loadUsers(); });
    onMounted(() => { loadKeys(); loadCatalog(); });

    return { KEYFILE, KEY_ENV, isAdmin, keys, tomlKeys, allKeys, keyLabel, fresh, keyError,
             armed, createKey, revokeKey, bindToml, users, fair, num, when, copied, copy,
             keyTools, catalog, editing, editRights, saveRights, rightsSummary, rightsShort };
  },
  template: `
    <div class="config-keys">
      <p class="panel note fair-note" v-if="fair">Partage équitable :
        <template v-if="fair.share">votre part est de <b>{{ num(fair.share) }}</b> tokens
          par heure glissante ; vous en avez utilisé <b>{{ num(fair.used) }}</b>.
          Au-delà, vos requêtes attendent que la part se libère.</template>
        <template v-else>votre part est <b>illimitée</b> ({{ num(fair.used) }} tokens
          sur la dernière heure).</template></p>
      <form class="panel key-new" @submit.prevent="createKey">
        <div class="panel-head"><h2>Nouvelle clé</h2></div>
        <div class="key-new-row">
          <input v-model="keyLabel" maxlength="80" spellcheck="false"
                 placeholder="libellé : le client qui l'utilisera (Claude Code, portable…)">
          <button type="submit" class="btn">Créer</button>
        </div>
        <tool-rights v-model="keyTools" :catalog="catalog"></tool-rights>
        <p class="err" v-if="keyError" role="status">{{ keyError }}</p>
      </form>
      <section class="panel key-fresh" v-if="fresh">
        <div class="panel-head"><h2>Clé créée{{ fresh.label ? " : " + fresh.label : "" }}</h2>
          <button type="button" class="btn ghost" @click="fresh = null">Fermer</button></div>
        <p class="note">Copiez-la maintenant : le proxy n'en garde qu'un condensé, elle ne
          sera plus affichée. Posez-la dans la variable de votre client
          (onglet <button type="button" class="link" @click="$emit('goto', 'clients')">Clients</button>).</p>
        <div class="key-copy">
          <code>{{ fresh.key }}</code>
          <button type="button" class="btn ghost" @click="copy({ name: fresh.id, content: fresh.key })">{{ copied === fresh.id ? "Copié" : "Copier" }}</button>
        </div>
        <pre>(umask 077; printf '%s' '{{ fresh.key }}' > {{ KEYFILE }})</pre>
        <p class="note">Le profil du shell la lit (onglet Clients) :</p>
        <pre>export {{ KEY_ENV }}="$(cat {{ KEYFILE }} 2>/dev/null || echo unused)"</pre>
      </section>
      <section class="panel">
        <div class="panel-head"><h2>{{ allKeys ? "Toutes les clés" : "Vos clés" }}</h2>
          <label class="check" v-if="isAdmin"><input type="checkbox" v-model="allKeys"> tous les comptes</label></div>
        <div class="scroll" v-if="keys.length || tomlKeys.length"><table>
          <thead><tr><th>Libellé</th><th v-if="allKeys">Compte</th><th>Clé</th>
            <th>Outils</th><th>Créée</th><th>Dernier usage</th><th></th></tr></thead>
          <tbody>
            <template v-for="k in keys" :key="k.id">
            <tr :class="{ dim: k.revoked }">
              <td>{{ k.label || "—" }}</td>
              <td v-if="allKeys">{{ k.user }}</td>
              <td><code>{{ k.hint }}…</code></td>
              <td class="key-tools-cell" :title="rightsSummary(k.tools)">{{ rightsShort(k.tools) }}</td>
              <td>{{ when(k.created) }}</td>
              <td>{{ when(k.last_used) }}</td>
              <td><span v-if="k.revoked" class="tag">révoquée</span>
                <span v-else class="row-actions">
                  <button type="button" class="btn ghost" @click="editRights(k)"
                          :aria-expanded="editing && editing.id === k.id ? 'true' : 'false'">Droits</button>
                  <button type="button" class="btn danger" :class="{ armed: armed === k.id }"
                          @click="revokeKey(k)">{{ armed === k.id ? "Révoquer ?" : "Révoquer" }}</button></span></td>
            </tr>
            <tr v-if="editing && editing.id === k.id" class="key-edit">
              <td :colspan="allKeys ? 7 : 6">
                <form @submit.prevent="saveRights">
                  <tool-rights v-model="editing.tools" :catalog="catalog"></tool-rights>
                  <div class="item-actions">
                    <button type="submit" class="btn">Enregistrer</button>
                    <button type="button" class="btn ghost" @click="editing = null">Annuler</button>
                  </div>
                  <p class="note">Pris en compte à la requête suivante de la clé.</p>
                </form></td>
            </tr>
            </template>
            <tr v-for="k in tomlKeys" :key="k.api_key_id">
              <td>proxy.api_keys <span class="tag" title="Écrite dans config.toml : se retire là">TOML</span></td>
              <td v-if="allKeys"><select :value="k.user_id ?? ''" @change="bindToml(k, $event.target.value)"
                  aria-label="Compte de cette clé du TOML"
                  title="Rattachée, la clé écrit dans la mémoire du compte et suit son état ; ce qu'elle avait rangé seule ne bouge pas">
                <option value="">— aucun compte —</option>
                <option v-for="u in users" :key="u.id" :value="u.id">{{ u.email || u.name }}</option></select></td>
              <td><code title="condensé (la clé n'est pas connue de la page)">#{{ k.hint }}</code></td>
              <td>tous</td><td>—</td><td>—</td><td></td>
            </tr>
          </tbody>
        </table></div>
        <p class="note" v-else>Aucune clé pour l'instant.</p>
      </section>
    </div>

  `,
};
