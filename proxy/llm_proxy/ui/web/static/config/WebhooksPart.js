/*
  Configuration → Webhooks : ceux du compte connecté (/ui/webhooks,
  webhook_routes.py).

    entrants  une adresse secrète (POST /hooks/<jeton>, format Slack) :
              son modèle (fixé, ou demandé par le corps si permis), son
              prompt système facultatif, ses outils hébergés, sa
              conversation (une nouvelle à chaque message, toujours la
              même, ou aucune : le passe plat) et le sortant où part la
              réponse. L'adresse se réaffiche (bouton Adresse : relue au
              proxy, GET …/token).
    sortants  une URL Apprise (le salon y est), et s'il est
              exposé à l'outil `webhook` du chat (décoché par défaut) ;
              sinon il ne sert qu'aux réponses des entrants. L'URL
              s'affiche en entier (à son compte seulement), se copie, et
              préremplit le formulaire.
*/
import { call } from "../lib/http.js";
import { stamp as when } from "../lib/format.js";
import { useCopy } from "../lib/files.js";
import { useCatalog } from "./shared.js";

const { ref, reactive, computed, onMounted } = Vue;

const blankIn = () => ({ id: null, name: "", model: "", model_override: false, system: "",
                         tools: [], target: "new", conversation: "", reply: "" });

