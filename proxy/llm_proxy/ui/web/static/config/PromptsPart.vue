<script>
// @ts-check
/*
  Configuration → Prompts (/ui/prompts, prompt_routes.py ; lue par un
  admin, ou la page d'un proxy sans comptes ; modifiée — écrite dans
  config.toml — par un admin OIDC seulement, `writable`) : les textes que le modèle lit — descriptions des outils
  et de leurs paramètres, consignes —, par catégorie (`categories`, dans
  l'ordre du proxy). Par défaut, les seules catégories ESSENTIELLES (la
  prose qui règle le chat) ; « tous les textes » montre le reste. La
  recherche et « modifiés seulement » portent sur tous les textes : une
  modification faite à la main n'est jamais cachée. Chacun se modifie et
  se rétablit ici ; effet à la requête suivante (les outils du navigateur :
  au rechargement de la page du chat). Un texte modifié n'utilise que les variables de son
  défaut, que le proxy remplit (une borne, le nom d'un outil).
*/
import * as Vue from "vue";
import { call } from "../lib/http.js";

const { ref, computed, onMounted } = Vue;

const PromptsPart = {
  setup() {
    const data = ref(null);
    const error = ref("");
    const query = ref("");
    const onlyChanged = ref(false);
    const showAll = ref(false);
    const open = ref("");             // la clé en cours d'édition
    const draft = ref("");
    const busy = ref(false);

    async function load() {
      try {
        data.value = await call("GET", "/ui/prompts");
        error.value = "";
      } catch (e) { error.value = e.message; }
    }
    const groups = computed(() => {
      if (!data.value) return [];
      const q = query.value.trim().toLowerCase();
      const all = showAll.value || onlyChanged.value || q;
      // Le texte en cours d'édition reste affiché, même si le filtre change.
      const keep = (p) => p.key === open.value || ((all || p.essential) && (!onlyChanged.value || p.override !== null)
        && (!q || p.key.toLowerCase().includes(q) || (p.override ?? p.default).toLowerCase().includes(q)));
      return data.value.categories.map(({ label }) => ({
        label, prompts: data.value.prompts.filter((p) => p.category === label && keep(p)) }))
        .filter((g) => g.prompts.length);
    });
    function edit(p) {
      open.value = p.key;
      draft.value = p.override ?? p.default;
      error.value = "";
    }
    function replace(entry) {
      const list = data.value.prompts;
      list.splice(list.findIndex((p) => p.key === entry.key), 1, entry);
    }
    async function save(p) {
      busy.value = true;
      error.value = "";
      try {
        replace(await call("PUT", "/ui/prompts/" + encodeURIComponent(p.key), { text: draft.value }));
        open.value = "";
      } catch (e) { error.value = e.message; }
      finally { busy.value = false; }
    }
    async function reset(p) {
      busy.value = true;
      error.value = "";
      try {
        replace(await call("DELETE", "/ui/prompts/" + encodeURIComponent(p.key)));
        if (open.value === p.key) draft.value = p.default;
      } catch (e) { error.value = e.message; }
      finally { busy.value = false; }
    }
    const changedCount = computed(() => (data.value ? data.value.prompts.filter((p) => p.override !== null).length : 0));

    onMounted(load);
    return { data, error, query, onlyChanged, showAll, open, draft, busy, groups, changedCount, edit, save, reset,
             cancel: () => { open.value = ""; } };
  },
};

export default PromptsPart;
</script>

<template>
    <div class="config-keys owned">
      <p class="err" v-if="error" role="status">{{ error }}</p>
      <section class="panel" v-if="data">
        <div class="panel-head"><h2>Prompts</h2>
          <div class="item-actions"><span class="tag" v-if="changedCount">{{ changedCount }} modifié(s)</span></div></div>
        <p class="note">Les textes que le modèle lit, par catégorie. Par défaut, ceux qui règlent le chat
          (résumé, état du moment, consignes) ; « tous les textes » ajoute les descriptions des outils et de
          leurs paramètres, à ne modifier qu'en connaissance de cause : le modèle s'en sert pour appeler les
          outils. La recherche et « modifiés seulement » portent sur tous les textes. Une modification vaut
          pour tout le proxy, dès la requête suivante (les outils du navigateur : après rechargement de la
          page du chat).
          Les <code>{variables}</code> sont remplies par le proxy ; <code v-text="'{{'"></code> et
          <code v-text="'}}'"></code> écrivent une accolade.</p>
        <p class="note" v-if="!data.writable">Sans comptes OIDC, la page n'écrit pas config.toml : un texte
          se modifie à la main dans sa table <code>[prompts]</code> (clé = texte), puis « Recharger »
          (Configuration → Proxy).</p>
        <div class="item-form">
          <input type="search" v-model="query" placeholder="Chercher une clé ou un texte" aria-label="Chercher">
          <label class="check"><input type="checkbox" v-model="onlyChanged"> modifiés seulement</label>
          <label class="check"><input type="checkbox" v-model="showAll"> tous les textes</label>
        </div>
        <div class="scroll" v-for="g in groups" :key="g.label"><table>
          <thead><tr><th>{{ g.label }}</th><th>Texte</th><th></th></tr></thead>
          <tbody>
            <tr v-for="p in g.prompts" :key="p.key">
              <td><code>{{ p.key }}</code> <span class="tag" v-if="p.override !== null">modifié</span></td>
              <td v-if="open === p.key" class="prompt-edit">
                <form class="item-form" @submit.prevent="save(p)">
                  <textarea v-model="draft" rows="8" :maxlength="data.max_chars" :aria-label="p.key"></textarea>
                  <p class="note" v-if="p.variables.length">Variables :
                    <code v-for="v in p.variables" :key="v" v-text="'{' + v + '} '"></code></p>
                  <div class="item-actions">
                    <button type="submit" class="btn" :disabled="busy">Enregistrer</button>
                    <button type="button" class="btn ghost" @click="cancel">Annuler</button></div>
                </form></td>
              <td v-else class="prompt-text">{{ p.override ?? p.default }}</td>
              <td><div class="row-actions" v-if="data.writable">
                <button type="button" class="btn ghost" v-if="open !== p.key" @click="edit(p)">Modifier</button>
                <button type="button" class="btn ghost" v-if="p.override !== null" :disabled="busy"
                        @click="reset(p)">Rétablir</button></div></td>
            </tr>
          </tbody>
        </table></div>
        <p class="note" v-if="!groups.length">Aucun texte ne correspond.</p>
      </section>
    </div>
</template>
