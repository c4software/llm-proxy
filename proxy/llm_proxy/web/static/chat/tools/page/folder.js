// @ts-check
/*
  Le DOSSIER ouvert (chat/workspace.js, le modèle du dossier) : ses
  outils, déclarés tant qu'un dossier est ouvert dans la conversation.

    folder_list    le contenu d'un sous-dossier (ou tout, récursif)
    folder_read    un fichier texte, par tranches de lignes numérotées
    folder_search  une chaîne ou une expression dans les fichiers texte
    folder_write   créer ou remplacer un fichier  ┐ une carte montre le
    folder_edit    remplacer un passage exact     ┘ diff (WriteCard) :
                                                    Appliquer ou Refuser

  Le modèle ne lit que ce dont il a besoin : un dépôt de mille fichiers
  ne coûte que ce qu'il en ouvre.
*/
import { TOOL, FAMILY, PageTool, defineFamily, failure } from "../contract.js";
import * as workspace from "../../workspace.js";
import { lineDiff, counts } from "../../diff.js";

const { computed, toRaw } = Vue;

const NO_FOLDER = failure("aucun dossier ouvert (l'utilisateur doit le rouvrir).");

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
  template: `
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
    </div>`,
};

/** @param {any} w */
const comment = (w) => (w.comment ? ` Son commentaire : « ${w.comment} »` : "");

class FolderList extends PageTool {
  constructor() {
    super({
      name: TOOL.FOLDER_LIST,
      description: "Liste le contenu du dossier ouvert par l'utilisateur dans le chat. "
        + "Sans `path` : sa racine. Les sous-dossiers finissent par « / », avec leur nombre "
        + "de fichiers ; les fichiers ont leur taille. `recursive` : tous les fichiers sous `path`.",
      params: {
        path: { type: "string", description: "Sous-dossier relatif à la racine ; vide = la racine." },
        recursive: { type: "boolean", description: "Lister tous les fichiers sous `path`." },
      },
    });
  }

  /** @override @param {any} args @param {import("../contract.js").Ctx} ctx */
  run(args, ctx) { return ctx.ws ? workspace.list(ctx.ws, args) : NO_FOLDER; }

  /** @override @param {any} a */
  summary(a) { return (a.path || "racine") + (a.recursive ? " (récursif)" : ""); }
}

class FolderRead extends PageTool {
  constructor() {
    super({
      name: TOOL.FOLDER_READ,
      description: "Lit un fichier texte du dossier ouvert, lignes numérotées. Un long fichier "
        + "se lit par tranches : `start_line` et `end_line` (incluses, à partir de 1).",
      params: {
        path: { type: "string", description: "Chemin du fichier, relatif à la racine du dossier." },
        start_line: { type: "integer", description: "Première ligne (défaut : 1)." },
        end_line: { type: "integer", description: "Dernière ligne (défaut : jusqu'à la limite d'une lecture)." },
      },
      required: ["path"],
    });
  }

  /** @override @param {any} args @param {import("../contract.js").Ctx} ctx */
  run(args, ctx) { return ctx.ws ? workspace.read(ctx.ws, args) : NO_FOLDER; }

  /** @override @param {any} a */
  summary(a) {
    return [a.path, a.start_line ? `lignes ${a.start_line}–${a.end_line || "…"}` : ""].filter(Boolean).join(" · ");
  }
}

class FolderSearch extends PageTool {
  constructor() {
    super({
      name: TOOL.FOLDER_SEARCH,
      description: "Cherche un texte dans les fichiers texte du dossier ouvert ; rend "
        + "« chemin:ligne: texte » pour chaque ligne trouvée.",
      params: {
        pattern: { type: "string", description: "Le texte cherché (ou une expression régulière si `regex`)." },
        path: { type: "string", description: "Ne chercher que sous ce sous-dossier." },
        regex: { type: "boolean", description: "`pattern` est une expression régulière (JavaScript)." },
        ignore_case: { type: "boolean", description: "Sans tenir compte de la casse (défaut : oui)." },
      },
      required: ["pattern"],
    });
  }

