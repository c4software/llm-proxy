<script>
/*
  Configuration → Exécution (/ui/runtime, runtime_routes.py) : les BACS de
  l'exécuteur de code et les CACHES du proxy.

    pour chacun   ses bacs (une conversation, ses dépôts), à détruire un à
                  un ou tous ; ses dépôts téléchargés (« Oublier » est dans
                  Dépôts)
    admin         l'état de l'exécuteur et ses bornes, les bacs de tous
                  (avec leur compte), les caches du proxy, à vider

  Détruire un bac perd ses fichiers, même en pleine exécution ; les
  boutons qui détruisent demandent un second clic.
*/
import * as Vue from "vue";
import { call } from "../lib/http.js";
import { dur, num, size } from "../lib/format.js";

const { ref, onMounted } = Vue;

const SESSIONS = { repository: "dépôts", "~": "un seul appel" };

const RuntimePart = {
  setup() {
    const data = ref(null);
    const error = ref("");
    const done = ref("");
    const armed = ref(null);
    const busy = ref(false);

    async function load() {
      try {
        data.value = await call("GET", "/ui/runtime");
        error.value = "";
      } catch (e) { error.value = e.message; }
    }
    // Un second clic dans les 3 s : ce qui détruit ou vide.
    function confirm(key) {
      if (armed.value === key) { armed.value = null; return true; }
      armed.value = key;
      setTimeout(() => { if (armed.value === key) armed.value = null; }, 3000);
      return false;
    }
    async function act(key, method, path, body, say) {
      if (!confirm(key)) return;
      busy.value = true;
      error.value = "";
      try {
        const r = await call(method, path, body);
        done.value = say(r);
        setTimeout(() => { done.value = ""; }, 4000);
      } catch (e) { error.value = e.message; }
      finally { busy.value = false; await load(); }
    }
    const destroy = (b, admin = false) => act("bac:" + b.client + ":" + b.session, "POST",
      "/ui/runtime/bacs/destroy", { session: b.session, ...(admin ? { client: b.client } : {}) },
      (r) => (r.destroyed ? "Bac détruit." : "Ce bac n'existait plus."));
    const destroyAll = (everyone) => act(everyone ? "all" : "mine", "POST",
      "/ui/runtime/bacs/destroy_all", { everyone },
      (r) => `${r.destroyed} bac${r.destroyed > 1 ? "s" : ""} détruit${r.destroyed > 1 ? "s" : ""}.`);
    const clear = (c) => act("cache:" + c.name, "POST", "/ui/runtime/caches/" + c.name + "/clear", {},
      (r) => `${c.label} : ${r.cleared} entrée${r.cleared > 1 ? "s" : ""} retirée${r.cleared > 1 ? "s" : ""}.`);

    const session = (s) => SESSIONS[s] || "conversation";
    const duration = dur;
    const bytes = (n) => (n == null ? "—" : size(n));

    onMounted(load);
    return { data, error, done, armed, busy, load, destroy, destroyAll, clear, session, duration, bytes, num };
  },
};

export default RuntimePart;
</script>

