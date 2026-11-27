<script>
// @ts-check
/*
  L'application de la page : la SESSION et l'entrée, les VUES et le menu
  latéral (montée par main.js). Chaque vue est un composant dans son
  dossier (stats/, chat/, config/, memory/) ; lib/ : appels, formats,
  fichiers, Markdown, thème.

  La page lit d'abord /ui/session : auth active sans clé valide (401), elle
  montre l'écran d'entrée — la clé saisie part à /ui/login, qui ouvre une
  session signée (cookie HttpOnly) ; avec les comptes OIDC (/ui/auth dit
  «oidc»), un bouton mène au fournisseur (/ui/auth/login), et un compte en
  attente ou bloqué ne voit que son état.

  La vue est mémorisée dans l'URL (#chat, #config) et le navigateur. Le
  chat reste monté : une réponse en cours continue pendant qu'on regarde
  les chiffres. /ui/health est lu ici (puis toutes les 30 s) et passé aux
  vues qui en ont besoin.
*/
import * as Vue from "vue";
import * as VueUse from "@vueuse/core";
import { ChatStore } from "./chat/store.js";
import { useTheme } from "./lib/theme.js";
import Loading from "./Loading.vue";

const { ref, reactive, computed, watch, onMounted, onUnmounted, defineAsyncComponent } = Vue;


// La configuration et les statistiques : lues à leur première ouverture
// seulement (le chat, lui, est là dès l'entrée).
const lazy = (load) => defineAsyncComponent({ loader: load, loadingComponent: Loading, delay: 150 });
const ConfigPanel = lazy(() => import("./config/ConfigPanel.vue"));
const StatsView = lazy(() => import("./stats/StatsView.vue"));
const UsageView = lazy(() => import("./stats/UsageView.vue"));
const SetupPanel = lazy(() => import("./config/SetupPanel.vue"));
const REFRESH = 5000;
const STORE_VIEW = "llm-proxy-view";

