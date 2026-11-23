/*
  Configuration → Backends : les BACKENDS du compte connecté (/ui/backends,
  backend_routes.py) — un nom (le préfixe de ses modèles), un protocole, une
  adresse, une clé, et pour qui : soi seul, ou tout le monde (avec la clé du
  créateur). La clé n'est jamais relue : vide à la modification, elle reste.
  En dessous, ceux que d'autres comptes partagent, en lecture, et pour
  l'admin ceux de tous les comptes, à retirer.
*/
import { call } from "../lib/http.js";
import { stamp as when } from "../lib/format.js";
import { useAccount } from "./shared.js";

const { ref, reactive, onMounted } = Vue;

const API_LABELS = { openai: "OpenAI (chat/completions)", anthropic: "Anthropic (Messages)" };
const blank = () => ({ id: null, name: "", api: "openai", url: "", api_key: "", shared: false });

export const BackendsPart = {
  props: { session: { type: Object, required: true } },
  setup(props) {
    const { account, isAdmin } = useAccount(props);
    const admin = !account.value || isAdmin.value;
    const mine = ref([]);
    const shared = ref([]);
    const every = ref([]);
    const reserved = ref([]);
    const error = ref("");
    const form = reactive(blank());
    const editing = ref(false);
    const armed = ref(null);

    async function load() {
      try {
        const d = await call("GET", "/ui/backends" + (admin ? "?all=1" : ""));
        mine.value = d.backends || [];
        shared.value = d.shared || [];
        every.value = d.all || [];
        reserved.value = d.reserved || [];
        error.value = "";
      } catch (e) { error.value = e.message; }
    }
    function open(b = null) {
      Object.assign(form, blank(), b ? { ...b, api_key: "" } : {});
      editing.value = true;
    }
    async function save() {
      error.value = "";
      const body = { name: form.name, api: form.api, url: form.url, shared: form.shared,
                     ...(form.api_key ? { api_key: form.api_key } : {}) };
      try {
        if (form.id) await call("PUT", "/ui/backends/" + encodeURIComponent(form.id), body);
        else await call("POST", "/ui/backends", body);
        editing.value = false;
        await load();
      } catch (e) { error.value = e.message; }
    }
    async function remove(b, anyone = false) {
      if (armed.value !== b.id) {
        armed.value = b.id;
        setTimeout(() => { if (armed.value === b.id) armed.value = null; }, 3000);
        return;
      }
      armed.value = null;
      try {
        await call("DELETE", "/ui/backends/" + encodeURIComponent(b.id) + (anyone ? "?all=1" : ""));
        await load();
      } catch (e) { error.value = e.message; }
    }

    onMounted(load);
    return { mine, shared, every, admin, reserved, error, form, editing, armed, open, save, remove, when, API_LABELS };
  },
  template: `
    <div class="config-keys owned">
      <p class="err" v-if="error" role="status">{{ error }}</p>
      <section class="panel">
        <div class="panel-head"><h2>Mes backends</h2>
          <div class="item-actions">
            <button type="button" class="btn" v-if="!editing" @click="open()">Nouveau</button></div></div>
        <p class="note">Un serveur de modèles à vous, avec votre clé. Ses modèles s'appellent
          <code>&lt;nom&gt;/&lt;modèle&gt;</code>, dans le chat comme avec vos clés.
          Partagé avec tout le monde, il sert à tous les comptes avec votre clé, qui
          ne leur est jamais montrée.<template v-if="reserved.length"> Noms pris par le
          proxy : {{ reserved.join(", ") }}.</template></p>
        <form class="item-form" v-if="editing" @submit.prevent="save">
          <label>Nom <span class="dim">(le préfixe des modèles : a-z, 0-9, « . _ - »)</span>
            <input v-model="form.name" maxlength="40" required spellcheck="false"
                   pattern="[a-z0-9][a-z0-9._\\-]*" placeholder="perso"></label>
          <label>Protocole
            <select v-model="form.api">
              <option v-for="(label, a) in API_LABELS" :key="a" :value="a">{{ label }}</option>
            </select></label>
          <label>Adresse <span class="dim">(sans /v1)</span>
            <input v-model="form.url" type="url" maxlength="2000" required spellcheck="false"
                   :placeholder="form.api === 'anthropic' ? 'https://api.anthropic.com' : 'https://api.exemple.fr'"></label>
          <label>Clé <span class="dim">{{ form.id ? "(vide : inchangée, sauf si l'adresse change d'hôte)" : "(facultative)" }}</span>
            <input v-model="form.api_key" type="password" maxlength="4000" autocomplete="off" spellcheck="false"></label>
          <label>Pour
            <select v-model="form.shared">
              <option :value="false">moi seul</option>
              <option :value="true">tout le monde (avec ma clé)</option>
            </select></label>
          <div class="item-actions">
            <button type="submit" class="btn">{{ form.id ? "Enregistrer" : "Créer" }}</button>
            <button type="button" class="btn ghost" @click="editing = false">Annuler</button>
          </div>
        </form>
        <div class="scroll" v-if="mine.length"><table>
          <thead><tr><th>Nom</th><th>Protocole</th><th>Adresse</th><th>Pour</th><th>Créé</th><th></th></tr></thead>
          <tbody>
            <tr v-for="b in mine" :key="b.id">
              <td><code>{{ b.name }}/</code></td>
              <td>{{ API_LABELS[b.api] || b.api }}</td>
              <td><code>{{ b.url }}</code></td>
              <td>{{ b.shared ? "tout le monde" : "moi seul" }}</td>
              <td>{{ when(b.created) }}</td>
              <td><div class="row-actions">
                <button type="button" class="btn ghost" @click="open(b)">Modifier</button>
                <button type="button" class="btn danger" :class="{ armed: armed === b.id }"
                        @click="remove(b)">{{ armed === b.id ? "Supprimer ?" : "Supprimer" }}</button></div></td>
            </tr>
          </tbody>
        </table></div>
        <p class="note" v-else-if="!editing">Aucun backend à vous.</p>
      </section>
      <section class="panel" v-if="shared.length">
        <div class="panel-head"><h2>Backends partagés</h2></div>
        <p class="note">Partagés par d'autres comptes, avec leur clé. Vos requêtes vers ces modèles
          partent vers leur serveur. Un backend à vous du même nom le remplace.</p>
        <div class="scroll"><table>
          <thead><tr><th>Nom</th><th>Protocole</th><th>Adresse</th><th>Partagé par</th></tr></thead>
          <tbody>
            <tr v-for="b in shared" :key="b.id">
              <td><code>{{ b.name }}/</code></td><td>{{ API_LABELS[b.api] || b.api }}</td>
              <td><code>{{ b.url }}</code></td><td>{{ b.by }}</td>
            </tr>
          </tbody>
        </table></div>
      </section>
      <section class="panel" v-if="admin && every.length">
        <div class="panel-head"><h2>Backends de tous les comptes</h2></div>
        <p class="note">Admin : ceux de chaque compte, à soi ou partagés. Un compte bloqué perd les siens.</p>
        <div class="scroll"><table>
          <thead><tr><th>Nom</th><th>Adresse</th><th>Pour</th><th>Compte</th><th></th></tr></thead>
          <tbody>
            <tr v-for="b in every" :key="b.id">
              <td><code>{{ b.name }}/</code></td><td><code>{{ b.url }}</code></td>
              <td>{{ b.shared ? "tout le monde" : "lui seul" }}</td><td>{{ b.by }}</td>
              <td><button type="button" class="btn danger" :class="{ armed: armed === b.id }"
                          @click="remove(b, true)">{{ armed === b.id ? "Supprimer ?" : "Supprimer" }}</button></td>
            </tr>
          </tbody>
        </table></div>
      </section>
    </div>
  `,
};
