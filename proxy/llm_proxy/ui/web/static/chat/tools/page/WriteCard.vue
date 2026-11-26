<script>
// @ts-check
/*
  La CARTE d'une écriture dans le dossier ouvert (folder_write,
  folder_edit ; folder.js) : le diff, un commentaire pour le modèle,
  Appliquer ou Refuser.
*/
import * as Vue from "vue";
import { lineDiff, counts } from "../../diff.js";

const { computed, toRaw } = Vue;

// La carte d'une écriture : le diff, un commentaire pour le modèle.
const WriteCard = {
  props: ["w", "busy", "ctx"],
  emits: ["act"],
  setup(props) {
    const diff = computed(() => lineDiff(toRaw(props.w).before, toRaw(props.w).after));
    const n = computed(() => counts(diff.value));
    const ready = computed(() => !!(props.ctx && props.ctx.ws));
    const cls = (d) => "d" + (d.type === "+" ? "add" : d.type === "-" ? "del" : d.type === "…" ? "gap" : "ctx");
    return { diff, n, ready, cls };
  },
};

export default WriteCard;
</script>

<template>
    <div class="tool-card write-card">
      <p class="write-head"><b>{{ w.created ? "Créer" : "Modifier" }}</b>
        <code>{{ w.path }}</code>
        <span class="dim">+{{ n.added }} −{{ n.removed }}</span></p>
      <pre class="diff"><span v-for="(d, k) in diff" :key="k" :class="cls(d)">{{ d.type === "…" ? "⋯" : d.type + " " + d.text }}
</span></pre>
      <input class="ask-other" v-model="w.comment" :disabled="busy"
             placeholder="Commentaire pour le modèle (facultatif)">
      <div class="card-actions">
        <button type="button" class="btn" :disabled="busy || !ready" @click="$emit('act', 'apply')"
                :title="ready ? '' : 'Rouvrir le dossier pour appliquer'">Appliquer</button>
        <button type="button" class="btn ghost" :disabled="busy" @click="$emit('act', 'refuse')">Refuser</button>
        <span class="dim" v-if="!ready">dossier à rouvrir pour appliquer</span>
      </div>
    </div>
</template>
