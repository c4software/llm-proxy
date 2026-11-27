<script>
// @ts-check
// Fil de messages du chat : état vide, résumés (/compact), messages
// utilisateur, réponses assistant en chronologie multi-tours, cartes d'outils,
// fichiers produits, sources et actions meta.
import { ref } from "vue";
import { ownImage } from "./history.js";
import { num, size } from "../lib/format.js";
import * as Compact from "./compact.js";
import { timeline } from "./turn.js";

export default {
  props: {
    current: { type: Object, required: true },
    sentFrom: { type: Number, default: 0 },
    streaming: { type: Boolean, default: false },
    imageMode: { type: Boolean, default: false },
    voiceMode: { type: String, default: "" },
    asrMode: { type: Boolean, default: false },
    justSpoken: { type: String, default: "" },
    voiceSupported: { type: Boolean, default: false },
    voiceName: { type: String, default: "" },
    playing: { type: Object, required: true },
    upscaleOpt: { type: Object, default: null },
    upscaleFactor: { type: Number, default: 2 },
    upscaleStrength: { type: Number, default: null },
    moreCount: { type: Number, default: 1 },
    keptNotes: { type: Object, required: true },
    html: { type: Function, required: true },
    md: { type: Function, required: true },
    body: { type: Function, required: true },
    candidateOf: { type: Function, required: true },
    thinking: { type: Function, required: true },
    preview: { type: Function, required: true },
    busyTool: { type: Function, required: true },
    argSummary: { type: Function, required: true },
    extOf: { type: Function, required: true },
    host: { type: Function, required: true },
    tooLong: { type: Function, required: true },
    strengthText: { type: Function, required: true },
    keptComponent: { type: Function, required: true },
    keptBelow: { type: Function, required: true },
    keptView: { type: Function, required: true },
    cardPlacement: { type: Function, required: true },
    cardPending: { type: Function, required: true },
    cardComponent: { type: Function, required: true },
    cardView: { type: Function, required: true },
    folderCtx: { type: Function, required: true },
  },
  emits: [
    "scroll", "thread-click", "only-one", "zoom", "image-load", "open-sheet",
    "card-act", "kept-act", "open-out", "uncompact", "edit", "resume-interrupted",
    "continue-reply", "retry-compacted", "copy-text", "listen", "retouch",
    "upscale", "generate-more", "regenerate",
    "update:upscaleFactor", "update:upscaleStrength", "update:moreCount",
  ],
  setup(_, { expose }) {
    const el = ref(null);
    expose({ el });
    return {
      el, ownImage, num, size, timeline,
      kTokens: Compact.kTokens, estimate: Compact.estimate,
    };
  },
};
</script>

