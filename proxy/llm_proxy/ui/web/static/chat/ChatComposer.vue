<script>
// @ts-check
// Zone de saisie du chat : file d'attente, pièces jointes et dossier ouvert,
// bandeau d'édition, autocomplétion des commandes « / », champ texte et barre
// d'options (Joindre, Micro, Outils, mode Images/Voix, Réflexion, Contexte,
// Envoyer / Arrêter).
import { ref } from "vue";
import { num, size } from "../lib/format.js";
import * as Compact from "./compact.js";

export default {
  props: {
    current: { type: Object, required: true },
    chat: { type: Object, required: true },
    caps: { type: Object, required: true },
    draft: { type: String, default: "" },
    queue: { type: Array, required: true },
    pending: { type: Array, required: true },
    streaming: { type: Boolean, default: false },
    dragging: { type: Boolean, default: false },
    narrow: { type: Boolean, default: false },
    editing: { type: Number, default: null },
    commandHints: { type: Array, required: true },
    canSend: { type: Boolean, default: false },
    status: { type: String, default: "" },
    blind: { type: Boolean, default: false },
    unsent: { type: Boolean, default: false },
    imageMode: { type: Boolean, default: false },
    voiceMode: { type: String, default: "" },
    asrMode: { type: Boolean, default: false },
    genMode: { type: Boolean, default: false },
    imageSizes: { type: Array, required: true },
    imageSteps: { type: Array, required: true },
    imageLoras: { type: Array, required: true },
    voiceChoices: { type: Array, required: true },
    rec: { type: Object, required: true },
    folderReady: { type: Boolean, default: false },
    folderCount: { type: Number, default: 0 },
    canPick: { type: Boolean, default: false },
    attachOpen: { type: Boolean, default: false },
    toolsOpen: { type: Boolean, default: false },
    hostedShown: { type: Array, required: true },
    devicesShown: { type: Array, required: true },
    hostedOn: { type: Function, required: true },
    activeTools: { type: Array, required: true },
    deviceOn: { type: Array, required: true },
    hostedCost: { type: Function, required: true },
    familyCost: { type: Function, required: true },
    toolsCost: { type: Number, default: 0 },
    context: { type: Object, default: null },
    contextTitle: { type: String, default: "" },
    ctxParts: { type: Object, default: null },
    ctxOpen: { type: Boolean, default: false },
    autoCompact: { type: Boolean, default: false },
  },
  emits: [
    "update:draft", "update:attachOpen", "update:toolsOpen", "update:ctxOpen",
    "update:autoCompact", "send", "stop", "resume", "unqueue", "remove-queue",
    "remove-pending", "pick-files", "pick-folder", "pick-folder-files",
    "close-folder", "cancel-edit", "use-command", "keydown", "paste",
    "toggle-record", "toggle-tool", "toggle-device", "show-prompt",
    "new-voice", "compact-now", "set-conv",
  ],
  setup(_, { expose }) {
    const textarea = ref(null);
    expose({ textarea });
    return { textarea, num, size, kTokens: Compact.kTokens, AUTO_AT: Compact.AUTO_AT };
  },
};
</script>

