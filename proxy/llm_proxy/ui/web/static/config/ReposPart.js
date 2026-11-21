/*
  Configuration → Dépôts : les SOURCES du compte connecté (/ui/repos,
  repo_routes.py), que lit l'outil `repository` (arborescence, fichier,
  recherche ; rien n'est exécuté) — une forge (GitLab, GitHub, Gitea/Forgejo, ou git
  en HTTPS), son adresse, un jeton, les projets permis. Le jeton n'est
  jamais relu : vide à la modification, il reste. En dessous, les sources
  partagées par l'admin ([repos.sources.<nom>]), en lecture.
*/
import { call } from "../lib/http.js";
import { stamp as when } from "../lib/format.js";

const { ref, reactive, onMounted } = Vue;

const FORGE_LABELS = { gitlab: "GitLab", github: "GitHub", gitea: "Gitea / Forgejo", git: "git (HTTPS)" };
const PLACEHOLDERS = { gitlab: "https://gitlab.exemple.fr", github: "https://github.com",
                       gitea: "https://gitea.exemple.fr", git: "https://git.exemple.fr" };
const blank = () => ({ id: null, name: "", forge: "gitlab", url: "", token: "", username: "", projects: "" });

export const ReposPart = {
  setup() {
    const sources = ref([]);
    const shared = ref([]);
    const maxBytes = ref(0);
    const error = ref("");
    const form = reactive(blank());
    const editing = ref(false);
    const armed = ref(null);

    async function load() {
      try {
        const d = await call("GET", "/ui/repos");
        sources.value = d.sources || [];
        shared.value = d.shared || [];
        maxBytes.value = d.max_bytes || 0;
        error.value = "";
      } catch (e) { error.value = e.message; }
    }
    function open(s = null) {
      Object.assign(form, blank(), s ? { ...s, token: "", projects: (s.projects || []).join(", ") } : {});
      editing.value = true;
    }
    async function save() {
      error.value = "";
      const body = { name: form.name, forge: form.forge, url: form.url, username: form.username,
                     projects: form.projects, ...(form.token ? { token: form.token } : {}) };
      try {
        if (form.id) await call("PUT", "/ui/repos/" + encodeURIComponent(form.id), body);
        else await call("POST", "/ui/repos", body);
        editing.value = false;
        await load();
      } catch (e) { error.value = e.message; }
    }
    async function remove(s) {
      if (armed.value !== s.id) {
        armed.value = s.id;
        setTimeout(() => { if (armed.value === s.id) armed.value = null; }, 3000);
        return;
      }
      armed.value = null;
      try {
        await call("DELETE", "/ui/repos/" + encodeURIComponent(s.id));
        await load();
      } catch (e) { error.value = e.message; }
    }
    const mb = (n) => (n / 1e6).toLocaleString("fr-FR", { maximumFractionDigits: 0 }) + " Mo";

    onMounted(load);
    return { sources, shared, maxBytes, error, form, editing, armed, open, save, remove, when, mb,
             FORGE_LABELS, PLACEHOLDERS };
  },
  template: `
    <div class="config-keys owned">
      <p class="err" v-if="error" role="status">{{ error }}</p>
      <section class="panel">
        <div class="panel-head"><h2>Sources de dépôts</h2>
          <button type="button" class="btn" v-if="!editing" @click="open()">Nouvelle</button></div>
        <p class="note">L'outil « Dépôts » du chat (et des webhooks) lit un projet d'une de ces sources :
          son arborescence, ses fichiers, une recherche — rien n'est exécuté. Le proxy le télécharge
          avec votre jeton (ni le modèle ni le bac ne le voient), un bac isolé le décompresse et le lit. Un jeton en lecture seule
          suffit<template v-if="maxBytes"> ; archive de {{ mb(maxBytes) }} au plus</template>.</p>
        <form class="item-form" v-if="editing" @submit.prevent="save">
          <label>Nom <span class="dim">(c'est par lui que le modèle la désigne)</span>
            <input v-model="form.name" maxlength="80" required placeholder="PMW, GitHub perso…"></label>
          <label>Forge
            <select v-model="form.forge">
              <option v-for="(label, f) in FORGE_LABELS" :key="f" :value="f">{{ label }}</option>
            </select></label>
          <label>Adresse de la forge
            <input v-model="form.url" type="url" maxlength="2000" spellcheck="false"
                   :required="form.forge !== 'github'" :placeholder="PLACEHOLDERS[form.forge]"></label>
          <label>Jeton <span class="dim" v-if="form.id">(vide : inchangé)</span>
            <input v-model="form.token" type="password" maxlength="1000" autocomplete="off" spellcheck="false"
                   :required="!form.id && form.forge !== 'git'"
                   :placeholder="form.forge === 'github' ? 'jeton d’accès (fine-grained, Contents: read)'
                     : form.forge === 'gitlab' ? 'jeton d’accès (read_repository, read_api)' : 'jeton d’accès'"></label>
          <label v-if="form.forge === 'git'">Utilisateur du jeton <span class="dim">(défaut : git ; oauth2 pour
            GitLab, x-access-token pour GitHub)</span>
            <input v-model="form.username" maxlength="100" spellcheck="false"></label>
          <label>Projets permis <span class="dim">(facultatif : motifs séparés par des virgules ; vide = tous
            ceux que le jeton voit)</span>
            <input v-model="form.projects" spellcheck="false" placeholder="playmoweb/*, kid-ai/*"></label>
          <div class="item-actions">
            <button type="submit" class="btn">{{ form.id ? "Enregistrer" : "Créer" }}</button>
            <button type="button" class="btn ghost" @click="editing = false">Annuler</button>
          </div>
        </form>
        <table v-if="sources.length">
          <thead><tr><th>Nom</th><th>Forge</th><th>Adresse</th><th>Projets</th><th>Dernier téléchargement</th><th></th></tr></thead>
          <tbody>
            <tr v-for="s in sources" :key="s.id">
              <td>{{ s.name }}</td>
              <td>{{ FORGE_LABELS[s.forge] || s.forge }}</td>
              <td><code>{{ s.url }}</code></td>
              <td>{{ s.projects.length ? s.projects.join(", ") : "tous" }}</td>
              <td>{{ when(s.last_used) }}<div class="err" v-if="s.last_error">{{ s.last_error }}</div></td>
              <td><div class="row-actions">
                <button type="button" class="btn ghost" @click="open(s)">Modifier</button>
                <button type="button" class="btn danger" :class="{ armed: armed === s.id }"
                        @click="remove(s)">{{ armed === s.id ? "Supprimer ?" : "Supprimer" }}</button></div></td>
            </tr>
          </tbody>
        </table>
        <p class="note" v-else-if="!editing">Aucune source à vous.</p>
      </section>
      <section class="panel" v-if="shared.length">
        <div class="panel-head"><h2>Sources partagées</h2></div>
        <p class="note">Déclarées par l'admin, offertes à tous. Une source à vous du même nom la remplace.</p>
        <table>
          <thead><tr><th>Nom</th><th>Forge</th><th>Adresse</th><th>Projets</th></tr></thead>
          <tbody>
            <tr v-for="s in shared" :key="s.id">
              <td>{{ s.name }}</td><td>{{ FORGE_LABELS[s.forge] || s.forge }}</td>
              <td><code>{{ s.url }}</code></td>
              <td>{{ s.projects.length ? s.projects.join(", ") : "tous" }}</td>
            </tr>
          </tbody>
        </table>
      </section>
    </div>
  `,
};