export default {
  components: { ConfigPanel, StatsView, UsageView, SetupPanel },
  setup() {
    // ── vues ────────────────────────────────────────────────────────────
    const views = [{ id: "stats", label: "Statistiques" }, { id: "usage", label: "Usage" },
                   { id: "chat", label: "Chat" }, { id: "config", label: "Configuration" }];
    const known = (id) => views.some((v) => v.id === id);
    // L'adresse : #<vue>, et pour le chat #chat/<id de la conversation>,
    // qu'un rechargement rouvre.
    const fromHash = () => {
      const [v, conv] = location.hash.slice(1).split("/");
      return { view: known(v) ? v : null, conv: conv ? decodeURIComponent(conv) : null };
    };
    // La dernière vue ouverte, gardée dans ce navigateur.
    const lastView = VueUse.useLocalStorage(STORE_VIEW, "stats");
    const view = ref(fromHash().view || (known(lastView.value) ? lastView.value : "stats"));
    // Les vues déjà ouvertes : montées une fois, gardées ensuite.
    const visited = reactive({ [view.value]: true });
    watch(view, (v) => { visited[v] = true; });
    const navOpen = ref(false);      // le menu latéral, sur mobile
    // Le menu latéral en rail d'icônes (grand écran), choix de ce navigateur.
    const navCompact = VueUse.useLocalStorage("llm-proxy-nav-compact", false);
    function setView(id) {
      navOpen.value = false;
      if (id === view.value || !known(id)) return;
      view.value = id;
      lastView.value = id;
    }

    // Les conversations du chat, listées dans le menu latéral.
    const chats = ChatStore;
    const chatState = chats.state;
    if (fromHash().conv) chats.want(fromHash().conv);
    // La vue et la conversation affichées, écrites dans l'adresse (sans
    // entrée d'historique) ; une adresse changée à la main est suivie.
    function writeHash() {
      const v = view.value, conv = chatState.currentId;
      // La liste pas encore là : l'id de l'adresse reste en attendant.
      if (v === "chat" && !conv && fromHash().view === "chat") return;
      const hash = "#" + v + (v === "chat" && conv ? "/" + encodeURIComponent(conv) : "");
      if (location.hash !== hash) history.replaceState(null, "", location.pathname + location.search + hash);
    }
    watch([view, () => chatState.currentId], writeHash, { immediate: true });
    window.addEventListener("hashchange", () => {
      const { view: v, conv } = fromHash();
      if (v) setView(v);
      if (conv) chats.want(conv);
      writeHash();
    });
    const chatList = chats.filtered;
    // La fin du menu en vue : sa page suivante.
    const moreRef = ref(null);
    VueUse.useIntersectionObserver(moreRef, ([entry]) => {
      if (entry && entry.isIntersecting) chats.loadMore();
    });
    function openChat(id) { chats.select(id); setView("chat"); navOpen.value = false; }
    function newChat() { chats.newConv(); setView("chat"); navOpen.value = false; }

    // ── session et entrée ───────────────────────────────────────────────
    const session = ref(null);       // /ui/session ; null tant qu'inconnu
    // La version en bas du menu mène à son dépôt, à cet état : le tag
    // d'une version publiée (vAAAA.MM.JJ…), le commit d'un sha-xxxxxxx,
    // sinon la racine.
    const versionLink = computed(() => {
      const { version = "", source = "" } = session.value || {};
      if (/^v\d/.test(version)) return `${source}/tree/${version}`;
      if (/^sha-[0-9a-f]+$/.test(version)) return `${source}/commit/${version.slice(4)}`;
      return source;
    });
    const needLogin = ref(false);
    const loginKey = ref("");
    const loginError = ref("");
    const loginBusy = ref(false);
    // L'entrée : «oidc» (bouton vers le fournisseur) ou «key» (/ui/auth).
    const authMode = ref(null);
    const authLabel = ref("");
    // Un compte OIDC en attente ou bloqué : son état, et rien d'autre.
    const inactive = ref(null);
    // Premier démarrage, sans configuration : la page d'installation seule.
    const setup = ref(false);

    async function loadSession() {
      try {
        const r = await fetch("/ui/session", { headers: { accept: "application/json" } });
        if (r.status === 401) { await loadAuth(); needLogin.value = true; session.value = null; return; }
        if (!r.ok) throw new Error("HTTP " + r.status);
        const s = await r.json();
        needLogin.value = false;
        setup.value = !!s.setup;
        if (s.setup) { session.value = null; return; }
        if (s.account && !["admin", "user"].includes(s.account.role)) {
          inactive.value = s.account;
          session.value = null;
          return;
        }
        inactive.value = null;
        session.value = s;
      } catch (e) {
        // Proxy injoignable : réessayé dans 5 s.
        setTimeout(loadSession, REFRESH);
      }
    }

    async function loadAuth() {
      if (authMode.value) return;
      try {
        const a = await (await fetch("/ui/auth", { headers: { accept: "application/json" } })).json();
        authMode.value = a.mode || "key";
        authLabel.value = a.label || "Se connecter";
      } catch { authMode.value = "key"; }
    }

    async function login() {
      if (!loginKey.value.trim() || loginBusy.value) return;
      loginBusy.value = true;
      loginError.value = "";
      try {
        const r = await fetch("/ui/login", {
          method: "POST",
          headers: { "content-type": "application/json", accept: "application/json" },
          body: JSON.stringify({ key: loginKey.value.trim() }),
        });
        if (r.status === 401) { loginError.value = "Clé refusée."; return; }
        if (!r.ok) { loginError.value = "Le proxy a répondu " + r.status + "."; return; }
        loginKey.value = "";
        await loadSession();
        loadHealth();
      } catch (e) {
        loginError.value = "Proxy injoignable.";
      } finally {
        loginBusy.value = false;
      }
    }

    async function logout() {
      try {
        await fetch("/ui/logout", { method: "POST",
          headers: { "content-type": "application/json" }, body: "{}" });
      } catch { /* le cookie partira au prochain 401 */ }
      session.value = null;
      inactive.value = null;
      await loadAuth();
      needLogin.value = true;
    }

    // Une vue a reçu un 401 (clé révoquée, cookie expiré) : l'entrée.
    function onUnauthorized() { needLogin.value = true; session.value = null; loadAuth(); }

    // ── état du déploiement ─────────────────────────────────────────────
    // /ui/health : /healthz détaillé, sous la session de la page. Un échec
    // passager garde le dernier état connu (pas de clignotement).
    const health = ref(null);
    function loadHealth() {
      fetch("/ui/health", { headers: { accept: "application/json" } })
        .then((r) => (r.ok ? r.json() : null))
        .then((h) => { if (h || !health.value) health.value = h; })
        .catch(() => {});
    }

    // ── thème ───────────────────────────────────────────────────────────
    const { theme, themeLabel, cycleTheme } = useTheme();

    function onKey(e) {
      if (e.key === "Escape" && navOpen.value) navOpen.value = false;
    }

    // L'application installable : son service worker (/ui/sw.js), qui ne
    // garde rien. Refusé hors HTTPS (sauf localhost) : sans effet alors.
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/ui/sw.js", { scope: "/ui" }).catch(() => {});
    }

    let timer = 0;
    onMounted(async () => {
      // Le retour d'une connexion OIDC refusée : dit sur l'écran d'entrée,
      // puis retiré de la barre d'adresse. Une «?key=» n'ouvre rien : retirée
      // aussi.
      const params = new URLSearchParams(location.search);
      if (params.has("auth_error")) loginError.value = params.get("auth_error");
      if (params.has("key") || params.has("auth_error")) {
        history.replaceState(null, "", location.pathname + location.hash);
      }
      await loadSession();
      // Installation : aucun backend à sonder.
      if (!setup.value) loadHealth();
      // /ui/health relu de temps en temps : le cache web bouge.
      timer = setInterval(() => { if (!document.hidden && session.value) loadHealth(); }, 6 * REFRESH);
      window.addEventListener("keydown", onKey);
    });
    onUnmounted(() => {
      clearInterval(timer);
      window.removeEventListener("keydown", onKey);
    });

    return { views, view, visited, setView, navOpen, navCompact, chats, chatState, chatList, moreRef, openChat,
             newChat, session, needLogin, loginKey, loginError,
             loginBusy, login, logout, onUnauthorized, authMode, authLabel,
             inactive, setup, loadSession, health, theme, themeLabel, cycleTheme, versionLink };
  },
};
</script>

