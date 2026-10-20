/*
  Le panneau TERMINAL, à droite du fil (comme celui des artifacts) : la VM
  Linux de la conversation (tools/sandbox/linuxvm.js), celle où run_shell
  exécute les scripts du modèle — bash dans /work, sans réseau, tout dans
  le navigateur. Le terminal est le cadre isolé de la VM, posé par-dessus
  le corps du panneau : fermer le panneau ne l'arrête pas. « Réseau »
  l'ouvre à internet pour CETTE conversation (`conv.linuxNetwork`), après
  un avertissement, comme pour un artifact : la VM redémarre, /work gardé.
  Une seule VM pour la page : quand une AUTRE conversation s'en sert (sa
  réponse continue en arrière-plan, et son modèle appelle run_shell), le
  panneau ne montre pas sa machine — il le dit, et propose de la reprendre.
  Largeur, Large, Réseau et son avertissement : SidePanel.js, commun avec
  les artifacts (pas de plein écran : le cadre de la VM est hors du panneau).
*/
import * as linux from "./tools/sandbox/linuxvm.js";
import { SidePanel } from "./SidePanel.js";
import { download } from "../lib/files.js";

const { ref, computed, watch, onMounted, onUnmounted } = Vue;

export const LinuxPanel = {
  components: { SidePanel },
  props: {
    conv: { type: Object, required: true },
  },
  emits: ["close"],
  setup(props) {
    const body = ref(null);
    const vm = linux.vm;
    const note = ref("");
    const wiping = ref(false);             // « Repartir de zéro » demande confirmation
    async function network(on) {
      note.value = "";
      try {
        await linux.setNetwork(props.conv, on);
      } catch (e) {
        note.value = e.message;
      }
    }
    const label = computed(() => ({
      off: "arrêtée", loading: "téléchargement de la VM…", starting: "démarrage…",
      ready: vm.typing ? "le modèle tape dans le terminal…" : vm.busy ? "le modèle exécute un script…" : "", failed: "en échec" }[vm.status] || ""));

    async function boot() {
      note.value = "";
      try {
        await linux.use(props.conv);
        linux.focus();
      } catch (e) {
        note.value = e.message;
      }
    }
    // La machine montrée : celle de cette conversation, pas une autre.
    const elsewhere = computed(() => !!vm.owner && vm.owner !== props.conv.id);
    watch(elsewhere, (away) => { if (away) linux.detach(); else if (body.value) linux.attach(body.value); });
    onMounted(() => { if (!elsewhere.value) linux.attach(body.value); boot(); });
    onUnmounted(() => { linux.detach(); linux.save().catch(() => {}); });
    // Une autre conversation : sa VM à elle (celle d'avant garde son /work).
    watch(() => props.conv.id, (id, old) => { if (id !== old) boot(); });

    async function restart(wipe) {
      note.value = "";
      wiping.value = false;
      try {
        await linux.restart(props.conv, { wipe });
        linux.focus();
      } catch (e) {
        note.value = e.message;
      }
    }
    async function stop() {
      await linux.stop();
      note.value = "VM arrêtée, /work gardé.";
    }
    async function saveWork() {
      try {
        const bytes = await linux.archive();
        download({ name: "work.tar.gz", content: bytes }, "application/gzip");
      } catch (e) {
        note.value = e.message;
      }
    }
    const running = computed(() => vm.status === "ready" && vm.owner === props.conv.id);
    return { body, vm, note, wiping, label, running, elsewhere, boot, restart, stop, saveWork, network };
  },
  template: `
  <side-panel class="linux-side" storage="llm-proxy-terminal-width" :fullscreen="false" :network="!!conv.linuxNetwork"
              :subject="conv.id" :network-titles="['Réseau ouvert à cette VM : couper (la VM redémarre, /work gardé)', 'Réseau coupé : l’ouvrir à cette VM']"
              @network="network" @close="$emit('close')">
    <template #head>
      <h3 title="Une VM Linux (Alpine) dans le navigateur : la même que celle du modèle (run_shell)">Terminal Linux</h3>
      <span class="tag" v-if="label">{{ label }}</span>
    </template>
    <template #actions>
      <button type="button" class="btn ghost art-btn" :disabled="!running" @click="saveWork"
              title="Télécharger /work (work.tar.gz)" aria-label="Télécharger /work">
        <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg></button>
    </template>
    <template #actions-end>
      <button type="button" class="btn ghost art-btn" :disabled="vm.status === 'loading' || vm.status === 'starting'"
              @click="restart(false)" title="Redémarrer la VM (/work gardé)" aria-label="Redémarrer">
        <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 12a8 8 0 1 1-2.3-5.7M20 4v5h-5"/></svg></button>
      <button type="button" class="btn ghost art-btn" v-if="running" @click="stop"
              title="Arrêter la VM (/work gardé) : libère sa mémoire" aria-label="Arrêter">
        <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M7 7h10v10H7z"/></svg></button>
    </template>
    <template #network-warning>
      <b>Ouvrir le réseau à cette VM ?</b> Le code qui y tourne — écrit par le modèle ou par
      vous — pourra télécharger, et envoyer ce que contient /work à n'importe quel site qui
      l'accepte.
      <template v-if="vm.relay">Le trafic passe par le proxy : un vrai accès à Internet (https,
        apk, pip, git, ssh), sauf vers les réseaux privés ; il part de l'adresse du serveur.</template>
      <template v-else>Le trafic passe par votre navigateur : des requêtes web (HTTP, faites en
        HTTPS) vers les sites qui l'autorisent (API publiques, PyPI, GitHub…) ; ni ssh, ni apk,
        ni pip.</template>
      La VM redémarre, /work gardé.
    </template>
    <p class="linux-note" v-if="elsewhere" role="status">
      Une autre conversation se sert de la VM (son modèle y travaille) : ce terminal n'est pas le sien.
      <button type="button" class="btn ghost" @click="boot">La reprendre pour celle-ci</button>
    </p>
    <p class="linux-note" v-else-if="note || vm.status === 'failed' || vm.note || vm.saved" role="status">
      {{ note || (vm.status === 'failed' ? vm.error : vm.note || vm.saved) }}
      <button type="button" class="btn ghost" v-if="vm.status === 'off' || vm.status === 'failed'" @click="boot">Démarrer</button>
      <template v-if="running">
        <button type="button" class="btn ghost" v-if="!wiping" @click="wiping = true"
                title="Une machine neuve et un /work vide">Repartir de zéro</button>
        <template v-else>
          <button type="button" class="btn danger" @click="restart(true)">Vider /work et redémarrer</button>
          <button type="button" class="btn ghost" @click="wiping = false">Annuler</button>
        </template>
      </template>
    </p>
    <div class="linux-term" ref="body"></div>
  </side-panel>`,
};
