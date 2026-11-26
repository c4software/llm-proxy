<script>
/*
  Configuration → Extensions (/ui/extensions, extension_routes.py ; lue
  par un admin, ou la page d'un proxy sans comptes ; basculée — écrite
  dans config.toml — par un admin OIDC seulement, `writable`) : les extensions de [extensions].path, un
  sous-dossier chacune. Une extension déposée paraît ici, DÉSACTIVÉE, au
  démarrage suivant ; elle s'active et se désactive ici, À CHAUD ; ses outils, où chacun s'exécute (proxy, navigateur), où il est
  offert (api, web) et s'il est configuré. Chaque outil s'active et se
  désactive par son bouton (celui du proxy sans celui du navigateur, par
  exemple). Une extension
  écartée au chargement est dite avec sa raison ; arrêtée par
  [extensions.<nom>].enabled = false, la page ne la réactive pas.
*/
import * as Vue from "vue";
import { call } from "../lib/http.js";

const { ref, onMounted } = Vue;

const OFFERS = { api: "API", web: "page et webhooks" };
const RUNS = { proxy: "proxy", browser: "navigateur" };

const ExtensionsPart = {
  setup() {
    const data = ref(null);
    const error = ref("");
    const busy = ref("");

    async function load() {
      try {
        data.value = await call("GET", "/ui/extensions");
        error.value = "";
      } catch (e) { error.value = e.message; }
    }
    // Un outil de l'extension.
    async function toggle(e, t) {
      busy.value = e.name;
      error.value = "";
      try {
        await call("POST", "/ui/extensions/" + encodeURIComponent(e.name), { enabled: !t.enabled, tool: t.name });
      } catch (err) { error.value = err.message; }
      finally { busy.value = ""; await load(); }
    }
    const state = (e) => (e.tools.every((t) => t.enabled) ? "active"
      : e.tools.some((t) => t.enabled) ? "en partie" : "inactive");
    const offered = (t) => (RUNS[t.runs] || t.runs) + " ; " + t.offered.map((o) => OFFERS[o] || o).join(", ");

    onMounted(load);
    return { data, error, busy, load, toggle, offered, state };
  },
};

export default ExtensionsPart;
</script>

<template>
    <div class="config-keys owned">
      <p class="err" v-if="error" role="status">{{ error }}</p>
      <section class="panel" v-if="data">
        <div class="panel-head"><h2>Extensions</h2>
          <div class="item-actions"><button type="button" class="btn ghost" @click="load">Rafraîchir</button></div></div>
        <p class="note" v-if="!data.path">Aucun dossier d'extensions : <code>[extensions].path</code> n'est pas réglé
          (voir docs/extensions.md).</p>
        <template v-else>
          <p class="note">Sous-dossiers de <code>{{ data.path }}</code>, chargés au démarrage. Une extension déposée y paraît
            désactivée : l'activer met ses outils en service, la désactiver les retire tout de suite,
            partout ; l'état est gardé dans config.toml.</p>
          <p class="note" v-if="!data.writable">Sans comptes OIDC, la page n'écrit pas config.toml : une extension
            s'active à la main, <code>[extensions.&lt;nom&gt;] active = true</code> (ou la liste de ses outils),
            puis redémarrage.</p>
          <div class="scroll" v-if="data.extensions.length"><table>
            <thead><tr><th>Extension</th><th>Outils</th><th>État</th></tr></thead>
            <tbody>
              <tr v-for="e in data.extensions" :key="e.name">
                <td><code>{{ e.name }}</code></td>
                <td v-if="e.tools">
                  <div v-for="t in e.tools" :key="t.name"><code>{{ t.name }}</code> — {{ offered(t) }}<span
                       class="tag" v-if="!t.configured">non configuré</span>
                    <template v-if="data.writable">· <button type="button" class="btn" :class="t.enabled ? 'ghost' : ''"
                              :disabled="busy === e.name" :aria-pressed="String(t.enabled)"
                              @click="toggle(e, t)">{{ t.enabled ? "Désactiver" : "Activer" }}</button></template></div></td>
                <td v-else class="err">{{ e.refused }}</td>
                <td>{{ e.tools ? state(e) : (e.stopped ? "bloquée par la configuration" : "écartée") }}</td>
              </tr>
            </tbody>
          </table></div>
          <p class="note" v-else>Aucune extension dans ce dossier.</p>
        </template>
      </section>
    </div>
</template>
