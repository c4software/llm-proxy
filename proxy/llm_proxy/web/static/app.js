/*
  L'application de la page : son SQUELETTE. Le gabarit est celui de la
  page (templates/index.html) ; chaque vue est un composant à part, dans
  son dossier, avec ses propres appels :

    stats/StatsView.js       les statistiques d'usage
    chat/ChatPanel.js        le chat (chat/store.js : les conversations)
    config/ConfigPanel.js    la configuration (clients, clés, comptes,
                             mémoire, proxy)
    memory/MemoryPanel.js    la mémoire de l'outil `memory`
    lib/                     appels, formats, fichiers, Markdown, thème

  Ici : la SESSION et l'entrée, les VUES et le menu latéral.

  La page lit d'abord /ui/session : auth active sans clé valide (401), elle
  montre l'écran d'entrée — la clé saisie part à /ui/login, qui ouvre une
  session signée (cookie HttpOnly) ; avec les comptes OIDC (/ui/auth dit
  «oidc»), un bouton mène au fournisseur (/ui/auth/login), et un compte en
  attente ou bloqué ne voit que son état. Une fois entrée, la personne ne
  voit que SES requêtes et SES conversations.

  Trois vues, menu latéral (tiroir sur mobile) : « Statistiques », « Chat »
  et « Configuration », mémorisées dans l'URL (#chat, #config) et le
  navigateur. Le chat reste monté : une réponse en cours continue pendant
  qu'on regarde les chiffres.

  /ui/health (l'état détaillé du déploiement) est lu ici, au chargement
  puis toutes les 30 s, et passé aux vues qui en ont besoin.
*/
import { ChatStore } from "./chat/store.js";
import { ChatPanel } from "./chat/ChatPanel.js";
import { ConfigPanel } from "./config/ConfigPanel.js";
import { MemoryPanel } from "./memory/MemoryPanel.js";
import { StatsView } from "./stats/StatsView.js";
import { useTheme } from "./lib/theme.js";

const { createApp, ref, onMounted, onUnmounted } = Vue;
const REFRESH = 5000;
const STORE_VIEW = "llm-proxy-view";

createApp({
  setup() {
    // ── vues ────────────────────────────────────────────────────────────
    const views = [{ id: "stats", label: "Statistiques" }, { id: "chat", label: "Chat" },
                   { id: "config", label: "Configuration" }];
    const known = (id) => views.some((v) => v.id === id);
    const storedView = () => {
      if (known(location.hash.slice(1))) return location.hash.slice(1);
      try { const v = localStorage.getItem(STORE_VIEW); return known(v) ? v : "stats"; }
      catch { return "stats"; }
    };
    const view = ref(storedView());
    const navOpen = ref(false);      // le menu latéral, sur mobile
    function setView(id) {
      navOpen.value = false;
      if (id === view.value || !known(id)) return;
      view.value = id;
      try { localStorage.setItem(STORE_VIEW, id); } catch { /* sans stockage */ }
      history.replaceState(null, "", location.pathname + location.search + "#" + id);
    }

    // Les conversations du chat, listées dans le menu latéral.
    const chats = ChatStore;
    const chatState = chats.state;
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
      // puis retiré de la barre d'adresse. Une «?key=» d'un ancien favori
      // n'ouvre plus rien : retirée aussi.
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

    return { views, view, setView, navOpen, chats, chatState, chatList, openChat,
             newChat, session, needLogin, loginKey, loginError,
             loginBusy, login, logout, onUnauthorized, authMode, authLabel,
             inactive, loadSession, health, theme, themeLabel, cycleTheme };
  },
}).component("memory-panel", MemoryPanel)
  .component("chat-panel", ChatPanel)
  .component("config-panel", ConfigPanel)
  .component("stats-view", StatsView)
  .mount("#app");