<template>
<div class="shell" :class="{ 'has-nav': session, 'nav-open': navOpen, 'nav-compact': navCompact }">
  <aside class="sidenav" v-if="session" aria-label="Navigation">
    <div class="nav-top">
      <div class="brand"><span class="dot"></span><span class="brand-name">llm-proxy</span></div>
      <button type="button" class="nav-collapse" @click="navCompact = !navCompact" :aria-pressed="String(navCompact)"
              :title="navCompact ? 'Déplier le menu' : 'Menu compact : les icônes seules'" aria-label="Menu compact">
        <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 4h16v16H4zM9 4v16"/><path :d="navCompact ? 'M13 10l2 2-2 2' : 'M16 10l-2 2 2 2'"/></svg></button>
    </div>
    <button type="button" class="nav-item" :aria-current="view === 'chat' ? 'page' : null"
            @click="setView('chat')" title="Chat">
      <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16v11H9l-5 4z"/></svg>
      <span>Chat</span></button>
    <div class="nav-chats" v-if="session.chat && session.chat.enabled">
      <button type="button" class="nav-new" :disabled="chatState.streaming" @click="newChat" title="Nouvelle conversation">
        <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>
        <span>Nouvelle conversation</span></button>
      <input type="search" class="nav-search" v-model="chatState.query"
             v-if="chats.sorted.value.length > 1 || chatState.total > 1"
             placeholder="Rechercher…" aria-label="Rechercher dans les conversations">
      <p class="nav-none dim" v-if="chatState.searching" role="status">Recherche…</p>
      <p class="nav-none dim" v-else-if="chatState.query && !chatList.some((c) => c.id !== chatState.currentId)">
        Aucune autre conversation ne contient « {{ chatState.query }} ».</p>
      <ul :aria-busy="String(chatState.listing)">
        <!-- La première page du menu arrive : des lignes vides à sa place. -->
        <template v-if="chatState.listing && chatList.length < 2">
          <li v-for="n in 5" :key="'s' + n" class="conv-skeleton" aria-hidden="true"><i></i><i></i></li>
        </template>
        <li v-for="c in chatList" :key="c.id"
            :class="{ current: view === 'chat' && c.id === chatState.currentId }">
          <input v-if="chatState.renaming === c.id" v-model="chatState.renameText" class="rename"
                 @keydown.enter.prevent="chats.endRename(c)" @keydown.esc="chatState.renaming = null"
                 @blur="chats.endRename(c)" maxlength="120">
          <button v-else type="button" class="conv"
                  :aria-current="view === 'chat' && c.id === chatState.currentId ? 'page' : null"
                  :disabled="chatState.streaming && c.id !== chatState.currentId"
                  @click="openChat(c.id)" @dblclick="chats.startRename(c)" :title="c.title">
            <span class="title"><span class="tag-incognito" v-if="c.incognito">incognito</span>{{ c.title }}</span>
            <span class="dim">{{ chats.when(c.updated) }} · {{ chats.count(c) }} msg</span>
          </button>
          <span class="conv-actions" v-if="chatState.renaming !== c.id">
            <button type="button" class="icon" title="Exporter en Markdown" :disabled="!chats.count(c)"
                    @click="chats.exportMd(c)">⤓</button>
            <button type="button" class="icon" title="Renommer" @click="chats.startRename(c)">✎</button>
            <button type="button" class="icon danger" :class="{ armed: chatState.confirming === c.id }"
                    :title="chatState.confirming === c.id ? 'Cliquer encore pour supprimer' : 'Supprimer'"
                    :disabled="chatState.streaming && c.id === chatState.currentId"
                    @click="chats.remove(c.id)">{{ chatState.confirming === c.id ? "Supprimer ?" : "×" }}</button>
          </span>
        </li>
        <!-- Au bout de la liste : la page suivante, chargée en y arrivant. -->
        <li class="nav-more" ref="moreRef" :key="chatState.more" v-if="chatState.more !== null && !chatState.query">
          <button type="button" class="link" :disabled="chatState.loadingMore" @click="chats.loadMore()">
            {{ chatState.loadingMore ? "Chargement…" : "Plus de conversations" }}</button>
        </li>
      </ul>
    </div>
    <div class="nav-bottom">
      <!-- Les vues et le thème : une ligne d'icônes, le nom en bulle (au
           survol) et en nom accessible. -->
      <div class="nav-icons">
        <button type="button" class="nav-item" :aria-current="view === 'stats' ? 'page' : null"
                @click="setView('stats')" title="Statistiques" aria-label="Statistiques">
          <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/></svg></button>
        <button type="button" class="nav-item" :aria-current="view === 'usage' ? 'page' : null"
                @click="setView('usage')" title="Usage" aria-label="Usage">
          <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 18a8 8 0 1 1 16 0"/><path d="M12 18l4-6"/></svg></button>
        <button type="button" class="nav-item" :aria-current="view === 'config' ? 'page' : null"
                @click="setView('config')" title="Configuration" aria-label="Configuration">
          <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M16 4v4M10 10v4M18 16v4"/></svg></button>
        <button type="button" class="nav-item" @click="cycleTheme" :aria-label="'Changer de thème (actuel : ' + themeLabel + ')'"
                :title="'Thème : ' + themeLabel + ' — cliquer pour changer (auto, clair, sombre)'">
          <svg class="ico" viewBox="0 0 24 24" aria-hidden="true" v-if="theme === 'dark'"><path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/></svg>
          <svg class="ico" viewBox="0 0 24 24" aria-hidden="true" v-else-if="theme === 'light'"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>
          <svg class="ico" viewBox="0 0 24 24" aria-hidden="true" v-else><circle cx="12" cy="12" r="8"/><path d="M12 4v16a8 8 0 0 0 0-16z"/></svg>
        </button>
      </div>
      <div class="nav-foot" v-if="session.account">
        <span :title="session.account.email">{{ session.account.name || session.account.email }}
          <span class="tag" v-if="session.account.role === 'admin'">admin</span></span>
        <button type="button" class="link" @click="logout">Se déconnecter</button>
      </div>
      <div class="nav-foot" v-else-if="session.auth_required">
        <span>clé <code title="condensé de la clé connectée">{{ session.client }}</code></span>
        <button type="button" class="link" @click="logout">Se déconnecter</button>
      </div>
      <a class="nav-version" v-if="session.version && session.source" :href="versionLink"
         target="_blank" rel="noopener noreferrer" title="Le dépôt de llm-proxy, à cette version">llm-proxy {{ session.version }}</a>
      <div class="nav-version" v-else-if="session.version" title="Version de llm-proxy">llm-proxy {{ session.version }}</div>
    </div>
  </aside>
  <div class="scrim" v-if="navOpen" @click="navOpen = false"></div>

