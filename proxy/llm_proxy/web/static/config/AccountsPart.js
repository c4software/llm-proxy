/*
  Configuration → Comptes (admin, comptes OIDC) : accepter un compte en
  attente, bloquer, promouvoir (/ui/users) ; régler la part de chacun au
  partage équitable — défaut, illimitée, ou un nombre de tokens par heure
  glissante ; télécharger une sauvegarde de data/.
*/
import { call } from "../lib/http.js";
import { num, stamp as when } from "../lib/format.js";
import { useAccount, useUsers } from "./shared.js";

const { reactive, onMounted } = Vue;

const ROLES = [
  { id: "pending", label: "en attente" }, { id: "user", label: "utilisateur" },
  { id: "admin", label: "admin" }, { id: "blocked", label: "bloqué" },
];

export const AccountsPart = {
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
      for (const u of users.value) if (u.share) customShare[u.id] = u.share;
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
    onMounted(reload);

    return { ROLES, account, users, userError, fair, num, when, shareMode, customShare,
             pickShare, typeShare, setRole };
  },
  template: `
    <div class="config-keys">
      <section class="panel">
        <div class="panel-head"><h2>Comptes</h2></div>
        <p class="err" v-if="userError" role="status">{{ userError }}</p>
        <table>
          <thead><tr><th>Compte</th><th>Clés</th><th>Créé</th><th>Connexion</th>
            <th title="Partage équitable : tokens par heure glissante ; au-delà, les requêtes du compte attendent">Part / h</th><th>Rôle</th></tr></thead>
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
              <td><select :value="u.role" @change="setRole(u, $event.target.value)"
                          :aria-label="'Rôle de ' + (u.email || u.name)">
                <option v-for="r in ROLES" :key="r.id" :value="r.id">{{ r.label }}</option></select></td>
            </tr>
          </tbody>
        </table>
      </section>
      <section class="panel backup-panel">
        <div class="panel-head"><h2>Sauvegarde</h2>
          <a class="btn" href="/ui/admin/backup" download>Télécharger une sauvegarde</a></div>
        <p class="note">Une archive de <code>data/</code>, faite à l'instant : les bases
          (statistiques, mémoire, comptes et clés), <code>client.secret</code> et
          <code>config.toml</code>. Elle contient le secret des condensés et la mémoire
          de tous : à garder comme un mot de passe. Le <code>.env</code> n'y est pas.
          Restauration : <code>python -m llm_proxy.backup --restore &lt;archive&gt;</code>,
          proxy arrêté (voir le README).</p>
      </section>
    </div>

  `,
};
