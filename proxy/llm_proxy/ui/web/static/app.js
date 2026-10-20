/*
  L'application de la page : la SESSION et l'entrée, les VUES et le menu
  latéral. Gabarit : templates/index.html ; chaque vue est un composant
  dans son dossier (stats/, chat/, config/, memory/) ; lib/ : appels,
  formats, fichiers, Markdown, thème.

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
import { ChatStore } from "./chat/store.js";
import { ChatPanel } from "./chat/ChatPanel.js";
import { ConfigPanel } from "./config/ConfigPanel.js";
import { MemoryPanel } from "./memory/MemoryPanel.js";
import { StatsView } from "./stats/StatsView.js";
import { useTheme } from "./lib/theme.js";

const { createApp, ref, watch, onMounted, onUnmounted } = Vue;
const REFRESH = 5000;
const STORE_VIEW = "llm-proxy-view";

createApp({
  setup() {
    // ── vues ────────────────────────────────────────────────────────────
    const views = [{ id: "stats", label: "Statistiques" }, { id: "chat", label: "Chat" },
                   { id: "config", label: "Configuration" }];
    const known = (id) => views.some((v) => v.id === id);
    // L'adresse : #<vue>, et pour le chat #chat/<id de la conversation>,
    // qu'un rechargement rouvre.
    const fromHash = () => {
      const [v, conv] = location.hash.slice(1).split("/");
      return { view: known(v) ? v : null, conv: conv ? decodeURIComponent(conv) : null };
    };
    const storedView = () => {
      if (fromHash().view) return fromHash().view;
      try { const v = localStorage.getItem(STORE_VIEW); return known(v) ? v : "stats"; }
      catch { return "stats"; }
    };
    const view = ref(storedView());
    const navOpen = ref(false);      // le menu latéral, sur mobile
    // Le menu latéral en rail d'icônes (grand écran), choix de ce navigateur.
    const navCompact = VueUse.useLocalStorage("llm-proxy-nav-compact", false);
    function setView(id) {
      navOpen.value = false;
      if (id === view.value || !known(id)) return;
      view.value = id;
      try { localStorage.setItem(STORE_VIEW, id); } catch { /* sans stockage */ }
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
    function openChat(id) { chats.select(id); setView("chat"); navOpen.value = false; }
    function newChat() { chats.newConv(); setView("chat"); navOpen.value = false; }

    // ── session et entrée ───────────────────────────────────────────────
    const session = ref(null);       // /ui/session ; null tant qu'inconnu
    const needLogin = ref(false);
    const loginKey = ref("");
    const loginError = ref("");
    const loginBusy = ref(false);
    // L'entrée : «oidc» (bouton vers le fournisseur) ou «key» (/ui/auth).
    const authMode = ref(null);
    const authLabel = ref("");
    // Un compte OIDC en attente ou bloqué : son état, et rien d'autre.
    const inactive = ref(null);

    async function loadSession() {
      try {
        const r = await fetch("/ui/session", { headers: { accept: "application/json" } });
        if (r.status === 401) { await loadAuth(); needLogin.value = true; session.value = null; return; }
        if (!r.ok) throw new Error("HTTP " + r.status);
        const s = await r.json();
        needLogin.value = false;
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
      loadHealth();
      // /ui/health relu de temps en temps : le cache web bouge.
      timer = setInterval(() => { if (!document.hidden && session.value) loadHealth(); }, 6 * REFRESH);
      window.addEventListener("keydown", onKey);
    });
    onUnmounted(() => {
      clearInterval(timer);
      window.removeEventListener("keydown", onKey);
    });

    return { views, view, setView, navOpen, navCompact, chats, chatState, chatList, openChat,
             newChat, session, needLogin, loginKey, loginError,
             loginBusy, login, logout, onUnauthorized, authMode, authLabel,
             inactive, loadSession, health, theme, themeLabel, cycleTheme };
  },
}).component("memory-panel", MemoryPanel)
  .component("chat-panel", ChatPanel)
  .component("config-panel", ConfigPanel)
  .component("stats-view", StatsView)
  .mount("#app");