<div class="wrap" :class="{ 'is-chat': session && view === 'chat', 'is-stats': session && view === 'stats' }">
  <header>
    <div class="mobilebar">
      <button type="button" class="icon menu-btn" v-if="session" @click="navOpen = true"
              aria-label="Menu" title="Menu">
        <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h16"/></svg>
      </button>
      <div class="brand"><span class="dot"></span> llm-proxy</div>
    </div>
    <h1 v-if="setup">Installation</h1>
    <h1 v-else-if="needLogin || inactive">Connexion</h1>
    <h1 v-else-if="view === 'stats'">Statistiques d'usage</h1>
    <h1 v-else-if="view === 'usage'">Usage et limites</h1>
    <h1 v-else-if="view === 'config'">Configuration</h1>
  </header>

  <div class="panel login" v-if="needLogin && authMode === 'oidc'">
    <p>Connectez-vous avec votre compte : vous y gérez <b>vos</b> clés pour
      vos clients, et ne voyez que <b>vos</b> statistiques et <b>vos</b> conversations.</p>
    <div class="login-bar">
      <a class="btn" href="/ui/auth/login">{{ authLabel }}</a>
      <span class="err" role="status">{{ loginError }}</span>
    </div>
  </div>

  <div class="panel login" v-if="inactive">
    <p v-if="inactive.role === 'pending'">Compte <b>{{ inactive.email || inactive.name }}</b>
      créé : il attend qu'un admin l'accepte. Revenez ensuite sur cette page.</p>
    <p v-else>Le compte <b>{{ inactive.email || inactive.name }}</b> est bloqué.
      Voyez avec un admin du proxy.</p>
    <div class="login-bar">
      <button type="button" class="btn" @click="loadSession">Vérifier à nouveau</button>
      <button type="button" class="btn ghost" @click="logout">Se déconnecter</button>
    </div>
  </div>

  <form class="panel login" v-if="needLogin && authMode !== 'oidc'" @submit.prevent="login">
    <p>Ce proxy demande une clé. Elle reste dans un cookie de ce navigateur,
      illisible par la page ; vous ne verrez que <b>vos</b> statistiques
      et <b>vos</b> conversations.</p>
    <label class="field">
      <span>Clé du proxy</span>
      <input type="password" v-model="loginKey" autocomplete="current-password"
             spellcheck="false" placeholder="sk-…" autofocus>
    </label>
    <div class="login-bar">
      <button type="submit" class="btn" :disabled="loginBusy || !loginKey.trim()">Entrer</button>
      <span class="err" role="status">{{ loginError }}</span>
    </div>
  </form>

  <div class="boot" role="status" v-if="!session && !needLogin && !inactive && !setup">
    <span class="spinner" aria-hidden="true"></span> Chargement…</div>

  <setup-panel v-if="setup"></setup-panel>

  <!-- Le chat reste monté d'une vue à l'autre : une réponse en cours
       continue pendant qu'on regarde les chiffres. -->
  <chat-panel v-if="session" v-show="view === 'chat'" :session="session"
              :key="session.client || 'open'" @unauthorized="onUnauthorized"></chat-panel>

  <config-panel v-if="session && visited.config" v-show="view === 'config'" :session="session"
                :health="health"></config-panel>

  <stats-view v-if="session && visited.stats" v-show="view === 'stats'" :session="session" :health="health"
              :active="view === 'stats'" @unauthorized="onUnauthorized"></stats-view>

  <usage-view v-if="session && visited.usage" v-show="view === 'usage'"
              :active="view === 'usage'" @unauthorized="onUnauthorized"></usage-view>
</div>
</div>
</template>
