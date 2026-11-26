/*
  Configuration → Proxy : TOUTE la configuration, à voir et à modifier.
  Un sommaire (la section à l'écran en évidence), une barre d'actions qui
  reste en haut, les sections du formulaire (proxy/form.js), la section
  « Avancé » (proxy/advanced.js : tout réglage hors formulaire), puis le
  fichier complet et le .env.

  Le formulaire part de la configuration en vigueur (GET /ui/config) et
  ce qu'on y change s'y applique côté proxy (POST /ui/config/merge, avec
  les `overrides` d'Avancé) : sans changement, le fichier est identique.
  Modifié à la main, le fichier n'est plus écrasé (« Réappliquer les
  sections »). Vérifié par le proxy (POST /ui/config/check), puis
  ENREGISTRÉ (POST /ui/config/save) : le proxy met à jour data/config.toml
  valeur par valeur — commentaires, «${VAR}» et secrets masqués gardés —
  et le recharge ; refusé si le fichier a changé depuis la lecture
  (`base`). Copier et Télécharger restent possibles.
*/
import { call } from "../lib/http.js";
import { useCopy, download } from "../lib/files.js";
import { randomKey, useCatalog } from "./shared.js";
import { TOOL_LIST, newForm, prefillForm, buildProxy } from "./proxy/form.js";
import { advancedRows, parseAdv, docHtml } from "./proxy/advanced.js";
import { PROVIDERS, providerBackend, buildOverride } from "./proxy/providers.js";

const { ref, reactive, computed, watch, onMounted, onBeforeUnmount, nextTick } = Vue;

