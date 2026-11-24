/*
  Configuration → Backends : les BACKENDS du compte connecté (/ui/backends,
  backend_routes.py) — un nom (le préfixe de ses modèles), un protocole, une
  adresse, une clé, et pour qui : soi seul, ou tout le monde (avec la clé du
  créateur), et des réglages d'une table [backends.<nom>] : images,
  tokenize_path, chat_template, effort_map (« medium=high, none= », un
  niveau vide n'est pas envoyé), l'injection de tool_choice et (admin) le
  limiteur : quotas, et ses règles `limits`, une par ligne (« claude-*, qwen-big rpm=10 tpd=5000 »).
  Ses en-têtes propres (« Nom: valeur », un par ligne) : secrets comme la
  clé, la page n'en reçoit que les noms ; « Nom: » sans valeur garde
  l'ancienne. L'admin ouvre aussi le backend d'un autre compte (PUT ?all=1) :
  en-têtes, limites, quotas… sans jamais voir sa clé ni ses valeurs.
  Ses modèles exposés : « Lire les modèles » interroge son /v1/models
  (/ui/backends/{id}/models), une case par modèle, cochée si le proxy
  l'expose. Les cases touchées remplacent les motifs : tout coché, aucun
  filtre (les nouveaux modèles viennent d'eux-mêmes), sinon les noms
  cochés ; pas touchées, les motifs restent.
  Désactivé, il reste (clé comprise) sans être routé. La clé
  n'est jamais relue : vide à la modification, elle reste.
  En dessous, ceux que d'autres comptes partagent, en lecture, et pour
  l'admin ceux de tous les comptes, à retirer.
*/
import { call } from "../lib/http.js";
import { stamp as when } from "../lib/format.js";
import { useAccount } from "./shared.js";

const { ref, reactive, onMounted } = Vue;

const API_LABELS = { openai: "OpenAI (chat/completions)", anthropic: "Anthropic (Messages)" };
const blank = () => ({ id: null, name: "", api: "openai", url: "", api_key: "", shared: false,
                       images: null, tokenize_path: "", chat_template: false, effort_map: {}, effortsText: "",
                       enabled: true, tool_choice: "", quotas: false,
                       limits: [], limitsText: "", headers: [], headersText: "",
                       models: [], catalog: null, picked: [], touched: false, reading: false,
                       saved_enabled: true,
                       anyone: false, by: "" });
const KINDS = ["rpm", "tpm", "rpd", "tpd"];
// Une règle par ligne : des motifs de noms, puis des plafonds « rpm=10 ».
const rulesText = (limits) => (limits || []).map((r) => [r.models.join(", "),
  ...KINDS.filter((k) => r[k]).map((k) => `${k}=${r[k]}`)].join(" ")).join("\n");
