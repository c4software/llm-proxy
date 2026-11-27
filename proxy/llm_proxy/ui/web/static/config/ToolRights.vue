<script>
// @ts-check
import * as Vue from "vue";
import { TOOL_LABELS } from "./rights.js";

const { ref, computed, watch } = Vue;

/*
  Le choix des droits : « tous les outils » (null), ou une case par outil
  natif et par serveur MCP — cochée, le serveur couvre aussi les outils
  qu'il découvrira ; sinon ses outils un par un. v-model : null | [noms].
  Un droit déjà donné mais que le proxy ne connaît plus reste affiché.
*/
const ToolRights = {
  props: { modelValue: { type: Array, default: null }, catalog: { type: Array, default: () => [] } },
  emits: ["update:modelValue"],
  setup(props, { emit }) {
    // La dernière valeur émise : deux cases changées avant que le parent
    // ne rende la nouvelle ne s'écrasent pas.
    const value = ref(props.modelValue);
    watch(() => props.modelValue, (v) => { value.value = v; });
    function set(v) { value.value = v; emit("update:modelValue", v); }
    const all = computed(() => value.value == null);
    const chosen = computed(() => new Set(value.value || []));
    const natives = computed(() => props.catalog.filter((t) => !t.server));
    const servers = computed(() => props.catalog.filter((t) => t.server));
    const unknown = computed(() => {
      const known = new Set(props.catalog.flatMap((t) => [t.name, ...(t.tools || []).map((x) => x.name)]));
      return (value.value || []).filter((r) => !known.has(r) && r !== "mcp");
    });
    function setAll(on) {
      // Passer à « choisis » part de ce qui est actif : rien ne se perd
      // en silence, on décoche ce qu'on retire.
      set(on ? null
        : props.catalog.filter((t) => t.server || t.enabled).map((t) => t.name));
    }
    function toggle(name, on) {
      const next = new Set(chosen.value);
      on ? next.add(name) : next.delete(name);
      set([...next].sort());
    }
    return { all, chosen, natives, servers, unknown, setAll, toggle, label: (n) => TOOL_LABELS[n] || n };
  },
};

export default ToolRights;
</script>

<template>
    <fieldset class="key-rights">
      <legend>Outils hébergés permis à cette clé</legend>
      <div class="key-rights-mode">
        <label class="check"><input type="radio" :checked="all" @change="setAll(true)"> tous</label>
        <label class="check"><input type="radio" :checked="!all" @change="setAll(false)"> seulement ceux-ci</label>
      </div>
      <div class="key-rights-list" v-if="!all">
        <label class="check" v-for="t in natives" :key="t.name" :class="{ dim: !t.enabled }"
               :title="t.enabled ? t.name : t.name + ' : inactif sur ce proxy'">
          <input type="checkbox" :checked="chosen.has(t.name)" @change="toggle(t.name, $event.target.checked)">
          {{ label(t.name) }}<span class="tag" v-if="!t.enabled">inactif</span></label>
        <div class="key-rights-server" v-for="s in servers" :key="s.name">
          <label class="check" title="Tous les outils du serveur, ceux qu'il découvrira compris">
            <input type="checkbox" :checked="chosen.has(s.name)" @change="toggle(s.name, $event.target.checked)">
            MCP <b>{{ s.server }}</b> (tout le serveur)</label>
          <label class="check sub" v-for="x in s.tools" :key="x.name">
            <input type="checkbox" :disabled="chosen.has(s.name)"
                   :checked="chosen.has(s.name) || chosen.has(x.name)" @change="toggle(x.name, $event.target.checked)">
            <code>{{ x.name }}</code></label>
        </div>
        <label class="check" v-for="r in unknown" :key="r" title="Plus connu du proxy">
          <input type="checkbox" checked @change="toggle(r, $event.target.checked)"> <code>{{ r }}</code>
          <span class="tag">inconnu</span></label>
        <p class="note" v-if="!chosen.size">Aucun outil : la clé ne sert qu'au modèle seul.</p>
      </div>
    </fieldset>
</template>