export const ProxyPart = {
  props: { session: { type: Object, required: true } },
  setup(props) {
    const { modelChoices } = useCatalog();
    // Le rôle d'un réglage, en HTML (Markdown, renvois vers GitHub).
    const doc = (text) => docHtml(text, props.session && props.session.version);
    const { copied, copy } = useCopy();
    const px = reactive(newForm());
    // ── la configuration complète : UN fichier ───────────────────────────
    // `generated` : le fichier complet que donne le formulaire (POST
    // /ui/config/merge) ; `text` : ce qu'affiche la page — `generated`, tant
    // qu'on ne l'a pas modifié à la main.
    const settings = reactive({ loaded: false, path: "", base: "", writable: false, defaults: 0, values: null,
                                defaultPaths: [], docs: {}, original: "",
                                generated: "", text: "", error: "", check: null, checking: false });
    async function loadSettings() {
      settings.error = "";
      settings.check = null;
      try {
        const r = await call("GET", "/ui/config");
        Object.assign(settings, { loaded: true, path: r.path, base: r.base, writable: !!r.writable,
                                  defaults: r.defaults,
                                  values: r.values, defaultPaths: r.default_paths || [],
                                  docs: r.docs || {},
                                  original: r.toml, generated: r.toml, text: r.toml });
        loadAdvanced();
        if (!prefilled.value) prefill();
      } catch (e) { settings.error = e.message; }
    }
    async function checkSettings() {
      settings.checking = true;
      settings.check = null;
      try { settings.check = await call("POST", "/ui/config/check", { toml: settings.text }); }
      catch (e) { settings.error = e.message; }
      finally { settings.checking = false; }
    }
    watch(() => settings.text, () => { settings.check = null; });
    // Recharger : le proxy relit SON data/config.toml (posé sur l'hôte) et
    // applique à chaud ce qui peut l'être (POST /ui/config/reload, comme un
    // SIGHUP) ; le reste attend un redémarrage. Puis la page relit la
    // configuration en vigueur.
    const reloaded = reactive({ busy: false, result: null, error: "", saved: false });
    // Recharger (le fichier tel qu'il est) ou Enregistrer (le fichier de la
    // page écrit, puis rechargé) : même compte rendu.
    async function applySettings(save) {
      reloaded.busy = true;
      reloaded.result = null;
      reloaded.error = "";
      reloaded.saved = save;
      try {
        reloaded.result = save
          ? await call("POST", "/ui/config/save", { toml: settings.text, base: settings.base })
          : await call("POST", "/ui/config/reload", {});
        await resetProxy();
      } catch (e) {
        reloaded.error = e.message;
        // Modifié entre-temps (une autre page, à la main) : la page relit
        // la configuration en vigueur ; les changements sont à refaire.
        if (e.status === 409) {
          await resetProxy();
          reloaded.error += " — configuration relue : refaire les changements.";
        }
      } finally { reloaded.busy = false; }
    }
    const reloadSettings = () => applySettings(false);
    const saveSettings = () => applySettings(true);
    const settingsFile = computed(() => ({ name: "config.toml", download: "config.toml",
                                           content: settings.text }));
    const edited = computed(() => settings.text !== settings.generated);

    const prefilled = ref(false);

    // Réinitialiser : relire la configuration, puis le formulaire.
    async function resetProxy() {
      prefilled.value = false;
      await loadSettings();
    }

    const addBackend = () => px.backends.push({ name: "", url: "http://", key: false,
      api: "openai", quotas: false, images: false, toolChoice: "", maxTokens: "", fairShare: true,
      chatTemplate: false, headerNames: "", headerValues: {}, headersPlain: false });
    // Un fournisseur connu : nom, URL et variable de la clé remplis. Le
    // menu revient à son libellé, prêt pour le suivant.
    const provider = ref("");
    const usedNames = computed(() => new Set(px.backends.map((b) => String(b.name || "").trim())));
    function addProvider() {
      const p = PROVIDERS.find((x) => x.id === provider.value);
      provider.value = "";
      if (p && !usedNames.value.has(p.id)) px.backends.push(providerBackend(p));
    }
    const addKey = () => px.keys.push({ name: "PROXY_API_KEY_" + (px.keys.length + 1), value: randomKey() });

    const proxyOut = computed(() => buildProxy(px));
    // Les fichiers à poser à côté du compose : le .env du formulaire, puis
    // l'override, tiré du fichier complet (une ${VAR} ajoutée à la main ou
    // par Avancé y figure aussi).
    const hostFiles = computed(() => [
      { ...proxyOut.value.files[0], section: "env" },
      { name: "docker-compose.override.yml", download: "docker-compose.override.yml",
        section: "override", content: buildOverride(settings.text),
        note: "Le service du proxy : l'image publiée et les variables que le docker-compose.yml "
          + "ne transmet pas. Un override existe déjà : y fusionner ces lignes. Puis "
          + "docker compose pull albert-proxy && docker compose up -d albert-proxy." },
    ]);

    // Le formulaire change → le fichier complet se recalcule (un instant
    // après la dernière frappe), sauf s'il a été modifié à la main.
    let mergeTimer = 0;
    async function mergeForm() {
      if (!prefilled.value) return;
      try {
        const r = await call("POST", "/ui/config/merge", { toml: proxyOut.value.partial,
                                                            overrides: overrides.value });
        const follow = !edited.value;
        settings.generated = r.toml;
        settings.defaults = r.defaults;
        if (follow) settings.text = r.toml;
        settings.error = "";
      } catch (e) { settings.error = e.message; }
    }
    // Ce qui diffère de la configuration en vigueur, en lignes du fichier :
    // une valeur changée compte une fois (sa ligne d'avant, sa ligne d'après).
    const changes = computed(() => {
      if (!settings.loaded) return 0;
      const before = settings.original.split("\n"), after = settings.text.split("\n");
      const a = new Set(before), b = new Set(after);
      return Math.max(after.filter((l) => !a.has(l)).length, before.filter((l) => !b.has(l)).length);
    });
    const showWarn = ref(false);

    // ── avancé : tout réglage que le formulaire ne couvre pas ─────────────
    // Généré depuis la configuration en vigueur (fichier et défauts) : une
    // ligne par réglage, éditable selon son type. Ce qui change part au
    // proxy en `overrides` ({path, value}) avec le formulaire.
    const advanced = ref([]);        // [{path, group, key, kind, orig, value, isDefault}]
    const advFilter = ref("");
    function loadAdvanced() {
      advanced.value = advancedRows(settings.values, settings.defaultPaths, settings.docs);
    }
    const advError = (row) => parseAdv(row).error || "";
    const backendNames = computed(() => new Set(px.backends.map((b) => String(b.name || "").trim())));
    const overrides = computed(() => advanced.value.flatMap((row) => {
      const { value, error } = parseAdv(row);
      if (error || JSON.stringify(value) === JSON.stringify(row.orig)) return [];
      // Un backend retiré du formulaire ne revient pas par ses réglages avancés.
      if (row.path[0] === "backends" && !backendNames.value.has(row.path[1])) return [];
      return [{ path: row.path, value }];
    }));
    // Le filtre cherche dans le chemin ET dans la description (« relais »
    // trouve linux_relay).
    const advGroups = computed(() => {
      const f = advFilter.value.trim().toLowerCase();
      const groups = new Map();
      for (const row of advanced.value) {
        if (f && !(row.path.join(".") + " " + row.doc).toLowerCase().includes(f)) continue;
        if (!groups.has(row.group)) groups.set(row.group, []);
        groups.get(row.group).push(row);
      }
      return [...groups].sort(([x], [y]) => x.localeCompare(y))
        .map(([name, rows]) => ({ name, rows, doc: settings.docs[name] || "" }));
    });
    const advChanged = (row) => {
      const { value, error } = parseAdv(row);
      return !error && JSON.stringify(value) !== JSON.stringify(row.orig);
    };
    watch(() => [proxyOut.value.partial, JSON.stringify(overrides.value)], () => {
      clearTimeout(mergeTimer);
      mergeTimer = setTimeout(mergeForm, 300);
    });

    // ── sommaire : les sections de l'onglet, celle à l'écran en évidence ──
    const SECTIONS = [
      { id: "acces", label: "Accès" }, { id: "backends", label: "Backends" },
      { id: "surfaces", label: "Surfaces" }, { id: "outils", label: "Outils" },
      { id: "partage", label: "Partage équitable" }, { id: "comptes", label: "Comptes (OIDC)" },
      { id: "divers", label: "Divers" }, { id: "avance", label: "Avancé" },
      { id: "fichier", label: "Fichier complet", sep: true }, { id: "env", label: ".env" },
      { id: "override", label: "Override Docker" },
    ];
    const activeSection = ref("acces");
    let observer = null;
    function goTo(id) {
      activeSection.value = id;
      const el = document.getElementById("px-" + id);
      if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    function observeSections() {
      if (observer) observer.disconnect();
      if (!("IntersectionObserver" in window)) return;
      const visible = new Map();
      observer = new IntersectionObserver((entries) => {
        for (const e of entries) visible.set(e.target.dataset.section, e.isIntersecting);
        const first = SECTIONS.find((sct) => visible.get(sct.id));
        if (first) activeSection.value = first.id;
      }, { rootMargin: "-80px 0px -55% 0px" });
      document.querySelectorAll("[data-section]").forEach((el) => observer.observe(el));
    }
    onMounted(async () => { loadSettings(); await nextTick(); observeSections(); });
    onBeforeUnmount(() => observer && observer.disconnect());

    function prefill() {
      if (!settings.values) return;
      prefillForm(px, settings.values);
      prefilled.value = true;
    }

    return { settings, checkSettings, reloaded, reloadSettings, saveSettings, settingsFile, edited, resetProxy, changes, showWarn,
             advanced, advFilter, advGroups, advChanged, advError, SECTIONS, activeSection,
             goTo, TOOL_LIST, px, modelChoices, addBackend, addKey, randomKey, proxyOut,
             PROVIDERS, provider, usedNames, addProvider, hostFiles, doc, copied, copy, download };
  },
  template: `
    <div class="proxy-layout">
      <nav class="proxy-nav" aria-label="Sections">
        <template v-for="sct in SECTIONS" :key="sct.id">
          <hr v-if="sct.sep">
          <button type="button" :aria-current="activeSection === sct.id ? 'true' : null"
                  @click="goTo(sct.id)">{{ sct.label }}<span class="count" v-if="sct.id === 'backends'">{{ px.backends.length }}</span><span
                  class="count" v-else-if="sct.id === 'avance' && advanced.length">{{ advanced.length }}</span></button>
        </template>
      </nav>
      <div class="proxy-main">
        <div class="proxy-bar panel">
          <span class="proxy-state">
            <strong v-if="changes">{{ changes }} ligne{{ changes > 1 ? "s" : "" }} modifiée{{ changes > 1 ? "s" : "" }}</strong>
            <span v-else class="dim">Configuration en vigueur, sans modification</span>
            <button type="button" class="link warn-toggle" v-if="proxyOut.warn.length"
                    @click="showWarn = !showWarn">⚠ {{ proxyOut.warn.length }} avertissement{{ proxyOut.warn.length > 1 ? "s" : "" }}</button>
            <span class="dim" v-if="edited">· fichier modifié à la main</span>
          </span>
          <span class="config-actions">
            <button type="button" class="btn ghost" @click="resetProxy" :disabled="!settings.loaded"
                    title="Relit la configuration en vigueur : formulaire et fichier y reviennent">Réinitialiser</button>
            <button type="button" class="btn ghost" @click="checkSettings"
                    :disabled="!settings.loaded || settings.checking">Vérifier</button>
            <button type="button" class="btn ghost" @click="reloadSettings"
                    :disabled="!settings.loaded || reloaded.busy"
                    title="Le proxy relit data/config.toml tel qu'il est et applique à chaud ce qui peut l'être">Recharger</button>
            <button type="button" class="btn ghost" @click="copy(settingsFile)"
                    :disabled="!settings.loaded">{{ copied === settingsFile.name ? "Copié" : "Copier" }}</button>
            <button type="button" class="btn ghost" @click="download(settingsFile)"
                    :disabled="!settings.loaded">Télécharger</button>
            <button type="button" class="btn" @click="saveSettings" v-if="settings.writable"
                    :disabled="!settings.loaded || reloaded.busy"
                    title="Met à jour data/config.toml (ce qui change seulement), puis le recharge">Enregistrer</button>
          </span>
          <ul class="proxy-warn" v-if="showWarn && proxyOut.warn.length">
            <li v-for="w in proxyOut.warn" :key="w">{{ w }}</li>
          </ul>
          <p class="err" v-if="settings.error" role="status">{{ settings.error }}</p>
        <div v-if="reloaded.result || reloaded.error" class="settings-check" role="status">
          <p class="err" v-if="reloaded.error">{{ reloaded.saved ? "Enregistrement refusé" : "Rechargement refusé" }},
            configuration inchangée : {{ reloaded.error }}</p>
          <template v-else>
            <p class="ok" v-if="reloaded.saved">Enregistré : {{ reloaded.result.changed }} valeur(s) mise(s) à jour
              dans {{ settings.path }} (l'ancien fichier est gardé dans config-history/).</p>
            <p class="ok" v-if="reloaded.result.applied.length">Appliqué à chaud :
              <code v-for="k in reloaded.result.applied" :key="k">{{ k }}</code></p>
            <p class="dim" v-else>Aucun réglage rechargeable n'a changé.</p>
            <p class="err" v-if="reloaded.result.restart.length">Attend un redémarrage :
              <code v-for="k in reloaded.result.restart" :key="k">{{ k }}</code></p>
          </template>
        </div>
        <div v-if="settings.check" class="settings-check" role="status">
          <p class="err" v-if="!settings.check.ok">TOML invalide : {{ settings.check.error }}</p>
          <p class="ok" v-else-if="!settings.check.unknown.length">TOML valide, toutes les clés sont connues du proxy.</p>
          <p class="err" v-else>TOML valide, mais le proxy ne lit pas ces clés (faute de frappe, réglage retiré) :
            <code v-for="k in settings.check.unknown" :key="k">{{ k }}</code></p>
          <p class="note" v-if="settings.check.masked">{{ settings.check.masked }} valeur(s) masquée(s) à
            remettre avant usage.</p>
        </div>
        </div>
        <p class="note proxy-intro">Tout part de la configuration en vigueur : ce qu'on change ici
          s'applique au fichier complet (section « Fichier complet »).
          <template v-if="settings.writable"><strong>Enregistrer</strong> met à jour data/config.toml sur le
          proxy, valeur par valeur : commentaires, <code>\${VAR}</code> et secrets masqués restent tels
          quels, l'ancien fichier est gardé dans config-history/. Ce qui est rechargeable s'applique tout
          de suite ; le reste attend un redémarrage.</template>
          <template v-else>Sans comptes OIDC, la page n'écrit pas config.toml : télécharger le fichier,
          remplacer data/config.toml sur l'hôte (valeurs masquées remises), puis « Recharger ».</template></p>
        <section class="panel proxy-section" id="px-acces" data-section="acces">
          <div class="panel-head"><h2>Accès</h2></div>
          <div class="config-fields">
            <p class="note">Une clé par personne : chacune a ses statistiques, sa mémoire, ses conversations.</p>
            <div class="row" v-for="(k, i) in px.keys" :key="i">
              <input v-model="k.name" spellcheck="false" placeholder="VARIABLE" class="mono">
              <input v-model="k.value" spellcheck="false" placeholder="valeur (dans le .env)" class="mono">
              <button type="button" class="icon" title="Nouvelle valeur" @click="k.value = randomKey()">↻</button>
              <button type="button" class="icon danger" title="Retirer" @click="px.keys.splice(i, 1)">×</button>
            </div>
            <button type="button" class="btn ghost" @click="addKey">Ajouter une clé</button>
          </div>
        </section>
        <section class="panel proxy-section" id="px-backends" data-section="backends">
          <div class="panel-head"><h2>Backends</h2></div>
          <div class="config-fields">
            <div class="backend" v-for="(b, i) in px.backends" :key="i">
              <div class="row">
                <input v-model="b.name" placeholder="nom (préfixe)" spellcheck="false" class="mono">
                <input v-model="b.url" placeholder="http://hôte:port" spellcheck="false" class="mono grow">
                <button type="button" class="icon danger" title="Retirer" @click="px.backends.splice(i, 1)">×</button>
              </div>
              <div class="row checks">
                <span class="check">protocole
                  <select v-model="b.api" aria-label="protocole du backend"
                          @change="b.api === 'anthropic' && (b.images = true)">
                    <option value="openai">OpenAI</option><option value="anthropic">Anthropic</option></select></span>
                <label class="check"><input type="checkbox" v-model="b.key"> clé d'API</label>
                <label class="check"><input type="checkbox" v-model="b.quotas"> quotas (Albert)</label>
                <label class="check"><input type="checkbox" v-model="b.images"> images</label>
                <label v-if="b.api !== 'anthropic'" class="check" title="llama.cpp, vLLM : reçoit chat_template_kwargs, store, la réflexion de l'historique ; décoché, ils sont retirés (chat_template)">
                  <input type="checkbox" v-model="b.chatTemplate"> gabarit</label>
                <span class="check">tool_choice
                  <select v-model="b.toolChoice" aria-label="tool_choice injecté">
                    <option value="">jamais</option><option value="auto">auto</option>
                    <option value="required">required</option></select></span>
                <span class="check">max_tokens <input v-model="b.maxTokens" class="num" inputmode="numeric"
                  aria-label="max_tokens" placeholder="—"></span>
                <label class="check" title="Décoché : ses requêtes ne comptent pas dans la part des clients (fair_share = false)">
                  <input type="checkbox" v-model="b.fairShare"> partage équitable</label>
              </div>
              <div class="row">
                <span class="check grow" title="Ajoutés à chaque requête vers ce backend ; leurs valeurs vont dans le .env (\${VAR}), jamais en clair ici">en-têtes
                  <input v-model="b.headerNames" class="mono grow" spellcheck="false" aria-label="en-têtes du backend"
                         placeholder="X-Equipe, User-Agent (valeurs dans le .env)"></span>
              </div>
            </div>
            <div class="row">
              <button type="button" class="btn ghost" @click="addBackend">Ajouter un backend</button>
              <select v-model="provider" @change="addProvider" aria-label="Ajouter un fournisseur connu">
                <option value="">Ajouter un fournisseur connu…</option>
                <option v-for="p in PROVIDERS" :key="p.id" :value="p.id" :disabled="usedNames.has(p.id)">{{ p.label }} — {{ p.note }}{{ usedNames.has(p.id) ? " (déjà là)" : "" }}</option>
              </select>
            </div>
            <p class="note">Un fournisseur connu remplit le nom, l'URL et la variable de sa clé : la valeur
              va dans le .env, et l'override la transmet au conteneur.</p>
          </div>
        </section>
        <section class="panel proxy-section" id="px-surfaces" data-section="surfaces">
          <div class="panel-head"><h2>Surfaces</h2></div>
          <div class="config-fields">
            <label class="check"><input type="checkbox" v-model="px.anthropic"> surface Anthropic (Claude Code)</label>
            <label class="field" v-if="px.anthropic"><span>Modèle par défaut de Claude Code</span>
              <select v-model="px.defaultModel" class="mono">
                <option value="">— aucun —</option>
                <option v-for="m in modelChoices(px.defaultModel)" :key="m" :value="m">{{ m }}</option>
              </select></label>
            <label class="check"><input type="checkbox" v-model="px.responses"> surface Responses (Codex)</label>
            <label class="check"><input type="checkbox" v-model="px.mcpServer"> serveur MCP (/mcp)</label>
            <label class="check"><input type="checkbox" v-model="px.uiChat"> chat de /ui</label>
            <label class="field" v-if="px.uiChat"><span>Modèle présélectionné dans le chat</span>
              <select v-model="px.chatModel" class="mono">
                <option value="">— le dernier choisi, sinon le premier —</option>
                <option v-for="m in modelChoices(px.chatModel)" :key="m" :value="m">{{ m }}</option>
              </select></label>
          </div>
        </section>
        <section class="panel proxy-section" id="px-outils" data-section="outils">
          <div class="panel-head"><h2>Outils hébergés</h2></div>
          <div class="config-fields">
            <label class="check"><input type="checkbox" v-model="px.hostedTools"> sur /v1/chat/completions</label>
            <div class="tools-grid">
              <template v-for="t in TOOL_LIST" :key="t.id">
                <label class="check"><input type="checkbox" v-model="px.tools[t.id]"> {{ t.label }}</label>
                <select v-if="t.model && px.tools[t.id]" v-model="px.toolModels[t.id]" class="mono">
                  <option value="">— choisir le modèle —</option>
                  <option v-for="m in modelChoices(px.toolModels[t.id], true)" :key="m" :value="m">{{ m }}</option>
                </select>
                <input v-else-if="t.id === 'web_search' && px.tools[t.id]" v-model="px.searxng"
                       spellcheck="false" class="mono">
                <input v-else-if="t.id === 'code_execution' && px.tools[t.id]" v-model="px.executor"
                       spellcheck="false" class="mono">
                <span v-else></span>
              </template>
            </div>
            <label class="field"><span>Appels d'outils par réponse</span>
              <input v-model.number="px.maxCalls" type="number" min="1" max="64" class="num"></label>
            <label class="field"><span>Présentés d'office ([chat].always)</span>
              <input v-model="px.always" placeholder="web_search, web_fetch" spellcheck="false" class="mono"></label>
          </div>
        </section>
        <section class="panel proxy-section" id="px-partage" data-section="partage">
          <div class="panel-head"><h2>Partage équitable</h2></div>
          <div class="config-fields">
            <p class="note">Une part de tokens par client sur une heure glissante : au-delà, ses requêtes
              attendent, rien n'est refusé. La part de chaque compte se règle dans Comptes.</p>
            <label class="field"><span>Part par défaut (tokens / heure, 0 = aucune limite)</span>
              <input v-model.number="px.fairShare" type="number" min="0" step="10000" class="num"></label>
            <label class="field"><span>Routes exemptées (quotas et partage)</span>
              <input v-model="px.exempt" placeholder="/embeddings, /ocr" spellcheck="false" class="mono"></label>
          </div>
        </section>
        <section class="panel proxy-section" id="px-comptes" data-section="comptes">
          <div class="panel-head"><h2>Comptes (OIDC)</h2></div>
          <div class="config-fields">
            <label class="check"><input type="checkbox" v-model="px.oidc.on"> connexion par un fournisseur OIDC</label>
            <template v-if="px.oidc.on">
              <p class="note">client_secret va dans le .env. Chez le fournisseur : un client
                confidentiel, retour sur &lt;adresse publique&gt;/ui/auth/callback.</p>
              <label class="field"><span>Émetteur (issuer)</span>
                <input v-model="px.oidc.issuer" placeholder="https://auth.example.org/realms/x" spellcheck="false" class="mono"></label>
              <label class="field"><span>Adresse de retour (vide : adresse publique + /ui/auth/callback)</span>
                <input v-model="px.oidc.redirectUri" placeholder="https://llm.example.org/ui/auth/callback" spellcheck="false" class="mono"></label>
              <label class="field"><span>Nouveaux comptes</span>
                <select v-model="px.oidc.newAccounts">
                  <option value="pending">en attente d'un admin</option>
                  <option value="user">actifs d'emblée</option></select></label>
              <label class="field"><span>Durée d'une session (jours)</span>
                <input v-model.number="px.oidc.sessionDays" type="number" min="1" class="num"></label>
              <label class="field"><span>Clés actives par compte</span>
                <input v-model.number="px.oidc.maxKeys" type="number" min="1" class="num"></label>
              <label class="field"><span>Libellé du bouton</span>
                <input v-model="px.oidc.label" spellcheck="false"></label>
            </template>
          </div>
        </section>
        <section class="panel proxy-section" id="px-divers" data-section="divers">
          <div class="panel-head"><h2>Divers</h2></div>
          <div class="config-fields">
            <label class="field"><span>Adresse publique (liens des fichiers)</span>
              <input v-model="px.publicUrl" placeholder="https://llm.example.org" spellcheck="false" class="mono"></label>
            <label class="field"><span>Rétention des statistiques (jours, 0 = illimitée)</span>
              <input v-model.number="px.retention" type="number" min="0" class="num"></label>
          </div>
        </section>
        <section class="panel proxy-section" id="px-avance" data-section="avance">
          <div class="panel-head"><h2>Avancé</h2>
            <input class="adv-filter" v-model="advFilter" type="search" placeholder="Filtrer (ex. timeout, relais)"
                   aria-label="Filtrer les réglages avancés" spellcheck="false"></div>
          <p class="note adv-intro">Chaque réglage que les sections ci-dessus ne couvrent pas, à sa valeur
            en vigueur (« défaut » : le fichier ne le dit pas, le proxy le prend d'office).</p>
          <div class="adv-group" v-for="g in advGroups" :key="g.name">
            <h3>[{{ g.name }}]</h3>
            <div class="adv-group-doc md" v-if="g.doc" v-html="doc(g.doc)"></div>
            <div class="adv-row" v-for="row in g.rows" :key="JSON.stringify(row.path)"
                 :class="{ changed: advChanged(row) }">
              <label class="adv-key mono" :for="'adv-' + row.path.join('.')">{{ row.key }}</label>
              <span class="adv-val">
                <input v-if="row.kind === 'bool'" :id="'adv-' + row.path.join('.')" type="checkbox" v-model="row.value">
                <input v-else :id="'adv-' + row.path.join('.')" v-model="row.value" spellcheck="false"
                       :class="{ mono: row.kind !== 'number', num: row.kind === 'number' }"
                       :inputmode="row.kind === 'number' ? 'decimal' : null">
                <span class="err" v-if="advError(row)">{{ advError(row) }}</span>
              </span>
              <span class="adv-tags">
                <span class="tag" v-if="row.isDefault && !advChanged(row)">défaut</span>
                <span class="tag changed" v-if="advChanged(row)">modifié</span>
              </span>
              <div class="adv-doc md" v-if="row.doc && row.doc !== g.doc" v-html="doc(row.doc)"></div>
            </div>
          </div>
          <p class="note adv-intro" v-if="!advGroups.length">{{ advFilter ? "Aucun réglage ne correspond." : "Chargement…" }}</p>
        </section>
        <section class="panel proxy-section config-settings" id="px-fichier" data-section="fichier">
          <div class="panel-head"><h2>Fichier complet</h2>
            <span class="config-actions">
              <button type="button" class="btn ghost" @click="settings.text = settings.generated"
                      :disabled="!edited" title="Rend au fichier les valeurs des sections">Réappliquer les sections</button>
            </span></div>
          <p class="note"><code>{{ settings.path }}</code> avec ce que les sections y changent ; les valeurs
            que le proxy prend d'office sont suivies de « # défaut » ({{ settings.defaults }}). Les
            <code>\${VAR}</code> renvoient au .env ; une valeur secrète écrite en clair est masquée. Modifiable
            directement : les sections ne l'écrasent plus alors.</p>
          <textarea v-model="settings.text" spellcheck="false" aria-label="config.toml"
                    :placeholder="settings.loaded ? '' : 'Chargement…'"></textarea>
        </section>
        <section class="panel proxy-section config-file" :id="'px-' + f.section" :data-section="f.section"
                 v-for="f in hostFiles" :key="f.name">
          <header class="panel-head">
            <h2>{{ f.name }}</h2>
            <span class="config-actions">
              <button type="button" class="btn ghost" @click="copy(f)">{{ copied === f.name ? "Copié" : "Copier" }}</button>
              <button type="button" class="btn ghost" @click="download(f)">Télécharger</button>
            </span>
          </header>
          <p class="note" v-if="f.note">{{ f.note }}</p>
          <pre>{{ f.content }}</pre>
        </section>
      </div>
    </div>
  `,
};