  /** @override @param {any} args @param {import("../contract.js").Ctx} ctx */
  run(args, ctx) { return ctx.ws ? workspace.search(ctx.ws, args) : NO_FOLDER; }

  /** @override @param {any} a */
  summary(a) { return [a.pattern, a.path].filter(Boolean).join(" · "); }
}

// Écrire : une carte, le diff ; appliqué sur l'accord de l'utilisateur.
class FolderWriting extends PageTool {
  /** @param {import("../contract.js").ToolSpec} spec */
  constructor(spec) {
    super({ ...spec, component: WriteCard });
  }

  /** @override */
  waits() { return true; }

  /** @override @param {any} args @param {import("../contract.js").Ctx} ctx */
  prepare(args, ctx) {
    if (!ctx || !ctx.ws) return { error: NO_FOLDER };
    return workspace.prepareWrite(ctx.ws, this.name === TOOL.FOLDER_WRITE, args);
  }

  /** @override @param {import("../contract.js").Card} w @param {string} action
   * @param {import("../contract.js").Ctx} ctx */
  async act(w, action, ctx) {
    if (action === "refuse") {
      return { result: `Refusé par l'utilisateur : « ${w.path} » n'a pas été modifié.${comment(w)}` };
    }
    if (!ctx.ws) throw new Error("Rouvrir le dossier pour appliquer l'écriture.");
    let where;
    try {
      where = await workspace.apply(ctx.ws, w.path, w.after, ctx.download);
    } catch (e) {
      throw new Error(`${w.path} : ${e.message}`);
    }
    const lines = w.after.split("\n").length;
    return { result: `Appliqué par l'utilisateur : « ${w.path} » ${w.created ? "créé" : "modifié"}, `
      + `${where} (${lines} ligne${lines > 1 ? "s" : ""}).${comment(w)}` };
  }

  /** @override */
  notice() { return "Une écriture attend votre accord"; }

  /** @override @param {any} a */
  summary(a) { return a.path || ""; }
}

class FolderWrite extends FolderWriting {
  constructor() {
    super({
      name: TOOL.FOLDER_WRITE,
      description: "Crée ou remplace entièrement un fichier texte du dossier ouvert. "
        + "L'utilisateur voit le diff et accepte ou refuse ; le résultat le dit. Pour une "
        + "modification localisée, préférer folder_edit.",
      params: {
        path: { type: "string", description: "Chemin relatif à la racine du dossier." },
        content: { type: "string", description: "Le contenu complet du fichier." },
      },
      required: ["path", "content"],
    });
  }
}

class FolderEdit extends FolderWriting {
  constructor() {
    super({
      name: TOOL.FOLDER_EDIT,
      description: "Remplace un passage EXACT d'un fichier du dossier ouvert (lire le fichier "
        + "d'abord). `old_text` doit y être une seule fois, sauf `replace_all`. L'utilisateur "
        + "voit le diff et accepte ou refuse.",
      params: {
        path: { type: "string" },
        old_text: { type: "string", description: "Le passage actuel, à l'identique (espaces compris)." },
        new_text: { type: "string", description: "Ce qui le remplace." },
        replace_all: { type: "boolean", description: "Remplacer toutes les occurrences." },
      },
      required: ["path", "old_text", "new_text"],
    });
  }
}

export const FAMILIES = [defineFamily({
  key: FAMILY.FOLDER, label: "Dossier ouvert", hint: "lire, chercher, écrire avec accord", fixed: true,
  enabled: (ctx) => !!ctx.ws,
  guidance: (ctx) => `Un dossier est ouvert dans le chat : « ${ctx.ws.name} » (${ctx.ws.count} fichiers). `
    + "Le parcourir avec folder_list, folder_read et folder_search plutôt que de supposer son "
    + "contenu ; toute écriture (folder_write, folder_edit) passe par l'accord de l'utilisateur.",
  tools: [new FolderList(), new FolderRead(), new FolderSearch(), new FolderWrite(), new FolderEdit()],
})];
