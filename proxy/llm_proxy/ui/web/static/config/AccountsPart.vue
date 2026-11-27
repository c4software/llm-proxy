<script>
// @ts-check
/*
  Configuration → Comptes (admin, comptes OIDC) : accepter un compte en
  attente, bloquer, promouvoir (/ui/users) ; régler la part de chacun au
  partage équitable — défaut, illimitée, ou un nombre de tokens par heure
  glissante ; borner ses modèles (motifs de noms préfixés, « albert/* » ;
  tous par défaut) ; télécharger une sauvegarde de data/.
*/
import * as Vue from "vue";
import { call } from "../lib/http.js";
import { num, stamp as when } from "../lib/format.js";
import { useAccount, useUsers } from "./shared.js";

const { reactive, onMounted } = Vue;

const ROLES = [
  { id: "pending", label: "en attente" }, { id: "user", label: "utilisateur" },
  { id: "admin", label: "admin" }, { id: "blocked", label: "bloqué" },
];

const AccountsPart = {
  props: { session: { type: Object, required: true } },
  setup(props) {
    const { account } = useAccount(props);
    const { users, userError, loadUsers } = useUsers();
    const fair = props.session.fair_share || null;
    // La part d'un compte : défaut (null), illimitée (0), ou un nombre.
    const shareMode = (u) => (u.share === null || u.share === undefined ? "default"
      : u.share === 0 ? "unlimited" : "custom");
    const customShare = reactive({});
    async function reload() {
      await loadUsers();
      for (const u of users.value) {
        if (u.share) customShare[u.id] = u.share;
        if (Array.isArray(u.models)) modelText[u.id] = u.models.join(", ");
      }
    }
    async function patch(u, body) {
      try {
        await call("PATCH", "/ui/users/" + u.id, body);
        userError.value = "";
      } catch (e) { userError.value = e.message; }
      await reload();
    }
    const setShare = (u, share) => patch(u, { share });
    function pickShare(u, mode) {
      if (mode === "default") return setShare(u, null);
      if (mode === "unlimited") return setShare(u, 0);
      customShare[u.id] = u.share || (fair && fair.default) || 100000;
      return setShare(u, customShare[u.id]);
    }
    function typeShare(u) {
      const n = Math.round(Number(customShare[u.id]));
      if (n > 0) setShare(u, n);
    }
    function setRole(u, role) { if (role !== u.role) patch(u, { role }); }
    // Les modèles permis : null = tous ; sinon des motifs, séparés par des
    // virgules ou des espaces (vide = aucun).
    // « limités » choisi ne borne rien tant qu'aucun motif n'est saisi.
    const modelText = reactive({});
    const modelMode = reactive({});
    const limited = (u) => (modelMode[u.id] || (Array.isArray(u.models) ? "some" : "all")) === "some";
    function pickModels(u, mode) {
      modelMode[u.id] = mode;
      if (mode === "all") return patch(u, { models: null });
      modelText[u.id] = (u.models || []).join(", ");
    }
    function typeModels(u) {
      const models = String(modelText[u.id] || "").split(/[\s,]+/).filter(Boolean);
      patch(u, { models });
    }
    onMounted(reload);

    return { ROLES, account, users, userError, fair, num, when, shareMode, customShare,
             pickShare, typeShare, setRole, modelText, limited, pickModels, typeModels };
  },
};

export default AccountsPart;
</script>

<template>
    <div class="config-keys">
      <section class="panel">
        <div class="panel-head"><h2>Comptes</h2></div>
        <p class="err" v-if="userError" role="status">{{ userError }}</p>
        <div class="scroll"><table>
          <thead><tr><th>Compte</th><th>Clés</th><th>Créé</th><th>Connexion</th>
            <th title="Partage équitable : tokens par heure glissante ; au-delà, les requêtes du compte attendent">Part / h</th>
            <th title="Les modèles que le compte voit et emploie : motifs de noms préfixés (albert/*) ; ses propres backends n'y sont pas soumis">Modèles</th><th>Rôle</th></tr></thead>
          <tbody>
            <tr v-for="u in users" :key="u.id">
              <td>{{ u.name || u.email }}<span class="dim" v-if="u.name && u.email"> · {{ u.email }}</span>
                <span class="tag" v-if="account && u.id === account.id">vous</span></td>
              <td>{{ u.keys }}</td>
              <td>{{ when(u.created) }}</td>
              <td>{{ when(u.last_login) }}</td>
              <td><div class="share-cell"><select :value="shareMode(u)" @change="pickShare(u, $event.target.value)"
                    :aria-label="'Part de ' + (u.email || u.name)">
                  <option value="default">défaut{{ fair && fair.default ? " (" + num(fair.default) + ")" : " (aucune limite)" }}</option>
                  <option value="unlimited">illimitée</option>
                  <option value="custom">personnalisée</option></select>
                <input v-if="shareMode(u) === 'custom'" type="number" min="1" step="1000"
                       v-model="customShare[u.id]"
                       @change="typeShare(u)" :placeholder="u.share" aria-label="Tokens par heure"></div></td>
              <td><div class="share-cell"><select :value="limited(u) ? 'some' : 'all'"
                    @change="pickModels(u, $event.target.value)" :aria-label="'Modèles de ' + (u.email || u.name)">
                  <option value="all">tous</option>
                  <option value="some">limités</option></select>
                <input v-if="limited(u)" v-model="modelText[u.id]" @change="typeModels(u)" spellcheck="false"
                       placeholder="albert/*, claude/claude-sonnet-*" aria-label="Motifs des modèles permis"></div></td>
              <td><select :value="u.role" @change="setRole(u, $event.target.value)"
                          :aria-label="'Rôle de ' + (u.email || u.name)">
                <option v-for="r in ROLES" :key="r.id" :value="r.id">{{ r.label }}</option></select></td>
            </tr>
          </tbody>
        </table></div>
      </section>
      <section class="panel backup-panel">
        <div class="panel-head"><h2>Sauvegarde</h2>
          <a class="btn" href="/ui/admin/backup" download>Télécharger une sauvegarde</a></div>
        <p class="note">Une archive de <code>data/</code>, faite à l'instant : les bases
          (statistiques, mémoire, comptes et clés), <code>client.secret</code> et
          <code>config.toml</code>. Elle contient le secret des condensés et la mémoire
          de tous : à garder comme un mot de passe. Le <code>.env</code> n'y est pas.
          Restauration : <code>python -m llm_proxy.backup --restore &lt;archive&gt;</code>,
          proxy arrêté (voir docs/deploiement.md).</p>
      </section>
    </div>
</template>
