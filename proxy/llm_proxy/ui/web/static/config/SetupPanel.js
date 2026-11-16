/*
  La page d'INSTALLATION : premier démarrage, sans data/config.toml
  (/ui/session → setup). Les fournisseurs et quelques options donnent les
  trois fichiers d'une installation Docker Compose : .env, data/config.toml
  et docker-compose.override.yml. Tout est calculé dans le navigateur :
  rien n'est écrit sur le proxy, et les clés des fournisseurs ne sont pas
  demandées (le .env les attend vides). Le fichier posé, un redémarrage
  quitte ce mode ; Configuration → Proxy prend alors le relais.
*/
import { useCopy, download } from "../lib/files.js";
import { q, list, randomKey } from "./shared.js";
import { PROVIDERS, buildOverride } from "./proxy/providers.js";

const { ref, reactive, computed, watch } = Vue;

const STORE = "llm-proxy-setup";

function hex(n) {
  const b = new Uint8Array(n);
  crypto.getRandomValues(b);
  return [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
}

export const SetupPanel = {
  setup() {
    const { copied, copy } = useCopy();
    // Les choix sont gardés dans ce navigateur, jamais les clés.
    const saved = (() => {
      try { return JSON.parse(localStorage.getItem(STORE) || "null") || {}; } catch { return {}; }
    })();
    const c = reactive({
      providers: ["nvidia", "amd"], second: false, chat: true, agents: false,
      share: 0, model: "", ...saved,
    });
    watch(c, (v) => { try { localStorage.setItem(STORE, JSON.stringify(v)); } catch { /* sans effet */ } },
          { deep: true });
    const keys = ref({});
    const newKeys = () => { keys.value = { proxy: randomKey(), second: randomKey(), searxng: hex(32) }; };
    newKeys();

    const active = computed(() => PROVIDERS.filter((p) => c.providers.includes(p.id)));
    const models = computed(() => active.value.filter((p) => p.model).map((p) => p.model));
    // Le modèle par défaut suit les fournisseurs cochés.
    watch(models, (m) => { if (!m.includes(c.model)) c.model = m[0] || ""; }, { immediate: true });

    const env = computed(() => {
      const L = ["# .env, à côté du docker-compose.yml : les secrets, jamais dans Git",
                 `PROXY_API_KEY=${keys.value.proxy}`];
      if (c.second) L.push(`PROXY_API_KEY_2=${keys.value.second}`);
      L.push("# Les clés des fournisseurs, à compléter");
      for (const p of active.value) L.push(`${p.keyVar}=`);
      L.push("# Le secret de SearXNG (recherche web)", `SEARXNG_SECRET=${keys.value.searxng}`,
             "# Votre identifiant (id -u et id -g) ; 0 et 0 avec Docker rootless",
             "PUID=1000", "PGID=1000");
      return L.join("\n") + "\n";
    });

    const toml = computed(() => {
      const accepted = ["${PROXY_API_KEY}"];
      if (c.second) accepted.push("${PROXY_API_KEY_2}");
      const L = ["# data/config.toml — chaque réglage est décrit dans data/config.example.toml", "",
                 "[proxy]", "# Les clés acceptées : une par personne. Liste vide = proxy ouvert à tous.",
                 `api_keys = ${list(accepted)}`];
      for (const p of active.value) {
        L.push("", `# Les modèles ${p.id}/…`, `[backends.${p.id}]`, `url = ${q(p.url)}`,
               `api_key = "\${${p.keyVar}}"`);
        if (p.api) L.push(`api = ${q(p.api)}`);
        if (p.quotas) L.push("quotas = true");
        if (p.toolChoice) L.push(`force_tool_choice = ${q(p.toolChoice)}`);
      }
      if (c.agents) {
        L.push("", "# Claude Code : l'API Messages d'Anthropic, traduite", "[anthropic]", "enabled = true");
        if (c.model) L.push("", "# Les noms Claude envoyés par Claude Code → un modèle du proxy",
                            "[anthropic.model_map]", `default = ${q(c.model)}`);
        L.push("", "# Codex : l'API Responses, traduite", "[responses]", "enabled = true");
      }
      if (c.chat) {
        L.push("", "# Le chat du tableau de bord (il dépense des tokens)", "[ui]", "chat = true");
        if (c.model) L.push(`chat_model = ${q(c.model)}`);
      }
      const share = Math.max(0, Math.floor(Number(c.share) || 0));
      if (share) L.push("", "[fair_share]", "# Tokens (entrée + sortie) par heure glissante et par clé.",
                        `tokens_per_hour = ${share}`);
      return L.join("\n") + "\n";
    });

    const files = computed(() => [
      { name: ".env", download: "llm-proxy.env", where: "À la racine du dossier, à côté du docker-compose.yml. "
          + "Complétez les clés des fournisseurs.", content: env.value },
      { name: "data/config.toml", download: "config.toml", where: "Dans le dossier data/ (le volume du proxy).",
        content: toml.value },
      { name: "docker-compose.override.yml", download: "docker-compose.override.yml",
        where: "À côté du docker-compose.yml : l'image publiée, et les clés transmises au conteneur. "
          + "Un override existe déjà : y fusionner ces lignes.", content: buildOverride(toml.value) },
    ]);
    const tab = ref(".env");

    return { PROVIDERS, c, models, newKeys, files, tab, copied, copy, download };
  },
  template: `
    <div class="setup">
      <p class="note setup-intro">Ce proxy n'a pas encore de configuration (<code>data/config.toml</code>
        absent) : son API répond 503 en attendant. Choisissez vos fournisseurs et vos options, posez les
        trois fichiers, puis redémarrez. <strong>Rien n'est écrit sur le proxy</strong>, et les clés
        sont tirées au hasard dans ce navigateur.</p>
      <div class="setup-form">
        <section class="panel">
          <div class="panel-head"><h2>Fournisseurs</h2></div>
          <div class="config-fields">
            <label class="check" v-for="p in PROVIDERS" :key="p.id">
              <input type="checkbox" v-model="c.providers" :value="p.id">
              <span><strong>{{ p.label }}</strong> <span class="dim">— {{ p.note }}</span></span></label>
          </div>
        </section>
        <section class="panel">
          <div class="panel-head"><h2>Options</h2></div>
          <div class="config-fields">
            <label class="check"><input type="checkbox" v-model="c.second"> une deuxième clé (un binôme)</label>
            <label class="check"><input type="checkbox" v-model="c.chat"> le chat du tableau de bord
              <span class="dim">(dépense des tokens)</span></label>
            <label class="check"><input type="checkbox" v-model="c.agents"> Claude Code et Codex
              <span class="dim">(surfaces Anthropic et Responses)</span></label>
            <label class="field"><span>Partage équitable, tokens par heure et par clé (0 = aucune limite)</span>
              <input v-model.number="c.share" type="number" min="0" step="10000" class="num"></label>
            <label class="field"><span>Modèle par défaut (chat, Claude Code)</span>
              <input v-model="c.model" list="setup-models" spellcheck="false" class="mono"
                     placeholder="fournisseur/modèle">
              <datalist id="setup-models"><option v-for="m in models" :key="m" :value="m"></option></datalist></label>
            <button type="button" class="btn ghost" @click="newKeys">Nouvelles clés</button>
          </div>
        </section>
      </div>

      <p class="err" v-if="!c.providers.length" role="status">Choisissez au moins un fournisseur.</p>
      <template v-else>
        <div class="setup-tabs" role="tablist">
          <button v-for="f in files" :key="f.name" type="button" role="tab" class="btn ghost"
                  :aria-selected="String(tab === f.name)" :class="{ on: tab === f.name }"
                  @click="tab = f.name">{{ f.name }}</button>
        </div>
        <section class="panel config-file" v-for="f in files" v-show="tab === f.name" :key="f.name">
          <header class="panel-head">
            <h2>{{ f.name }}</h2>
            <span class="config-actions">
              <button type="button" class="btn ghost" @click="copy(f)">{{ copied === f.name ? "Copié" : "Copier" }}</button>
              <button type="button" class="btn ghost" @click="download(f)">Télécharger</button>
            </span>
          </header>
          <p class="note">{{ f.where }}</p>
          <pre>{{ f.content }}</pre>
        </section>

        <section class="panel">
          <div class="panel-head"><h2>Ensuite</h2></div>
          <ol class="setup-steps">
            <li>Posez les trois fichiers, et complétez les clés des fournisseurs dans le <code>.env</code>.</li>
            <li>Redémarrez : <code>docker compose pull albert-proxy &amp;&amp; docker compose up -d</code>.</li>
            <li>Rechargez cette page et entrez la clé <code>PROXY_API_KEY</code> du <code>.env</code>.</li>
            <li><strong>Configuration → Clients</strong> donne alors la configuration d'OpenCode, de Claude Code
              ou de Codex ; <strong>Configuration → Proxy</strong>, tout le reste.</li>
          </ol>
        </section>
      </template>
    </div>
  `,
};