export const WebhooksPart = {
  props: { session: { type: Object, required: true } },
  setup(props) {
    const { modelChoices } = useCatalog();
    const { copied, copy } = useCopy();
    const incoming = ref([]);
    const outgoing = ref([]);
    const path = ref("/hooks/");
    const error = ref("");
    const form = reactive(blankIn());
    const editing = ref(false);         // le formulaire d'un entrant est ouvert
    const fresh = ref(null);            // { name, url } : l'adresse affichée (création, Adresse)
    const outForm = reactive({ id: null, name: "", url: "", was: "", tool: false });
    const outEditing = ref(false);
    const armed = ref(null);            // suppression : deuxième clic attendu
    const testing = ref(null);
    const tested = ref({});             // id → « envoyé » ou l'erreur
    const convs = ref([]);              // les conversations du compte (le choix « toujours la même »)

    const toolGroups = computed(() => ((props.session.chat || {}).tools || [])
      .map((g) => ({ type: g.type, label: g.type.startsWith("mcp:") ? "MCP " + g.type.slice(4) : g.tools.join(", ") })));
    const outName = (id) => (outgoing.value.find((o) => o.id === id) || {}).name || "";
    const convTitle = (id) => (convs.value.find((c) => c.id === id) || {}).title || id;

    async function load() {
      try {
        const d = await call("GET", "/ui/webhooks");
        incoming.value = d.incoming || [];
        outgoing.value = d.outgoing || [];
        path.value = d.path || "/hooks/";
        error.value = "";
      } catch (e) { error.value = e.message; }
    }
    async function loadConvs() {
      try {
        const d = await call("GET", "/ui/conversations?summary=1&limit=200");
        convs.value = d.data || [];
      } catch { convs.value = []; }
    }
    const urlOf = (token) => location.origin + path.value + token;

    // ── entrants ─────────────────────────────────────────────────────
    function openIn(h = null) {
      Object.assign(form, blankIn(), h ? { ...h, tools: [...(h.tools || [])] } : {});
      editing.value = true;
      loadConvs();
    }
    function toggleTool(t) {
      const i = form.tools.indexOf(t);
      if (i < 0) form.tools.push(t); else form.tools.splice(i, 1);
    }
    async function saveIn() {
      error.value = "";
      const body = { name: form.name, model: form.model, model_override: form.model_override,
                     system: form.system, tools: form.tools, target: form.target,
                     conversation: form.target === "conversation" ? form.conversation : "",
                     reply: form.reply };
      try {
        if (form.id) await call("PUT", "/ui/webhooks/in/" + encodeURIComponent(form.id), body);
        else {
          const r = await call("POST", "/ui/webhooks/in", body);
          fresh.value = { name: r.webhook.name, url: urlOf(r.token) };
        }
        editing.value = false;
        await load();
      } catch (e) { error.value = e.message; }
    }
    async function rotate(h) {
      if (armed.value !== "rotate:" + h.id) { arm("rotate:" + h.id); return; }
      armed.value = null;
      try {
        const r = await call("POST", "/ui/webhooks/in/" + encodeURIComponent(h.id) + "/rotate", {});
        fresh.value = { name: r.webhook.name, url: urlOf(r.token) };
        await load();
      } catch (e) { error.value = e.message; }
    }
    function arm(k) {
      armed.value = k;
      setTimeout(() => { if (armed.value === k) armed.value = null; }, 3000);
    }
    async function remove(kind, h) {
      if (armed.value !== kind + ":" + h.id) { arm(kind + ":" + h.id); return; }
      armed.value = null;
      try {
        await call("DELETE", `/ui/webhooks/${kind}/` + encodeURIComponent(h.id));
        await load();
      } catch (e) { error.value = e.message; }
    }
    // L'adresse d'un entrant, relue au proxy (elle ne voyage pas avec la
    // liste) : le panneau de la création, avec Copier et l'exemple curl.
    async function showLink(h) {
      error.value = "";
      try {
        const r = await call("GET", "/ui/webhooks/in/" + encodeURIComponent(h.id) + "/token");
        fresh.value = { name: h.name, url: urlOf(r.token) };
        window.scrollTo({ top: 0, behavior: "smooth" });
      } catch (e) { error.value = e.message; }
    }
    const curl = (url) => `curl -X POST -H 'Content-Type: application/json' \\\n  -d '{"text": "Bonjour !"}' ${url}`;

    // ── sortants ─────────────────────────────────────────────────────
    function openOut(o = null) {
      Object.assign(outForm, { id: o ? o.id : null, name: o ? o.name : "", url: o ? o.url : "",
                               was: o ? o.url : "", tool: o ? !!o.tool : false });
      outEditing.value = true;
    }
    async function saveOut() {
      error.value = "";
      try {
        // L'URL n'est renvoyée que changée : redonnée, elle effacerait le
        // salon d'un sortant JSON.
        const body = { name: outForm.name, tool: outForm.tool,
                       ...(outForm.url !== outForm.was ? { url: outForm.url } : {}) };
        if (outForm.id) await call("PUT", "/ui/webhooks/out/" + encodeURIComponent(outForm.id), body);
        else await call("POST", "/ui/webhooks/out", body);
        outEditing.value = false;
        await load();
      } catch (e) { error.value = e.message; }
    }
    async function test(o) {
      testing.value = o.id;
      try {
        await call("POST", "/ui/webhooks/out/" + encodeURIComponent(o.id) + "/test", {});
        tested.value = { ...tested.value, [o.id]: "envoyé" };
      } catch (e) { tested.value = { ...tested.value, [o.id]: e.message }; }
      finally { testing.value = null; await load(); }
    }

    onMounted(() => { load(); loadConvs(); });

    return { incoming, outgoing, error, form, editing, fresh, outForm, outEditing, armed, testing, tested,
             convs, toolGroups, outName, convTitle, modelChoices, openIn, toggleTool, saveIn, rotate, remove,
             openOut, saveOut, test, curl, copied, copy, showLink, when };
  },
  template: `
    <div class="config-keys owned">
      <p class="err" v-if="error" role="status">{{ error }}</p>

      <section class="panel key-fresh" v-if="fresh">
        <div class="panel-head"><h2>Adresse du webhook « {{ fresh.name }} »</h2>
          <button type="button" class="btn ghost" @click="fresh = null">Fermer</button></div>
        <p class="note">Elle vaut droit d'écrire dans votre historique et de dépenser vos tokens : à garder
          comme une clé. Le bouton Adresse du webhook la réaffiche ; Régénérer en donne une autre et ferme celle-ci.</p>
        <div class="key-copy">
          <code>{{ fresh.url }}</code>
          <button type="button" class="btn ghost" @click="copy({ name: 'hook', content: fresh.url })">{{ copied === "hook" ? "Copié" : "Copier" }}</button>
        </div>
        <p class="note">Le corps est celui d'un webhook entrant de Slack :</p>
        <pre>{{ curl(fresh.url) }}</pre>
      </section>

      <section class="panel">
        <div class="panel-head"><h2>Webhooks entrants</h2>
          <button type="button" class="btn" v-if="!editing" @click="openIn()">Nouveau</button></div>
        <p class="note">Un message reçu (<code>{"text": "…"}</code>, comme Slack) va dans une conversation
          du chat, et le modèle y répond, à votre nom. La réponse part vers le webhook sortant choisi.</p>
        <form class="item-form" v-if="editing" @submit.prevent="saveIn">
          <label>Nom <input v-model="form.name" maxlength="80" required placeholder="Alertes, Zabbix, formulaire…"></label>
          <label>Modèle
            <select v-model="form.model">
              <option value="">— celui de la conversation, sinon le modèle par défaut —</option>
              <option v-for="m in modelChoices(form.model)" :key="m" :value="m">{{ m }}</option>
            </select></label>
          <label class="check"><input type="checkbox" v-model="form.model_override">
            le message peut en demander un autre (champ <code>model</code> du corps)</label>
          <label>Prompt système <span class="dim">(facultatif : à la place de celui de la conversation)</span>
            <textarea v-model="form.system" rows="4" maxlength="20000"
                      placeholder="Tu reçois des alertes de supervision. Résume en une phrase et dis s'il faut agir."></textarea></label>
          <fieldset v-if="toolGroups.length"><legend>Outils hébergés</legend>
            <label class="check" v-for="g in toolGroups" :key="g.type">
              <input type="checkbox" :checked="form.tools.includes(g.type)" @change="toggleTool(g.type)">
              {{ g.type }} <span class="dim">{{ g.label }}</span></label>
            <p class="note">Aucun coché : ceux de la conversation. Ceux de la page (navigateur, appareil)
              n'existent pas ici.</p>
          </fieldset>
          <fieldset><legend>Conversation</legend>
            <label class="check"><input type="radio" value="new" v-model="form.target">
              une nouvelle à chaque message</label>
            <label class="check"><input type="radio" value="conversation" v-model="form.target">
              toujours la même</label>
            <label class="check" :title="form.reply ? '' : 'Choisir d’abord un webhook sortant de réponse'">
              <input type="radio" value="none" v-model="form.target" :disabled="!form.reply">
              aucune, passe plat <span class="dim">(rien n'est gardé : la réponse va seulement au sortant)</span></label>
            <p class="note">Le modèle ne reçoit que le message reçu (et le prompt système), jamais
              l'historique de la conversation.<template v-if="form.target === 'none'"> Passe plat : rien n'est
              gardé (incognito) — ni l'outil mémoire, et un fichier produit n'est lisible que 15 min.</template></p>
            <select v-if="form.target === 'conversation'" v-model="form.conversation" aria-label="Conversation">
              <option value="">— une conversation dédiée, créée au premier message —</option>
              <option v-if="form.conversation && !convs.some((c) => c.id === form.conversation)" :value="form.conversation">{{ form.conversation }}</option>
              <option v-for="c in convs" :key="c.id" :value="c.id">{{ c.title }}</option>
            </select>
          </fieldset>
          <label>Réponse envoyée à
            <select v-model="form.reply">
              <option value="">— aucun webhook sortant (la conversation seulement) —</option>
              <option v-for="o in outgoing" :key="o.id" :value="o.id">{{ o.name }}</option>
            </select></label>
          <div class="item-actions">
            <button type="submit" class="btn">{{ form.id ? "Enregistrer" : "Créer" }}</button>
            <button type="button" class="btn ghost" @click="editing = false">Annuler</button>
          </div>
        </form>
        <table v-if="incoming.length">
          <thead><tr><th>Nom</th><th>Adresse</th><th>Modèle</th><th>Conversation</th><th>Réponse</th>
            <th>Dernier message</th><th></th></tr></thead>
          <tbody>
            <tr v-for="h in incoming" :key="h.id">
              <td>{{ h.name }}<span class="tag" v-if="h.system" title="Prompt système du webhook">prompt</span></td>
              <td><code title="début du jeton">…/hooks/wh-{{ h.hint }}…</code></td>
              <td>{{ h.model || "—" }}<span class="dim" v-if="h.model_override"> (ou celui du message)</span></td>
              <td>{{ h.target === "none" ? "aucune (passe plat)" : h.target === "new" ? "nouvelle à chaque fois" : h.conversation ? convTitle(h.conversation) : "dédiée (créée au premier message)" }}</td>
              <td>{{ h.reply ? outName(h.reply) : "—" }}</td>
              <td>{{ when(h.last_used) }}<span class="dim" v-if="h.calls"> · {{ h.calls }}</span>
                <div class="err" v-if="h.last_error">{{ h.last_error }}</div></td>
              <td><div class="row-actions">
                <button type="button" class="btn ghost" @click="showLink(h)"
                        :title="h.copyable ? 'Afficher l’adresse du webhook, à copier' : 'Créé avant que l’adresse soit gardée : le régénérer pour pouvoir l’afficher'"
                        >Adresse</button>
                <button type="button" class="btn ghost" @click="openIn(h)">Modifier</button>
                <button type="button" class="btn ghost" :class="{ armed: armed === 'rotate:' + h.id }"
                        @click="rotate(h)">{{ armed === "rotate:" + h.id ? "Régénérer ?" : "Régénérer" }}</button>
                <button type="button" class="btn danger" :class="{ armed: armed === 'in:' + h.id }"
                        @click="remove('in', h)">{{ armed === "in:" + h.id ? "Supprimer ?" : "Supprimer" }}</button></div></td>
            </tr>
          </tbody>
        </table>
        <p class="note" v-else-if="!editing">Aucun webhook entrant.</p>
      </section>

      <section class="panel">
        <div class="panel-head"><h2>Webhooks sortants</h2>
          <button type="button" class="btn" v-if="!outEditing" @click="openOut()">Nouveau</button></div>
        <p class="note">Une URL <a href="https://github.com/caronc/apprise/wiki" target="_blank" rel="noopener">Apprise</a>
          (Slack, Mattermost, Discord, Teams, Telegram, ntfy, e-mail…) : le proxy y envoie la réponse d'un webhook
          entrant, et ce que le modèle envoie par l'outil « Webhooks » du chat, quand vous le lui demandez.</p>
        <form class="item-form" v-if="outEditing" @submit.prevent="saveOut">
          <label>Nom <span class="dim">(c'est par lui que le modèle le désigne)</span>
            <input v-model="outForm.name" maxlength="80" required placeholder="Équipe, Ops…"></label>
          <label>URL
            <input v-model="outForm.url" maxlength="2000" required spellcheck="false"
                   placeholder="slack://T…/B…/X…/#alertes, mmost://chat.exemple.fr/clé, discord://…">
            <span class="dim">Le salon fait partie de l'URL quand le service le permet. L'URL d'un webhook
              (<code>https://hooks.slack.com/…</code>, <code>https://discord.com/api/webhooks/…</code>) est aussi
              reconnue ; une autre URL <code>https://</code> reçoit <code>{"text": "…"}</code>.</span></label>
          <label class="check"><input type="checkbox" v-model="outForm.tool">
            exposé à l'outil « Webhooks » du chat <span class="dim">(sinon : réponses des webhooks entrants seulement)</span></label>
          <div class="item-actions">
            <button type="submit" class="btn">{{ outForm.id ? "Enregistrer" : "Créer" }}</button>
            <button type="button" class="btn ghost" @click="outEditing = false">Annuler</button>
          </div>
        </form>
        <table v-if="outgoing.length">
          <thead><tr><th>Nom</th><th>Service</th><th>URL</th><th>Dernier envoi</th><th></th></tr></thead>
          <tbody>
            <tr v-for="o in outgoing" :key="o.id">
              <td>{{ o.name }}<span class="tag" v-if="o.tool" title="Le modèle peut y poster par l'outil Webhooks">outil</span></td>
              <td>{{ o.service }}<span class="dim" v-if="o.channel"> · {{ o.channel }}</span>
                <span class="tag" v-if="o.legacy" title="Envoi JSON compatible Slack, hors Apprise : ressaisir l'URL en URL Apprise">à ressaisir</span></td>
              <td class="out-url"><code>{{ o.url }}</code></td>
              <td>{{ when(o.last_used) }}<span class="dim" v-if="o.calls"> · {{ o.calls }}</span>
                <div class="err" v-if="o.last_error">{{ o.last_error }}</div>
                <div class="dim" v-else-if="tested[o.id]">essai : {{ tested[o.id] }}</div></td>
              <td><div class="row-actions">
                <button type="button" class="btn ghost" @click="copy({ name: 'out:' + o.id, content: o.url })">{{ copied === 'out:' + o.id ? "Copié" : "Copier" }}</button>
                <button type="button" class="btn ghost" :disabled="testing === o.id" @click="test(o)">Essayer</button>
                <button type="button" class="btn ghost" @click="openOut(o)">Modifier</button>
                <button type="button" class="btn danger" :class="{ armed: armed === 'out:' + o.id }"
                        @click="remove('out', o)">{{ armed === "out:" + o.id ? "Supprimer ?" : "Supprimer" }}</button></div></td>
            </tr>
          </tbody>
        </table>
        <p class="note" v-else-if="!outEditing">Aucun webhook sortant.</p>
      </section>
    </div>
  `,
};