<template>
    <div class="config-keys owned">
      <p class="err" v-if="error" role="status">{{ error }}</p>
      <p class="note ok" v-if="done" role="status">{{ done }}</p>
      <template v-if="data">
        <section class="panel">
          <div class="panel-head"><h2>Vos bacs</h2>
            <div class="item-actions">
              <button type="button" class="btn ghost" @click="load">Rafraîchir</button>
              <button type="button" class="btn danger" v-if="data.own.bacs && data.own.bacs.length"
                      :class="{ armed: armed === 'mine' }" :disabled="busy"
                      @click="destroyAll(false)">{{ armed === "mine" ? "Tout détruire ?" : "Tout détruire" }}</button></div></div>
          <p class="note">Les fichiers de vos conversations (exécution de code) et de vos dépôts. Détruire un bac
            efface ses fichiers.</p>
          <p class="note" v-if="data.own.bacs === null">Exécuteur injoignable.</p>
          <div class="scroll" v-else-if="data.own.bacs.length"><table>
            <thead><tr><th>Usage</th><th>Ouvert depuis</th><th>Inactif depuis</th><th>Détruit dans</th><th></th></tr></thead>
            <tbody>
              <tr v-for="b in data.own.bacs" :key="b.session">
                <td>{{ session(b.session) }}<span class="tag" v-if="b.running">en cours</span></td>
                <td>{{ duration(b.age) }}</td><td>{{ duration(b.idle_for) }}</td><td>{{ duration(b.expires_in) }}</td>
                <td><div class="row-actions">
                  <button type="button" class="btn danger" :class="{ armed: armed === 'bac:' + b.client + ':' + b.session }"
                          :disabled="busy || b.session === '~'" @click="destroy(b)">{{ armed === 'bac:' + b.client + ':' + b.session ? "Détruire ?" : "Détruire" }}</button></div></td>
              </tr>
            </tbody>
          </table></div>
          <p class="note" v-else>Aucun bac ouvert.</p>
          <p class="note" v-if="data.own.repositories.entries">Dépôts téléchargés : {{ data.own.repositories.entries }}
            ({{ bytes(data.own.repositories.bytes) }}).</p>
        </section>

        <template v-if="data.admin">
          <section class="panel">
            <div class="panel-head"><h2>Exécuteur</h2></div>
            <p class="note" v-if="!data.admin.executor">Injoignable.</p>
            <dl class="kv" v-else>
              <dt>État</dt><dd>{{ data.admin.executor.status }}<span class="err" v-if="data.admin.executor.error"> — {{ data.admin.executor.error }}</span></dd>
              <dt>En service depuis</dt><dd>{{ duration(data.admin.executor.uptime) }}</dd>
              <dt>Bacs ouverts</dt><dd>{{ data.admin.executor.sessions }} sur {{ data.admin.executor.limits.max_sessions }},
                {{ data.admin.executor.limits.max_sessions_per_client }} par compte</dd>
              <dt>Un bac</dt><dd>{{ data.admin.executor.limits.memory }}, {{ data.admin.executor.limits.cpus }} CPU,
                /work de {{ data.admin.executor.limits.work_size }} ; détruit après {{ duration(data.admin.executor.limits.idle) }}
                d'inactivité, {{ duration(data.admin.executor.limits.lifetime) }} au plus</dd>
              <dt>Réseau des bacs</dt><dd><template v-if="data.admin.executor.egress.enabled">aucun, sauf
                {{ data.admin.executor.egress.hosts.length }} domaines pour installer des paquets (pip, npm, cargo…)
                <details><summary>voir les domaines</summary>{{ data.admin.executor.egress.hosts.join(", ") }}</details></template>
                <template v-else>aucun</template></dd>
            </dl>
          </section>
          <section class="panel">
            <div class="panel-head"><h2>Tous les bacs</h2>
              <button type="button" class="btn danger" v-if="data.admin.bacs && data.admin.bacs.length"
                      :class="{ armed: armed === 'all' }" :disabled="busy"
                      @click="destroyAll(true)">{{ armed === "all" ? "Tout détruire ?" : "Tout détruire" }}</button></div>
            <p class="note" v-if="data.admin.bacs === null">Exécuteur injoignable.</p>
            <div class="scroll" v-else-if="data.admin.bacs.length"><table>
              <thead><tr><th>Compte</th><th>Usage</th><th>Ouvert depuis</th><th>Inactif depuis</th><th>Détruit dans</th><th></th></tr></thead>
              <tbody>
                <tr v-for="b in data.admin.bacs" :key="b.client + b.session">
                  <td>{{ b.account }}</td>
                  <td>{{ session(b.session) }}<span class="tag" v-if="b.running">en cours</span></td>
                  <td>{{ duration(b.age) }}</td><td>{{ duration(b.idle_for) }}</td><td>{{ duration(b.expires_in) }}</td>
                  <td><div class="row-actions">
                    <button type="button" class="btn danger" :class="{ armed: armed === 'bac:' + b.client + ':' + b.session }"
                            :disabled="busy || b.session === '~'" @click="destroy(b, true)">{{ armed === 'bac:' + b.client + ':' + b.session ? "Détruire ?" : "Détruire" }}</button></div></td>
                </tr>
              </tbody>
            </table></div>
            <p class="note" v-else>Aucun bac ouvert.</p>
          </section>
          <section class="panel">
            <div class="panel-head"><h2>Caches du proxy</h2></div>
            <p class="note">En mémoire. Vider un cache ne casse rien : ce qui manque est relu au besoin.</p>
            <div class="scroll"><table>
              <thead><tr><th>Cache</th><th>Entrées</th><th>Taille</th><th>Durée de vie</th><th></th></tr></thead>
              <tbody>
                <tr v-for="c in data.admin.caches" :key="c.name">
                  <td>{{ c.label }}</td><td>{{ num(c.entries) }}</td>
                  <td>{{ c.entries && c.bytes != null ? bytes(c.bytes) : "—" }}</td><td>{{ duration(Math.round(c.ttl)) }}</td>
                  <td><div class="row-actions">
                    <button type="button" class="btn danger" v-if="c.entries" :class="{ armed: armed === 'cache:' + c.name }"
                            :disabled="busy" @click="clear(c)">{{ armed === 'cache:' + c.name ? "Vider ?" : "Vider" }}</button></div></td>
                </tr>
              </tbody>
            </table></div>
          </section>
        </template>
      </template>
    </div>
</template>