<template>
  <div class="thread" ref="el" @scroll="$emit('scroll', $event)" @click="$emit('thread-click', $event)">
    <div class="empty" v-if="!current.messages.length">
      <div class="big">{{ imageMode ? "Quelle image ?" : voiceMode ? "Quel texte lire ?" : asrMode ? "Quel audio transcrire ?" : current.incognito ? "Une question, sans trace ?" : "Une question ?" }}</div>
      <div v-if="voiceMode === 'design'">La voix se décrit dans le prompt système (Réglages) : genre, âge, timbre, accent, style.</div>
      <div v-else-if="voiceMode">Le message envoyé est lu par la voix choisie<template v-if="voiceMode === 'custom'"> ;
        le prompt système (Réglages) en donne le ton</template>.</div>
      <div v-if="asrMode">Joindre un audio (ou l'enregistrer : Micro) ; la réponse est son texte.
        Le message, facultatif, guide le modèle (noms propres, vocabulaire).</div>
      <div v-if="!current.model">Choisir un modèle</div>
    </div>
    <article v-for="(m, i) in current.messages" :key="m.id" :data-msg="m.id" class="msg"
             :class="[m.role, { compacted: i < sentFrom && m.role !== 'summary' }]">
      <template v-if="m.role === 'summary'">
        <div class="compact-mark" role="separator">
          <span class="compact-label">
            <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M7 12h10M10 17h4"/></svg>
            {{ m.running ? "Résumé de la conversation…" : m.auto ? "Résumé automatique" : "Conversation résumée" }}
            <span class="dim" v-if="!m.running">· {{ m.covered }} message{{ m.covered > 1 ? "s" : "" }}
              · ≈ {{ kTokens(m.before) }} → {{ kTokens(estimate(m.content)) }} tokens<template v-if="m.light">
              · d'un historique allégé</template></span>
          </span>
        </div>
        <div class="typing" v-if="m.running"><i></i><i></i><i></i></div>
        <details class="compact-body" v-else>
          <summary>Voir le résumé<span class="dim" v-if="m.focus"> · consigne : {{ m.focus }}</span></summary>
          <div class="md" v-html="html(m)"></div>
        </details>
        <div class="msg-meta" v-if="!m.running">
          <span>{{ m.model }}</span>
          <span v-if="m.duration">{{ m.duration.toFixed(1) }} s</span>
          <span>les messages au-dessus ne sont plus envoyés au modèle</span>
          <button type="button" class="link" :disabled="streaming" @click="$emit('uncompact', i)"
                  title="Retirer ce résumé : tout l'historique repart au modèle">Annuler le résumé</button>
        </div>
      </template>
      <template v-else-if="m.role === 'user'">
        <div class="msg-origin" v-if="m.webhook" :title="m.webhook.at ? 'Reçu le ' + new Date(m.webhook.at).toLocaleString('fr-FR') : ''">
          reçu par le webhook « {{ m.webhook.name }} »<template v-if="m.webhook.from"> · {{ m.webhook.from }}</template></div>
        <div class="bubble">
          <div class="attachments" v-if="m.attachments && m.attachments.length">
            <template v-for="a in m.attachments" :key="a.name">
              <img v-if="a.kind === 'image' && a.data" :src="a.data" :alt="a.name">
              <span v-else-if="a.kind === 'audio' && a.data" class="msg-audio">
                <audio class="gen-audio" controls preload="metadata" :src="a.data" @play="$emit('only-one', $event)"></audio>
                <span class="dim">{{ a.name }}</span></span>
              <span v-else class="chip file" :data-ext="extOf(a.name)">{{ a.name }}<span class="dim" v-if="a.kind === 'image'">
                · image absente</span></span>
            </template>
          </div>
          <div class="text" v-if="m.content">{{ m.content }}</div>
        </div>
        <div class="msg-meta">
          <button type="button" class="link" :disabled="streaming" @click="$emit('edit', i)">Modifier</button>
        </div>
      </template>
      <template v-else>
        <!-- La réponse en CHRONOLOGIE (turn.timeline) : par tour, sa
             réflexion, son texte, ses puces ; le tour en cours en
             dernier, là où l'on regarde. Une réponse d'avant les
             positions a sa réflexion d'un seul tenant en tête (`head`). -->
        <template v-for="t in [timeline(m)]" :key="'tl' + m.id">
          <details class="reasoning" v-if="t.head && current.showReasoning">
            <summary>
              <span class="r-label">Raisonnement</span>
              <span class="r-last">{{ candidateOf(t.head) }}</span>
            </summary>
            <div class="md" v-html="md(m, 'head', t.head)"></div>
          </details>
          <template v-for="(seg, k) in t.segments" :key="'seg' + k">
            <!-- Plié de base : une ligne d'aperçu, remplacée à son rythme
                 (clé = n), et le CONTENEUR miroite tant que le modèle
                 réfléchit (jamais le span coupé par « … », qui transite). -->
            <details class="reasoning" v-if="seg.reasoning && current.showReasoning">
              <summary>
                <span class="r-label">Raisonnement</span>
                <span class="r-last" :class="{ live: thinking(m, i, t, k) }">
                  <transition name="r-swap" mode="out-in">
                    <span :key="preview(m, i, t, k).n">{{ preview(m, i, t, k).text }}</span>
                  </transition></span>
              </summary>
              <div class="md" v-html="md(m, 'r' + k, seg.reasoning)"></div>
            </details>
            <!-- Les images et l'audio produits : avant le texte final. -->
            <template v-if="k === t.segments.length - 1">
            <div class="gen-images" :class="{ multi: m.attachments.length > 1 }" v-if="m.attachments && m.attachments.length">
              <template v-for="a in m.attachments" :key="a.name">
                <audio v-if="a.kind === 'audio' && a.data" class="gen-audio" controls preload="metadata"
                       :src="a.data" :autoplay="m.id === justSpoken" @play="$emit('only-one', $event)"></audio>
                <button v-else-if="a.data" type="button" class="gen-image" title="Agrandir"
                        @click="$emit('zoom', a)"><img :src="a.data" :alt="a.name" @load="$emit('image-load', $event)"></button>
                <span v-else class="chip file" :data-ext="extOf(a.name)">{{ a.name }}<span class="dim">
                  · image absente</span></span>
              </template>
            </div>
            <details class="gen-prompt" v-if="m.image && m.prompt && m.prompt.includes('\n')">
              <summary>prompt envoyé (avec le contexte)</summary><pre>{{ m.prompt }}</pre></details>
            </template>
            <template v-for="(part, n) in body(m, seg.content, k, k === t.segments.length - 1, seg.start)"
                      :key="part.k ? part.k.id : 'md' + k + '-' + n">
              <component v-if="part.k" :is="keptComponent(part.k)" :k="part.k"></component>
              <div class="md" v-else v-html="part.html"></div>
            </template>
            <div class="tool-calls" v-if="seg.tools.length">
              <details v-for="c in seg.tools" :key="c.id" class="tool" :class="c.status">
                <summary>
                  <i class="state"></i><b>{{ c.name }}</b>
                  <span class="arg">{{ argSummary(c) }}</span>
                  <span class="dim" v-if="c.status === 'writing'">écriture… {{ num(c.chars) }} car.</span>
                  <span class="dim" v-else-if="c.status === 'running'">en cours…</span>
                  <span class="dim" v-else-if="c.status === 'limit'">limite d'appels atteinte</span>
                  <span class="dim" v-else-if="c.chars !== undefined">{{ num(c.chars) }} car.</span>
                </summary>
                <pre v-if="c.preview">{{ c.preview }}{{ c.chars > c.preview.length ? "\n…" : "" }}</pre>
              </details>
            </div>
          </template>
        </template>
        <!-- Ce qui attend l'utilisateur : la CARTE d'un outil (tools/) —
             son composant, ou le gabarit commun (view) ; une carte de la
             feuille du bas n'a ici qu'une ligne qui la rouvre. -->
        <template v-if="m.waiting">
          <button type="button" class="ask-pending" v-if="cardPlacement(m.waiting) === 'sheet'"
                  @click="$emit('open-sheet')">{{ cardPending(m.waiting) }}</button>
          <component v-else-if="cardComponent(m.waiting)" :is="cardComponent(m.waiting)" :key="m.waiting.id"
                     :w="m.waiting" :busy="streaming || !!m.waiting.busy" :ctx="folderCtx()"
                     @act="(action, payload) => $emit('card-act', m, action, payload)"></component>
          <div class="tool-card device-card" v-else>
            <template v-for="v in [cardView(m.waiting)]" :key="m.waiting.id">
              <p class="write-head"><b>{{ v.title }}</b></p>
              <p class="dim" v-if="v.detail">{{ v.detail }}</p>
              <code class="device-url" v-if="v.code">{{ v.code }}</code>
              <span class="device-swatch" v-if="v.swatch" :style="{ background: v.swatch }"></span>
              <pre class="device-quote" v-if="v.quote">{{ v.quote }}</pre>
              <video class="device-media" v-if="v.video" :srcObject.prop="v.video" autoplay muted playsinline></video>
              <img class="device-media" v-if="v.preview" :src="v.preview" alt="Aperçu de l'image à envoyer">
              <div class="card-actions">
                <button v-for="a in v.actions" :key="a.id" type="button" :class="['btn', { ghost: !a.primary }]"
                        :disabled="streaming || m.waiting.busy" :title="a.title || ''"
                        @click="$emit('card-act', m, a.id)">{{ a.label }}</button>
                <span class="dim" v-if="v.note">{{ v.note }}</span>
              </div>
            </template>
          </div>
        </template>
        <div class="tool-card device-card kept-card" v-for="k in keptBelow(m)" :key="k.id">
          <template v-for="v in [keptView(k)]" :key="k.id">
            <p class="write-head"><b>{{ v.title }}</b></p>
            <p class="dim" v-if="v.detail">{{ v.detail }}</p>
            <code class="device-url" v-if="v.code">{{ v.code }}</code>
            <span class="device-swatch" v-if="v.swatch" :style="{ background: v.swatch }"></span>
            <pre class="device-quote" v-if="v.quote">{{ v.quote }}</pre>
            <div class="card-actions">
              <button v-for="a in v.actions" :key="a.id" type="button" :class="['btn', { ghost: !a.primary }]"
                      :disabled="keptNotes[k.id] === '…'" :title="a.title || ''"
                      @click="$emit('kept-act', m, k, a.id)">{{ a.label }}</button>
              <span class="dim" v-if="keptNotes[k.id] && keptNotes[k.id] !== '…'">{{ keptNotes[k.id] }}</span>
            </div>
          </template>
        </div>
        <div class="out-files" v-if="m.files && m.files.length">
          <button type="button" class="chip file" v-for="f in m.files" :key="f.name" :data-ext="extOf(f.name)"
                  @click="$emit('open-out', f)" title="Ouvrir (aperçu, copier, télécharger)">{{ f.name }}
            <span class="dim">{{ size(f.size) }}</span></button>
        </div>
        <div class="typing" v-if="m.serverTurn && !m.error">
          <i></i><i></i><i></i><span class="dim">réponse en cours, menée par le proxy (webhook « {{ m.webhook ? m.webhook.name : "" }} »)…</span></div>
        <div class="typing" v-else-if="streaming && !m.error
             && (m.image ? m.want > (m.attachments || []).length
                 : i === current.messages.length - 1 && [timeline(m).segments.at(-1)].every((seg) =>
                   !seg.content.trim() && !busyTool(m)))">
          <i></i><i></i><i></i><span class="dim" v-if="m.image">génération de l'image{{ m.want > 1
            ? " " + ((m.attachments || []).length + 1) + " / " + m.want : "" }}…</span>
          <span class="dim" v-else-if="m.voice">synthèse vocale{{ m.progress ? " · " + m.progress : "" }}…</span>
          <span class="dim" v-else-if="m.asr">transcription{{ m.progress ? " · " + m.progress : "" }}…</span></div>
        <p class="msg-warn" v-if="m.interrupted && !streaming">Réponse interrompue : la page a été
          rechargée pendant qu'elle s'écrivait.
          <button type="button" class="link" v-if="i === current.messages.length - 1"
                  @click="$emit('resume-interrupted', m)">Reprendre</button></p>
        <p class="msg-warn" v-else-if="m.cut && !(streaming && i === current.messages.length - 1)">{{ m.cut === "length"
            ? "Réponse coupée : le modèle a atteint sa limite de longueur."
            : "Réponse incomplète : le flux s'est arrêté avant la fin." }}
          <button type="button" class="link" v-if="i === current.messages.length - 1 && !streaming"
                  @click="$emit('continue-reply')">Continuer</button></p>
        <p class="msg-error" v-if="m.error">{{ m.error }}
          <button type="button" class="link" v-if="tooLong(m) && i === current.messages.length - 1 && !streaming"
                  @click="$emit('retry-compacted')">Résumer la conversation et réessayer</button></p>
        <div class="sources" v-if="m.sources.length">
          <a v-for="s in m.sources" :key="s.url" :href="s.url" target="_blank"
             rel="noopener noreferrer" :title="s.url">{{ s.title || host(s.url) }}
            <span class="dim">{{ host(s.url) }}</span></a>
        </div>
        <div class="msg-meta" v-if="!m.serverTurn && !(streaming && i === current.messages.length - 1)">
          <span>{{ m.model }}</span>
          <span v-if="m.usage">{{ num(m.usage.prompt_tokens) }} → {{ num(m.usage.completion_tokens) }} tokens</span>
          <span v-if="m.duration">{{ m.duration.toFixed(1) }} s</span>
          <button type="button" class="link" v-if="m.content" @click="$emit('copy-text', m.content, $event.target)">Copier</button>
          <button type="button" class="link" v-if="m.content && voiceSupported" @click="$emit('listen', m)"
                  :title="'Lire à voix haute (' + voiceName + ')'">{{ playing.id === m.id
                    ? (playing.loading ? "Synthèse…" : "Arrêter la lecture") : "Écouter" }}</button>
          <span v-if="m.asr && m.audioDuration">{{ m.audioDuration.toFixed(1) }} s d'audio</span>
          <!-- Une image : ses actions ici ; plusieurs : dans l'image agrandie. -->
          <template v-if="m.voice && m.attachments.length && m.attachments[0].data">
            <span v-if="m.voiceName">voix : {{ m.voiceName }}</span>
            <span v-if="m.attachments[0].duration">{{ m.attachments[0].duration.toFixed(1) }} s d'audio</span>
            <a class="link" :href="m.attachments[0].data" :download="m.attachments[0].name">Télécharger</a>
          </template>
          <template v-if="m.image && m.attachments.length === 1 && m.attachments[0].data">
            <a class="link" :href="m.attachments[0].data" :download="m.attachments[0].name">Télécharger</a>
            <button type="button" class="link" v-if="ownImage(m.attachments[0].data)"
                    :disabled="streaming" @click="$emit('retouch', m.attachments[0])">Retoucher</button>
            <span class="gen-more" v-if="upscaleOpt && ownImage(m.attachments[0].data) && !streaming">
              <button type="button" class="link" @click="$emit('upscale', m.attachments[0])"
                      title="Agrandir : le modèle d'agrandissement redessine l'image à une plus grande taille (détails un peu changés)">Agrandir</button>
              <select :value="upscaleFactor" aria-label="Facteur d'agrandissement"
                      @change="$emit('update:upscaleFactor', Number(/** @type {HTMLSelectElement} */ ($event.target).value))">
                <option v-for="f in upscaleOpt.factors" :key="f" :value="f">× {{ f }}</option></select>
              <select v-if="upscaleOpt.strengths && upscaleOpt.strengths.length > 1" :value="upscaleStrength"
                      aria-label="Force de l'agrandissement" title="Plus bas : plus fidèle à l'image, moins de détail ajouté"
                      @change="$emit('update:upscaleStrength', Number(/** @type {HTMLSelectElement} */ ($event.target).value))">
                <option v-for="s in upscaleOpt.strengths" :key="s.value" :value="s.value">{{ strengthText(s) }}</option></select></span>
          </template>
          <span v-else-if="m.image && m.attachments.length > 1">cliquer une image pour la télécharger ou la retoucher</span>
          <span class="gen-more" v-if="m.image && m.prompt && !streaming">
            <button type="button" class="link" @click="$emit('generate-more', m)"
                    title="D'autres images avec le même prompt, jointes à cette réponse (sans le fil depuis)">Générer encore</button>
            <select :value="moreCount" aria-label="Nombre d'images à générer encore"
                    @change="$emit('update:moreCount', Number(/** @type {HTMLSelectElement} */ ($event.target).value))">
              <option v-for="n in 4" :key="n" :value="n">{{ n }}</option></select></span>
          <button type="button" class="link" v-if="i === current.messages.length - 1 && !m.upscale"
                  @click="$emit('regenerate')">Régénérer</button>
        </div>
      </template>
    </article>
  </div>
</template>
