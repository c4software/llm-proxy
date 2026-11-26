/*
  L'onglet « Configuration » : un sélecteur de PARTIES, chacune un
  composant à part, dans ce dossier :

    ClientsPart.js    les fichiers à poser pour brancher un client
    KeysPart.js       ses clés (comptes OIDC)
    WebhooksPart.js   ses webhooks entrants et sortants (si actifs)
    ReposPart.js      ses sources de dépôts (si [repos] est actif)
    BackendsPart.js   ses backends (si [user_backends] est actif)
    RuntimePart.js    les bacs de l'exécuteur (les siens ; tous pour un
                      admin) et les caches du proxy (admin), à nettoyer
    ../memory/        ce que l'outil `memory` a gardé (si actif)
    QuotasPart.js     l'état du limiteur des backends à quotas (admin, ou
                      proxy sans comptes)
    ExtensionsPart.js les extensions chargées, à éteindre ou rallumer
                      (admin, ou proxy sans comptes)
    PromptsPart.js    les textes que le modèle lit, à modifier ou
                      rétablir (admin, ou proxy sans comptes)
    AccountsPart.js   les comptes, leur part et leurs modèles permis, la
                      sauvegarde (admin)
    ProxyPart.js      toute la configuration du proxy (admin, ou proxy
                      sans comptes) — proxy/form.js, proxy/advanced.js
    shared.js         ce qu'elles partagent : TOML, clé du poste,
                      catalogue des modèles, comptes

  La configuration du proxy n'est jamais écrite par la page : elle
  produit des fichiers. Seuls l'interrupteur des extensions et les prompts
  modifiés sont gardés par le proxy (data/extensions.json, prompts.json).
  La partie choisie est gardée dans le navigateur.
*/
import { ClientsPart } from "./ClientsPart.js";
import { KeysPart } from "./KeysPart.js";
import { WebhooksPart } from "./WebhooksPart.js";
import { ReposPart } from "./ReposPart.js";
import { BackendsPart } from "./BackendsPart.js";
import { RuntimePart } from "./RuntimePart.js";
import { ExtensionsPart } from "./ExtensionsPart.js";
import { PromptsPart } from "./PromptsPart.js";
import { QuotasPart } from "./QuotasPart.js";
import { AccountsPart } from "./AccountsPart.js";
import { ProxyPart } from "./ProxyPart.js";
import { useAccount, useUsers } from "./shared.js";

const { computed, onMounted, ref } = Vue;
const STORE_PART = "llm-proxy-config-part";

export const ConfigPanel = {
  components: { ClientsPart, KeysPart, WebhooksPart, ReposPart, BackendsPart, RuntimePart, ExtensionsPart, PromptsPart, QuotasPart, AccountsPart, ProxyPart },
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
      ...(props.session.webhooks ? [{ id: "webhooks", label: "Webhooks" }] : []),
      ...(props.session.repos ? [{ id: "repos", label: "Dépôts" }] : []),
      ...(props.session.backends ? [{ id: "backends", label: "Backends" }] : []),
      ...(props.session.executor ? [{ id: "runtime", label: "Exécution" }] : []),
      ...(!account.value || isAdmin.value ? [{ id: "quotas", label: "Quotas" }] : []),
      ...(!account.value || isAdmin.value ? [{ id: "extensions", label: "Extensions" }] : []),
      ...(!account.value || isAdmin.value ? [{ id: "prompts", label: "Prompts" }] : []),
      ...(isAdmin.value ? [{ id: "accounts", label: "Comptes" }] : []),
      ...(!account.value || isAdmin.value ? [{ id: "proxy", label: "Proxy" }] : []),
    ]);
    const stored = VueUse.useLocalStorage(STORE_PART, "clients");
    // « settings », encore gardé par un navigateur : la partie Proxy.
    const part = computed(() => {
      const want = stored.value === "settings" ? "proxy" : stored.value;
      return parts.value.some((p) => p.id === want) ? want : "clients";
    });
    function setPart(p) {
      stored.value = p;
    }
    // Sur un téléphone, les parties défilent dans leur pastille : la partie
    // ouverte y est amenée en vue.
    const tabs = ref(null);
    onMounted(() => {
      // Admin : le nombre de comptes en attente, sur le sélecteur.
      if (isAdmin.value) loadUsers();
      const on = tabs.value && tabs.value.querySelector('[aria-pressed="true"]');
      if (on) {
        const box = tabs.value.getBoundingClientRect(), b = on.getBoundingClientRect();
        tabs.value.scrollLeft += b.left - box.left - (box.width - b.width) / 2;
      }
    });

    return { parts, part, setPart, pendingCount, tabs };
  },
  template: `
  <div class="config-view">
    <div class="toolbar">
      <div class="range" ref="tabs" role="group" aria-label="Partie">
        <button type="button" v-for="p in parts" :key="p.id" :aria-pressed="String(part === p.id)"
                @click="setPart(p.id)">{{ p.label }}<template
                v-if="p.id === 'accounts' && pendingCount"> ({{ pendingCount }})</template></button>
      </div>
    </div>
    <clients-part v-if="part === 'clients'" :session="session" :health="health"
                  @goto="setPart"></clients-part>
    <keys-part v-else-if="part === 'keys'" :session="session" @goto="setPart"></keys-part>
    <webhooks-part v-else-if="part === 'webhooks'" :session="session"></webhooks-part>
    <repos-part v-else-if="part === 'repos'"></repos-part>
    <backends-part v-else-if="part === 'backends'" :session="session"></backends-part>
    <runtime-part v-else-if="part === 'runtime'"></runtime-part>
    <div class="config-keys" v-else-if="part === 'memory'">
      <memory-panel expanded></memory-panel>
    </div>
    <quotas-part v-else-if="part === 'quotas'"></quotas-part>
    <extensions-part v-else-if="part === 'extensions'"></extensions-part>
    <prompts-part v-else-if="part === 'prompts'"></prompts-part>
    <accounts-part v-else-if="part === 'accounts'" :session="session"></accounts-part>
    <proxy-part v-else-if="part === 'proxy'" :session="session"></proxy-part>
  </div>`,
};
