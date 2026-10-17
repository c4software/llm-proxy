/*
  Configuration → Clés (comptes OIDC) : chacun crée ses clés (des uuid4,
  /ui/keys) pour ses clients — montrées UNE fois, à la création, avec la
  commande qui la pose sur le poste — et les révoque. Un admin voit aussi
  celles de tous, et rattache une clé du TOML à un compte. En tête : la
  part du partage équitable et sa consommation.
*/
import { call } from "../lib/http.js";
import { num, stamp as when } from "../lib/format.js";
import { useCopy } from "../lib/files.js";
import { KEYFILE, KEY_ENV, useAccount, useUsers } from "./shared.js";

const { ref, watch, onMounted } = Vue;

export const KeysPart = {
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
    async function createKey() {
      keyError.value = "";
      try {
        fresh.value = await call("POST", "/ui/keys", { label: keyLabel.value });
        keyLabel.value = "";
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
    onMounted(loadKeys);

    return { KEYFILE, KEY_ENV, isAdmin, keys, tomlKeys, allKeys, keyLabel, fresh, keyError,
             armed, createKey, revokeKey, bindToml, users, fair, num, when, copied, copy };
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
        <table v-if="keys.length || tomlKeys.length">
          <thead><tr><th>Libellé</th><th v-if="allKeys">Compte</th><th>Clé</th>
            <th>Créée</th><th>Dernier usage</th><th></th></tr></thead>
          <tbody>
            <tr v-for="k in keys" :key="k.id" :class="{ dim: k.revoked }">
              <td>{{ k.label || "—" }}</td>
              <td v-if="allKeys">{{ k.user }}</td>
              <td><code>{{ k.hint }}…</code></td>
              <td>{{ when(k.created) }}</td>
              <td>{{ when(k.last_used) }}</td>
              <td><span v-if="k.revoked" class="tag">révoquée</span>
                <button v-else type="button" class="btn danger" :class="{ armed: armed === k.id }"
                        @click="revokeKey(k)">{{ armed === k.id ? "Révoquer ?" : "Révoquer" }}</button></td>
            </tr>
            <tr v-for="k in tomlKeys" :key="k.api_key_id">
              <td>proxy.api_keys <span class="tag" title="Écrite dans config.toml : se retire là">TOML</span></td>
              <td v-if="allKeys"><select :value="k.user_id ?? ''" @change="bindToml(k, $event.target.value)"
                  aria-label="Compte de cette clé du TOML"
                  title="Rattachée, la clé écrit dans la mémoire du compte et suit son état ; ce qu'elle avait rangé seule ne bouge pas">
                <option value="">— aucun compte —</option>
                <option v-for="u in users" :key="u.id" :value="u.id">{{ u.email || u.name }}</option></select></td>
              <td><code title="condensé (la clé n'est pas connue de la page)">#{{ k.hint }}</code></td>
              <td>—</td><td>—</td><td></td>
            </tr>
          </tbody>
        </table>
        <p class="note" v-else>Aucune clé pour l'instant.</p>
      </section>
    </div>

  `,
};