// Les en-têtes : « Nom: valeur » par ligne ; sans valeur, l'ancienne reste.
function headersOf(text) {
  const out = {};
  for (const line of text.split("\n").map((l) => l.trim()).filter(Boolean)) {
    const at = line.indexOf(":");
    if (at <= 0) throw new Error(`en-têtes : « ${line} » (attendu : Nom: valeur)`);
    out[line.slice(0, at).trim()] = line.slice(at + 1).trim();
  }
  return out;
}
// Les niveaux de réflexion : « demandé=envoyé », séparés par des virgules ;
// « none= » : ce niveau n'est pas envoyé.
const effortsText = (map) => Object.entries(map || {}).map(([k, v]) => `${k}=${v}`).join(", ");
function effortsOf(text) {
  const out = {};
  for (const word of text.split(/[\s,]+/).filter(Boolean)) {
    const [k, v] = word.split("=");
    if (v === undefined) throw new Error(`niveaux de réflexion : « ${word} » (attendu : medium=high)`);
    out[k] = v;
  }
  return out;
}
const keepHeaders = (names) => Object.fromEntries((names || []).map((n) => [n, ""]));
function rulesOf(text) {
  return text.split("\n").map((line) => line.trim()).filter(Boolean).map((line) => {
    const rule = { models: [] };
    for (const word of line.split(/[\s,]+/).filter(Boolean)) {
      const [k, v] = word.split("=");
      if (v === undefined) rule.models.push(k);
      else if (KINDS.includes(k) && /^\d+$/.test(v)) rule[k] = Number(v);
      else throw new Error(`limites : « ${word} » (attendu : rpm=, tpm=, rpd=, tpd=)`);
    }
    return rule;
  });
}
// Un nom coché, en motif de `models` qui ne désigne que lui.
const literal = (id) => id.replace(/[*?[]/g, "[$&]");
const TOOL_CHOICES = [["", "aucune injection"], ["auto", "auto"], ["required", "required"], ["none", "none"]];
const IMAGES = [[null, "selon le protocole"], [true, "oui"], [false, "non"]];

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
    // `anyone` : l'admin ouvre le backend d'un autre compte (PUT ?all=1).
    function open(b = null, anyone = false) {
      Object.assign(form, blank(), b ? { ...b, api_key: "", saved_enabled: b.enabled, limitsText: rulesText(b.limits),
                                         effortsText: effortsText(b.effort_map),
                                         headersText: (b.headers || []).map((n) => `${n}:`).join("\n"),
                                         anyone, by: anyone ? b.by : "" } : {});
      editing.value = true;
      if (anyone) Vue.nextTick(() => document.querySelector(".backends-form")?.scrollIntoView({ block: "start" }));
    }
    const fields = (b) => ({ name: b.name, api: b.api, url: b.url, shared: b.shared,
                             images: b.images, tokenize_path: b.tokenize_path,
                             chat_template: !!b.chat_template, effort_map: b.effort_map || {},
                             enabled: b.enabled,
                             tool_choice: b.tool_choice, quotas: b.quotas, limits: b.limits || [],
                             headers: b.sentHeaders || keepHeaders(b.headers),
                             models: b.models || [],
                             ...(b.api_key ? { api_key: b.api_key } : {}) });
    // Le catalogue entier du backend enregistré, coché selon ses `models`.
    async function readModels() {
      error.value = "";
      form.reading = true;
      try {
        const d = await call("GET", "/ui/backends/" + encodeURIComponent(form.id) + "/models"
                             + (form.anyone ? "?all=1" : ""));
        form.catalog = d.models.map((m) => m.id);
        form.picked = d.models.filter((m) => m.exposed).map((m) => m.id);
        form.touched = false;
      } catch (e) { error.value = e.message; }
      form.reading = false;
    }
    // Cases touchées : tout coché = aucun filtre, sinon les noms cochés ;
    // pas touchées, les motifs restent tels quels.
    function chosen() {
      if (!form.touched) return form.models;
      if (!form.picked.length) throw new Error("modèles : cochez-en au moins un");
      return form.picked.length === form.catalog.length ? [] : form.picked.map(literal);
    }
    function pick(all) {
      form.picked = all ? [...form.catalog] : [];
      form.touched = true;
    }
    async function save() {
      error.value = "";
      try {
        const body = fields({ ...form, limits: rulesOf(form.limitsText), models: chosen(),
                              effort_map: effortsOf(form.effortsText),
                              sentHeaders: headersOf(form.headersText) });
        if (form.id) await call("PUT", "/ui/backends/" + encodeURIComponent(form.id)
                                + (form.anyone ? "?all=1" : ""), body);
        else await call("POST", "/ui/backends", body);
        editing.value = false;
        await load();
      } catch (e) { error.value = e.message; }
    }
    async function quotas(b) {
      error.value = "";
      try {
        await call("PUT", "/ui/backends/" + encodeURIComponent(b.id) + "?all=1", fields({ ...b, quotas: !b.quotas }));
        await load();
      } catch (e) { error.value = e.message; }
    }
    async function toggle(b) {
      error.value = "";
      try {
        await call("PUT", "/ui/backends/" + encodeURIComponent(b.id), fields({ ...b, enabled: !b.enabled }));
        if (form.id === b.id) form.enabled = !b.enabled;
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
    return { mine, shared, every, admin, reserved, error, form, editing, armed, open, save, toggle, quotas, remove, when,
             readModels, pick,
             API_LABELS, IMAGES, TOOL_CHOICES };
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
        <form class="item-form backends-form" v-if="editing" @submit.prevent="save">
          <p class="note" v-if="form.anyone">Admin : le backend <code>{{ form.name }}/</code> de {{ form.by }}.
            Sa clé et la valeur de ses en-têtes ne sont pas montrées ; laissées vides, elles restent.</p>
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
          <label>État
            <select v-model="form.enabled">
              <option :value="true">actif</option>
              <option :value="false">désactivé (gardé, pas routé)</option>
            </select></label>
          <label>Images <span class="dim">(pour les modèles que son catalogue dit multimodaux)</span>
            <select v-model="form.images">
              <option v-for="[v, label] in IMAGES" :key="String(v)" :value="v">{{ label }}</option>
            </select></label>
          <label>Tokenisation <span class="dim">(chemin d'un comptage exact, llama.cpp : /tokenize ; vide : estimation)</span>
            <input v-model="form.tokenize_path" maxlength="200" spellcheck="false" placeholder="/tokenize"></label>
          <label v-if="form.api !== 'anthropic'" class="check"><input type="checkbox" v-model="form.chat_template">
            Gabarit <span class="dim">(llama.cpp, vLLM : reçoit chat_template_kwargs, store, la réflexion de l'historique ;
            décoché, ils sont retirés et le niveau de réflexion passe en reasoning_effort)</span></label>
          <label v-if="form.api !== 'anthropic'">Niveaux de réflexion <span class="dim">(ceux que le backend
            accepte : « demandé=envoyé », « medium=high, xhigh=high, none= » ; vide : tels quels)</span>
            <input v-model="form.effortsText" maxlength="300" spellcheck="false"
                   placeholder="minimal=low, medium=high, xhigh=high"></label>
          <label>En-têtes <span class="dim">(un par ligne, « Nom: valeur », ajoutés à chaque requête ;
            jamais réaffichés : « Nom: » sans valeur garde l'ancienne)</span>
            <textarea v-model="form.headersText" rows="2" spellcheck="false" autocomplete="off"
                      placeholder="X-Mon-En-Tete: valeur"></textarea></label>
          <label>tool_choice <span class="dim">(injecté quand une requête a des outils sans tool_choice ; Albert : auto)</span>
            <select v-model="form.tool_choice">
              <option v-for="[v, label] in TOOL_CHOICES" :key="v" :value="v">{{ label }}</option>
            </select></label>
          <label v-if="admin || form.quotas" class="check">
            <input type="checkbox" v-model="form.quotas" :disabled="!admin && !form.quotas">
            Quotas <span class="dim">(le limiteur : règles ci-dessous, limites du compte Albert, familles de [quotas] ; admin)</span></label>
          <label v-if="form.quotas && (admin || form.limitsText)">Limites
            <span class="dim">(une règle par ligne : motifs de noms puis plafonds, « claude-*, qwen-big rpm=10 tpd=5000000 » ; admin)</span>
            <textarea v-model="form.limitsText" rows="3" spellcheck="false" :readonly="!admin"
                      placeholder="claude-* rpm=50 tpm=200000 tpd=5000000"></textarea></label>
          <fieldset class="backend-models" v-if="form.id">
            <legend>Modèles exposés <span class="dim">(les autres ne sont ni listés ni routés, pour personne)</span></legend>
            <p class="note" v-if="!form.catalog">{{ form.models.length ? form.models.join(", ") : "Tous." }}</p>
            <template v-else>
              <div class="item-actions">
                <button type="button" class="btn ghost" @click="pick(true)">Tout cocher</button>
                <button type="button" class="btn ghost" @click="pick(false)">Tout décocher</button>
                <span class="dim">{{ form.picked.length }} / {{ form.catalog.length }}
                  {{ form.picked.length === form.catalog.length ? "· les nouveaux modèles aussi" : "" }}</span></div>
              <div class="model-picks">
                <label class="check" v-for="m in form.catalog" :key="m">
                  <input type="checkbox" :value="m" v-model="form.picked" @change="form.touched = true"> <code>{{ m }}</code></label></div>
            </template>
            <div class="item-actions" v-if="!form.catalog">
              <button type="button" class="btn ghost" @click="readModels" :disabled="form.reading || !form.saved_enabled">
                {{ form.reading ? "Lecture…" : "Lire les modèles" }}</button>
              <span class="dim" v-if="!form.saved_enabled">(backend actif seulement)</span></div>
          </fieldset>
          <div class="item-actions">
            <button type="submit" class="btn">{{ form.id ? "Enregistrer" : "Créer" }}</button>
            <button type="button" class="btn ghost" @click="editing = false">Annuler</button>
          </div>
        </form>
        <div class="scroll" v-if="mine.length"><table>
          <thead><tr><th>Nom</th><th>Protocole</th><th>Adresse</th><th>Pour</th><th>État</th><th>Créé</th><th></th></tr></thead>
          <tbody>
            <tr v-for="b in mine" :key="b.id" :class="{ dim: !b.enabled }">
              <td><code>{{ b.name }}/</code></td>
              <td>{{ API_LABELS[b.api] || b.api }}</td>
              <td><code>{{ b.url }}</code></td>
              <td>{{ b.shared ? "tout le monde" : "moi seul" }}</td>
              <td>{{ b.enabled ? "actif" : "désactivé" }}{{ b.quotas ? " · quotas" : "" }}</td>
              <td>{{ when(b.created) }}</td>
              <td><div class="row-actions">
                <button type="button" class="btn ghost" @click="open(b)">Modifier</button>
                <button type="button" class="btn ghost" @click="toggle(b)">{{ b.enabled ? "Désactiver" : "Activer" }}</button>
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
          <thead><tr><th>Nom</th><th>Adresse</th><th>Pour</th><th>État</th><th>Compte</th><th></th></tr></thead>
          <tbody>
            <tr v-for="b in every" :key="b.id" :class="{ dim: !b.enabled }">
              <td><code>{{ b.name }}/</code></td><td><code>{{ b.url }}</code></td>
              <td>{{ b.shared ? "tout le monde" : "lui seul" }}</td>
              <td>{{ b.enabled ? "actif" : "désactivé" }}{{ b.quotas ? " · quotas" : "" }}</td><td>{{ b.by }}</td>
              <td><div class="row-actions">
                <button type="button" class="btn ghost" @click="open(b, true)">Modifier</button>
                <button type="button" class="btn ghost" @click="quotas(b)">{{ b.quotas ? "Sans quotas" : "Quotas" }}</button>
                <button type="button" class="btn danger" :class="{ armed: armed === b.id }"
                        @click="remove(b, true)">{{ armed === b.id ? "Supprimer ?" : "Supprimer" }}</button></div></td>
            </tr>
          </tbody>
        </table></div>
      </section>
    </div>
  `,
};