<template>
  <form class="composer" :class="{ dragging }" @submit.prevent="$emit('send')">
    <div class="queue" v-if="queue.length">
      <div class="queue-head">
        <span>En file · {{ queue.length }}</span>
        <span class="dim" v-if="!streaming">en pause</span>
        <button type="button" class="link" v-if="!streaming" @click="$emit('resume')">Envoyer la suite</button>
      </div>
      <ol>
        <li v-for="(q, k) in queue" :key="q.id">
          <span class="text">{{ q.content || q.attachments.map((a) => a.name).join(", ") }}</span>
          <span class="dim" v-if="q.content && q.attachments.length">+ {{ q.attachments.length }} pièce{{ q.attachments.length > 1 ? "s" : "" }}</span>
          <button type="button" class="link" @click="$emit('unqueue', k)">Modifier</button>
          <button type="button" class="icon" title="Retirer de la file" @click="$emit('remove-queue', k)">×</button>
        </li>
      </ol>
    </div>
    <div class="attachments" v-if="pending.length || (current && current.folder)">
      <span class="chip folder" v-if="current && current.folder"
            :title="folderReady ? 'Le modèle peut lister, lire et chercher dans ce dossier' : 'Rouvrir le dossier pour que le modèle y accède'">
        <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h6l2 2h10v11H3z"/></svg>
        {{ current.folder.name }}/
        <span class="dim" v-if="folderReady">{{ num(folderCount) }} fichier{{ folderCount > 1 ? "s" : "" }}</span>
        <button type="button" class="link" v-else-if="canPick" @click="$emit('pick-folder')">à rouvrir</button>
        <button type="button" class="link" v-else @click="$emit('pick-folder-files')">à rouvrir</button>
        <button type="button" class="icon" title="Fermer le dossier" @click="$emit('close-folder')">×</button>
      </span>
      <span v-for="(a, k) in pending" :key="k" class="chip" :class="{ file: a.kind !== 'image' }">
        <img v-if="a.kind === 'image'" :src="a.data" alt="">
        {{ a.name }} <span class="dim">{{ size(a.size) }}</span>
        <button type="button" class="icon" title="Retirer" @click="$emit('remove-pending', k)">×</button>
      </span>
    </div>
    <div class="editing-note" v-if="editing !== null">
      <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20h4L19 9l-4-4L4 16zM14 6l4 4"/></svg>
      <span>Modification d'un message : l'envoyer reprend la conversation de lui (la suite disparaît)</span>
      <button type="button" class="link" @click="$emit('cancel-edit')">Annuler <kbd>Échap</kbd></button>
    </div>
    <div class="command-hints" v-if="commandHints.length" role="listbox">
      <button type="button" v-for="c in commandHints" :key="c.name" role="option"
              @mousedown.prevent="$emit('use-command', c)">
        <code>{{ c.name }}</code> <span class="dim">{{ c.args }}</span> <small>{{ c.hint }}</small>
        <kbd>Tab</kbd></button>
    </div>
    <textarea ref="textarea" :value="draft" rows="1"
              @input="$emit('update:draft', /** @type {HTMLTextAreaElement} */ ($event.target).value)"
              @keydown="$emit('keydown', $event)" @paste="$emit('paste', $event)"
              :placeholder="narrow ? (streaming ? 'Réponse en cours… (mise en file)' : imageMode ? 'Décrire l’image…' : voiceMode ? 'Texte à lire…' : asrMode ? 'Contexte (facultatif)…' : 'Écrire un message…')
                : streaming ? 'Réponse en cours — Entrée pour mettre un message en file'
                : imageMode ? 'Décrire l’image à générer — Entrée pour envoyer'
                : voiceMode ? 'Texte à lire à voix haute — Entrée pour envoyer'
                : asrMode ? 'Audio joint ou enregistré ; contexte facultatif (noms propres, vocabulaire) — Entrée pour envoyer'
                : 'Écrire un message — Entrée pour envoyer'"
              title="Entrée pour envoyer, Maj+Entrée pour aller à la ligne"></textarea>
    <div class="composer-bar">
      <div class="attach-menu">
        <button type="button" class="btn ghost attach ico-btn" aria-haspopup="menu"
                :aria-expanded="String(attachOpen)" @click="$emit('update:attachOpen', !attachOpen)"
                title="Joindre : des fichiers (ou glisser-déposer, ou coller), ou un dossier que le modèle lira">
          <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11.5l-8.2 8.2a5 5 0 0 1-7-7L13 4.5a3.3 3.3 0 0 1 4.7 4.7l-8.2 8.2a1.7 1.7 0 0 1-2.4-2.4L14.6 7.5"/></svg>
          <span class="lbl">Joindre</span></button>
        <div class="menu" role="menu" v-show="attachOpen" @click="$emit('update:attachOpen', false)">
          <button type="button" role="menuitem" @click="$emit('pick-files')"><svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 3h9l3 3v15H6z"/></svg>
            <span>Fichiers…<small>{{ asrMode ? "audios : transcrits" : "images, textes : joints au message" }}</small></span></button>
          <button type="button" role="menuitem" v-if="caps.folder && canPick" @click.stop="$emit('pick-folder')"><svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h6l2 2h10v11H3z"/></svg>
            <span>Dossier…<small>le modèle le parcourt, lit ce qu'il lui faut, propose des écritures</small></span></button>
          <button type="button" role="menuitem" v-else-if="caps.folder" @click="$emit('pick-folder-files')"><svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h6l2 2h10v11H3z"/></svg>
            <span>Dossier…<small>le modèle le parcourt, lit ce qu'il lui faut ; écritures téléchargées</small></span></button>
        </div>
      </div>
      <button type="button" class="btn ghost ico-btn mic" v-if="asrMode && rec.supported.value"
              :class="{ rec: rec.state.recording }" @click="$emit('toggle-record')"
              :title="rec.state.recording ? 'Arrêter : l’enregistrement est joint au message' : 'Enregistrer au micro (' + rec.MAX / 60 + ' min au plus)'">
        <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3a3 3 0 0 1 3 3v6a3 3 0 0 1-6 0V6a3 3 0 0 1 3-3zM5 11a7 7 0 0 0 14 0M12 18v3"/></svg>
        <span class="lbl">{{ rec.state.recording ? "Arrêter · " + Math.floor(rec.state.seconds) + " s" : "Micro" }}</span></button>
      <div class="attach-menu tools-menu" v-if="!genMode && (hostedShown.length || devicesShown.length)">
        <button type="button" class="btn ghost attach ico-btn" aria-haspopup="menu"
                :aria-expanded="String(toolsOpen)" @click="$emit('update:toolsOpen', !toolsOpen)"
                :title="activeTools.length ? 'Outils actifs : ' + activeTools.join(', ') : 'Aucun outil actif'">
          <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M14.7 6.3a4 4 0 0 0-5.4 5.2L3 17.8V21h3.2l6.3-6.3a4 4 0 0 0 5.2-5.4l-2.6 2.6-2.4-.6-.6-2.4z"/></svg>
          <span class="lbl">Outils</span><span class="count">{{ activeTools.length }}</span></button>
        <div class="menu" role="menu" v-show="toolsOpen">
          <template v-if="hostedShown.length">
            <p class="menu-head">Proxy</p>
            <label role="menuitemcheckbox" v-for="g in hostedShown" :key="g.type"
                   :aria-checked="String(hostedOn(g))"
                   :title="current.incognito && g.type === 'memory' ? 'Pas de mémoire en incognito' : ''">
              <input type="checkbox" :checked="hostedOn(g)" @change="$emit('toggle-tool', g.type)"
                     :disabled="current.incognito && g.type === 'memory'">
              <span>{{ g.label }}<small>{{ current.incognito && g.type === 'memory'
                ? "pas de mémoire en incognito" : g.tools.join(", ") }}<template v-if="g.specs">
                · ≈ {{ kTokens(hostedCost(g)) }} tokens</template></small></span></label>
          </template>
          <p class="menu-note" v-else-if="caps.hostedTools && !chat.hosted_tools"
             title="[chat] hosted_tools = true dans config.toml">Outils du proxy inactifs</p>
          <template v-if="devicesShown.length">
            <p class="menu-head">Navigateur</p>
            <label role="menuitemcheckbox" v-for="f in devicesShown" :key="f.key"
                   :aria-checked="String(deviceOn.includes(f.key))">
              <input type="checkbox" :checked="deviceOn.includes(f.key)" @change="$emit('toggle-device', f.key)">
              <span>{{ f.label }}<small>{{ f.hint }} · ≈ {{ kTokens(familyCost(f)) }} tokens</small></span></label>
          </template>
          <p class="menu-note tools-cost">≈ {{ kTokens(toolsCost) }} tokens ajoutés à chaque requête
            par les outils cochés<br>
            <button type="button" class="link" @click="$emit('show-prompt')">Voir le prompt invisible</button></p>
        </div>
      </div>
      <label class="effort" v-if="imageMode"
             title="Contexte : avec la suite, les demandes précédentes de la même suite d'images partent avec le message (« plus sombre » suit « un phare ») ; seul, le dernier message uniquement">
        <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h12M4 12h16M4 18h9"/></svg>
        <select :value="String(!!current.imageAlone)" :disabled="streaming" aria-label="Contexte des images"
                @change="$emit('set-conv', 'imageAlone', /** @type {HTMLSelectElement} */ ($event.target).value === 'true')">
          <option value="false">avec la suite</option>
          <option value="true">message seul</option>
        </select>
      </label>
      <label class="effort" v-if="imageMode" title="Nombre d'images par demande">
        <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z"/></svg>
        <select :value="current.imageCount" :disabled="streaming" aria-label="Nombre d'images"
                @change="$emit('set-conv', 'imageCount', Number(/** @type {HTMLSelectElement} */ ($event.target).value))">
          <option v-for="k in 4" :key="k" :value="k">× {{ k }}</option>
        </select>
      </label>
      <label class="effort" v-if="imageMode" title="Taille de l'image (largeur × hauteur) ; plus grande, plus lente">
        <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16v14H4zM4 15l5-5 4 4 3-3 4 4"/></svg>
        <select :value="current.imageSize" :disabled="streaming" aria-label="Taille de l'image"
                @change="$emit('set-conv', 'imageSize', /** @type {HTMLSelectElement} */ ($event.target).value)">
          <option v-for="z in imageSizes" :key="z" :value="z">{{ z.replace("x", " × ") }}</option>
        </select>
      </label>
      <label class="effort" v-if="imageMode && imageSteps.length" title="Passes de débruitage ; moins, plus rapide et moins fin ; auto : le défaut du modèle">
        <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 19V9M10 19V5M15 19v-7M20 19v-4"/></svg>
        <select :value="current.imageSteps" :disabled="streaming" aria-label="Passes de débruitage"
                @change="$emit('set-conv', 'imageSteps', Number(/** @type {HTMLSelectElement} */ ($event.target).value))">
          <option :value="0">auto</option>
          <option v-for="k in imageSteps" :key="k" :value="k">{{ k }} passes</option>
        </select>
      </label>
      <label class="effort" v-if="imageMode && imageLoras.length" title="LoRA appliqué par le modèle d'images (adaptateur de style ou de contenu) ; sans : le modèle seul">
        <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3l8 4.5-8 4.5-8-4.5zM4 12l8 4.5 8-4.5M4 16.5l8 4.5 8-4.5"/></svg>
        <select :value="current.imageLora" :disabled="streaming" aria-label="LoRA"
                @change="$emit('set-conv', 'imageLora', /** @type {HTMLSelectElement} */ ($event.target).value)">
          <option value="">sans LoRA</option>
          <option v-for="k in imageLoras" :key="k" :value="k">{{ k }}</option>
        </select>
      </label>
      <label class="effort" v-if="voiceMode === 'custom' || voiceMode === 'clone'"
             :title="voiceMode === 'clone' ? 'Voix clonée (gardée avec le compte)' : 'Voix du modèle'">
        <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3zM5 11a7 7 0 0 0 14 0M12 18v3"/></svg>
        <select v-if="voiceMode === 'custom'" :value="current.voiceName" :disabled="streaming" aria-label="Voix"
                @change="$emit('set-conv', 'voiceName', /** @type {HTMLSelectElement} */ ($event.target).value)">
          <option v-if="!voiceChoices.length" value="" disabled>chargement…</option>
          <option v-for="v in voiceChoices" :key="v.id" :value="v.id">{{ v.name }}</option>
        </select>
        <select v-else :value="current.voiceClone || ''" :disabled="streaming" aria-label="Voix clonée"
                @change="/** @type {HTMLSelectElement} */ ($event.target).value === '+' ? (/** @type {HTMLSelectElement} */ ($event.target).value = current.voiceClone || '', $emit('new-voice')) : $emit('set-conv', 'voiceClone', /** @type {HTMLSelectElement} */ ($event.target).value)">
          <option v-if="!voiceChoices.length" value="" disabled>aucune voix</option>
          <option v-for="v in voiceChoices" :key="v.id" :value="v.id">{{ v.name }}</option>
          <option value="+">＋ nouvelle voix…</option>
        </select>
      </label>
      <label class="effort" v-if="!genMode && caps.reasoning" title="Niveau de réflexion du modèle : auto (celui du modèle), non, faible, moyenne, élevée">
        <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-3.5 10.9c.6.5 1 1.2 1 2.1h5c0-.9.4-1.6 1-2.1A6 6 0 0 0 12 3z"/></svg>
        <select :value="current.reasoning" :disabled="streaming" aria-label="Réflexion"
                @change="$emit('set-conv', 'reasoning', /** @type {HTMLSelectElement} */ ($event.target).value)">
          <option value="auto">auto</option>
          <option value="none">non</option>
          <option value="low">faible</option>
          <option value="medium">moyenne</option>
          <option value="high">élevée</option>
        </select>
      </label>
      <div class="attach-menu ctx-menu" v-if="context && context.tokens">
        <button type="button" class="btn ghost ctx-gauge" aria-haspopup="menu" :aria-expanded="String(ctxOpen)"
                :class="{ warn: context.max && context.ratio >= 0.6, full: context.max && context.ratio >= AUTO_AT }"
                @click="$emit('update:ctxOpen', !ctxOpen)" :title="contextTitle">
          <svg class="ring" viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="8" class="track"/>
            <circle cx="10" cy="10" r="8" class="fill" v-if="context.max"
                    :stroke-dasharray="(Math.min(1, context.ratio) * 50.27).toFixed(2) + ' 50.27'"/></svg>
          <span class="lbl">{{ context.max ? Math.round(context.ratio * 100) + " %" : kTokens(context.tokens) }}</span></button>
        <div class="menu" role="menu" v-show="ctxOpen">
          <p class="menu-head">Contexte</p>
          <p class="menu-note">{{ context.measured ? "" : "≈ " }}{{ num(context.tokens) }} tokens{{ context.max
            ? " sur " + num(context.max) + " (" + Math.round(context.ratio * 100) + " %)" : "" }}<br>
            <span class="dim">{{ context.measured ? "mesuré par le modèle à sa dernière réponse"
              : "estimé : la dernière mesure, plus ce qui a suivi" }}</span></p>
          <template v-if="ctxParts">
            <div class="ctx-bar" role="img" :aria-label="ctxParts.parts.map((p) => p.label + ' ' + num(p.tokens)).join(', ')">
              <span v-for="p in ctxParts.parts" :key="p.key" :class="'part-' + p.key"
                    :style="{ width: p.pct + '%' }" :title="p.label + ' : ≈ ' + num(p.tokens) + ' tokens'"></span></div>
            <ul class="ctx-legend">
              <li v-for="p in ctxParts.parts" :key="p.key"
                  :title="p.key === 'other' ? 'Ce que mesure le modèle sans que l’estimation l’explique : son gabarit de conversation, l’écart de l’estimation (≈ 3,5 caractères par token)' : ''"><i :class="'part-' + p.key"></i>{{ p.label }}<b>≈ {{ kTokens(p.tokens) }}</b></li>
              <li v-if="ctxParts.free"><i class="part-free"></i>Libre<b>{{ kTokens(ctxParts.free) }}</b></li>
            </ul>
          </template>
          <button type="button" role="menuitem" :disabled="streaming" @click="$emit('compact-now')">
            <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M7 12h10M10 17h4"/></svg>
            <span>Résumer maintenant<small>/compact [consigne] — les messages restent affichés, le résumé part à leur place</small></span></button>
          <label role="menuitemcheckbox" :aria-checked="String(autoCompact)">
            <input type="checkbox" :checked="autoCompact"
                   @change="$emit('update:autoCompact', /** @type {HTMLInputElement} */ ($event.target).checked)">
            <span>Résumé automatique<small>{{ context.max ? "avant le message suivant, au-delà de " + Math.round(AUTO_AT * 100) + " % du contexte"
              : "fenêtre de ce modèle inconnue : jamais automatique" }}</small></span></label>
        </div>
      </div>
      <span class="status" :class="{ warn: (blind || unsent) && !status }">{{ status
        || (unsent && voiceMode ? "les pièces jointes ne sont pas lues"
          : unsent && asrMode ? "seuls les audios sont transcrits"
          : unsent && !imageMode ? "l'audio n'est lu que par un modèle de transcription"
          : unsent ? "seule la première image est envoyée (à retoucher)"
        : blind ? "ce modèle ne lit peut-être pas les images" : "") }}</span>
      <button type="submit" class="btn ico-btn send" :class="{ ghost: streaming }" :disabled="!canSend"
              :title="streaming ? 'Mettre en file' : 'Envoyer'">
        <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5M5 12l7-7 7 7"/></svg>
        <span class="lbl">{{ streaming ? "Mettre en file" : "Envoyer" }}</span></button>
      <button v-if="streaming" type="button" class="btn danger ico-btn" @click="$emit('stop')" title="Arrêter">
        <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M7 7h10v10H7z"/></svg>
        <span class="lbl">Arrêter</span></button>
    </div>
  </form>
</template>
