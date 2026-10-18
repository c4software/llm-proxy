/*
  L'onglet « Configuration » : un sélecteur de PARTIES, chacune un
  composant à part, dans ce dossier :

    ClientsPart.js    les fichiers à poser pour brancher un client
    KeysPart.js       ses clés (comptes OIDC)
    ../memory/        ce que l'outil `memory` a gardé (si actif)
    AccountsPart.js   les comptes et leur part, la sauvegarde (admin)
    ProxyPart.js      toute la configuration du proxy (admin, ou proxy
                      sans comptes) — proxy/form.js, proxy/advanced.js
    shared.js         ce qu'elles partagent : TOML, clé du poste,
                      catalogue des modèles, comptes

  Rien n'est jamais écrit sur le proxy : la page produit des fichiers.
  La partie choisie est gardée dans le navigateur.
*/
import { ClientsPart } from "./ClientsPart.js";
import { KeysPart } from "./KeysPart.js";
import { AccountsPart } from "./AccountsPart.js";
import { ProxyPart } from "./ProxyPart.js";
import { useAccount, useUsers } from "./shared.js";

const { ref, computed, onMounted } = Vue;
const STORE_PART = "llm-proxy-config-part";

export const ConfigPanel = {
  components: { ClientsPart, KeysPart, AccountsPart, ProxyPart },
  props: {
    session: { type: Object, required: true },
    health: { type: Object, default: null },
  },
  setup(props) {
    const { account, isAdmin } = useAccount(props);
    const { pendingCount, loadUsers } = useUsers();
    const parts = computed(() => [
      { id: "clients", label: "Clients" },
      ...(account.value ? [{ id: "keys", label: "Clés" }] : []),
      ...(props.session.memory ? [{ id: "memory", label: "Mémoire" }] : []),
      ...(isAdmin.value ? [{ id: "accounts", label: "Comptes" }] : []),
      ...(!account.value || isAdmin.value ? [{ id: "proxy", label: "Proxy" }] : []),
    ]);
    const stored = ref((() => {
      try { return localStorage.getItem(STORE_PART) || "clients"; }
      catch { return "clients"; }
    })());
    // « settings », encore gardé par un navigateur : la partie Proxy.
    const part = computed(() => {
      const want = stored.value === "settings" ? "proxy" : stored.value;
      return parts.value.some((p) => p.id === want) ? want : "clients";
    });
    function setPart(p) {
      stored.value = p;
      try { localStorage.setItem(STORE_PART, p); } catch { /* sans stockage */ }
    }
    // Admin : le nombre de comptes en attente, sur le sélecteur.
    onMounted(() => { if (isAdmin.value) loadUsers(); });

    return { parts, part, setPart, pendingCount };
  },
  template: `
  <div class="config-view">
    <div class="toolbar">
      <div class="range" role="group" aria-label="Partie">
        <button type="button" v-for="p in parts" :key="p.id" :aria-pressed="String(part === p.id)"
                @click="setPart(p.id)">{{ p.label }}<template
                v-if="p.id === 'accounts' && pendingCount"> ({{ pendingCount }})</template></button>
      </div>
    </div>
    <clients-part v-if="part === 'clients'" :session="session" :health="health"
                  @goto="setPart"></clients-part>
    <keys-part v-else-if="part === 'keys'" :session="session" @goto="setPart"></keys-part>
    <div class="config-keys" v-else-if="part === 'memory'">
      <memory-panel expanded></memory-panel>
    </div>
    <accounts-part v-else-if="part === 'accounts'" :session="session"></accounts-part>
    <proxy-part v-else-if="part === 'proxy'" :session="session"></proxy-part>
  </div>`,
};
