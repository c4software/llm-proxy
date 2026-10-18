// @ts-check
/*
  Les ARTIFACTS : ce que le modèle montre dans le panneau latéral du chat
  (ArtifactPanel.js) — une page, un document, un schéma, un SVG, du code.

    show_artifact  une nouvelle version (un même `id` : la suivante)
    edit_artifact  une RETOUCHE : des remplacements de passages exacts dans
                   la dernière version, qui en font une nouvelle — sans
                   réécrire (ni re-streamer) tout l'artifact
    read_artifact  la dernière version, telle qu'elle est (après des
                   retouches, le modèle ne l'a plus en entier sous les yeux)

  Et pour DÉBOGUER la page affichée — celle que l'utilisateur voit, dans
  le panneau (chat/artifact-bridge.js). Actif par défaut ; l'utilisateur
  le coupe pour un artifact (bouton Débogage du panneau, `art.debug`
  faux), et le modèle peut alors proposer de le rétablir par une carte.

    request_artifact_debug  la carte qui propose de le rétablir (s'il y a
                            un artifact au débogage coupé)
    inspect_artifact        sa console, ses erreurs, sa structure ; une
                            capture                    ┐ si un artifact a
    run_in_artifact         un script dans la page,    │ son débogage
                            avec click, type, key,     ┘ activé
                            wait, text, all

  Les artifacts sont gardés avec la conversation (conv.artifacts) ; leur
  rendu isolé est dans chat/artifacts.js.
*/
import { TOOL, FAMILY, PageTool, defineFamily, failure, clip } from "../contract.js";
import * as bridge from "../../artifact-bridge.js";
import { debugOn } from "../../artifacts.js";

const MAX_ARTIFACT = 5e5;      // caractères d'un artifact
const TYPES = ["html", "markdown", "svg", "mermaid", "code"];

// L'identifiant d'un artifact : minuscules, sans accents, des tirets.
const slug = (s) => String(s || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase()
  .replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "artifact";

// show_artifact : la version rejoint conv.artifacts ; rend l'id montré.
function showArtifact(conv, args) {
  const type = TYPES.includes(args.type) ? args.type : "";
  if (!type) return { result: failure(`\`type\` doit être l'un de ${TYPES.join(", ")}.`) };
  const content = String(args.content ?? "");
  if (!content.trim()) return { result: "Error: `content` est vide." };
  if (content.length > MAX_ARTIFACT) return { result: `Error: artifact trop long (${MAX_ARTIFACT} caractères au plus).` };
  const title = String(args.title || "Sans titre").trim().slice(0, 120);
  const id = slug(args.id || title);
  const list = conv.artifacts || (conv.artifacts = []);
  let art = list.find((a) => a.id === id);
  if (!art) { list.push({ id, title, versions: [] }); art = list[list.length - 1]; }
  art.title = title;
  art.versions.push({ type, content, language: args.language || "", at: Date.now() });
  const n = art.versions.length;
  return { id, result: `Affiché dans le panneau : « ${title} » (id « ${id} »`
    + (n > 1 ? `, version ${n}).` : ").") + " Pour le modifier ensuite : edit_artifact." };
}

// Retrouvé par son id exact, sinon par son slug (« Checklist randonnée »,
// « checklist-randonnée » : « checklist-randonnee »).
function findArtifact(conv, id) {
  const list = conv.artifacts || [];
  return list.find((a) => a.id === String(id || "")) || list.find((a) => slug(a.id) === slug(id));
}
const known = (conv) => (conv.artifacts || []).map((a) => `« ${a.id} »`).join(", ") || "aucun";

// edit_artifact : les remplacements, tous ou aucun → une nouvelle version.
function editArtifact(conv, args) {
  const art = findArtifact(conv, args.id);
  if (!art) return { result: `Error: artifact « ${args.id} » inconnu (artifacts : ${known(conv)}).` };
  const last = art.versions[art.versions.length - 1];
  const edits = Array.isArray(args.edits) ? args.edits : [];
  if (!edits.length) return { result: "Error: `edits` attendu, au moins une retouche." };
  let content = last.content;
  for (const [k, e] of edits.entries()) {
    const from = String((e && e.old_string) ?? "");
    const to = String((e && e.new_string) ?? "");
    const where = edits.length > 1 ? `retouche ${k + 1} : ` : "";
    if (!from) return { result: `Error: ${where}\`old_string\` est vide.` };
    const count = content.split(from).length - 1;
    if (!count) {
      return { result: `Error: ${where}passage introuvable dans la version ${art.versions.length}. `
        + "Le texte doit être EXACT (espaces, retours à la ligne) ; read_artifact pour le relire. "
        + "Rien n'a été modifié." };
    }
    if (count > 1 && !e.replace_all) {
      return { result: `Error: ${where}passage présent ${count} fois ; l'allonger pour qu'il soit `
        + "unique, ou `replace_all`. Rien n'a été modifié." };
    }
    content = e.replace_all ? content.split(from).join(to) : content.replace(from, () => to);
  }
  if (content.length > MAX_ARTIFACT) return { result: `Error: artifact trop long (${MAX_ARTIFACT} caractères au plus).` };
  if (args.title) art.title = String(args.title).trim().slice(0, 120);
  art.versions.push({ ...last, content, at: Date.now() });
  const n = art.versions.length;
  return { id: art.id, result: `Artifact « ${art.id} » retouché (${edits.length} remplacement`
    + `${edits.length > 1 ? "s" : ""}) : version ${n}, affichée dans le panneau.` };
}

// read_artifact : la dernière version.
function readArtifact(conv, args) {
  const art = findArtifact(conv, args.id);
  if (!art) return failure(`artifact « ${args.id} » inconnu (artifacts : ${known(conv)}).`);
  const last = art.versions[art.versions.length - 1];
  return `Artifact « ${art.id} », version ${art.versions.length} (${last.type}, `
    + `${last.content.length} caractères) :\n${last.content}`;
}


class ShowArtifact extends PageTool {
  constructor() {
    super({
      name: TOOL.SHOW_ARTIFACT,
      description: "Affiche dans le panneau latéral du chat une page HTML (scripts permis, "
        + "bibliothèques depuis cdnjs, jsdelivr ou unpkg ; le reste du réseau — fetch, images "
        + "et tuiles distantes — est bloqué tant que l'utilisateur ne l'a pas autorisé pour cet "
        + "artifact par le bouton Réseau du panneau : prévoir alors un message clair si une "
        + "requête échoue), un document Markdown, un SVG, un schéma Mermaid ou du code. Réutiliser "
        + "le même `id` pour en publier une nouvelle version. Pour les visualisations, maquettes, "
        + "petits outils interactifs, documents longs — pas pour une réponse courte.",
      params: {
        id: { type: "string", description: "Identifiant stable (ex. « tableau-de-bord ») ; même id = nouvelle version." },
        title: { type: "string", description: "Titre court." },
        type: { type: "string", enum: TYPES },
        content: { type: "string", description: "Le contenu complet (une page HTML entière pour `html`)." },
        language: { type: "string", description: "Pour `code` : le langage." },
      },
      required: ["title", "type", "content"],
    });
  }

  /** @override @param {any} args @param {import("../contract.js").Ctx} ctx */
  run(args, ctx) {
    const { id, result } = showArtifact(ctx.conv, args);
    return id ? { result, artifact: id } : result;
  }

  /** @override @param {any} a */
  summary(a) { return [a.title, a.type].filter(Boolean).join(" · "); }
}

class EditArtifact extends PageTool {
  constructor() {
    super({
      name: TOOL.EDIT_ARTIFACT,
      description: "Retouche un artifact déjà affiché SANS le réécrire : chaque `old_string`, "
        + "un passage exact et unique de sa dernière version (espaces et retours à la ligne "
        + "compris, assez long pour être unique), est remplacé par `new_string`. À préférer à "
        + "show_artifact pour toute modification qui ne change pas l'essentiel. Toutes les "
        + "retouches s'appliquent, ou aucune ; en cas de doute sur le texte exact, read_artifact.",
      params: {
        id: { type: "string", description: "L'identifiant de l'artifact." },
        edits: { type: "array", minItems: 1, items: {
          type: "object", required: ["old_string", "new_string"], properties: {
            old_string: { type: "string", description: "Le passage exact à remplacer." },
            new_string: { type: "string", description: "Ce qui le remplace (vide : le supprimer)." },
            replace_all: { type: "boolean", description: "Remplacer toutes ses occurrences (sinon il doit être unique)." },
          } } },
        title: { type: "string", description: "Un nouveau titre (facultatif)." },
      },
      required: ["id", "edits"],
    });
  }

  /** @override @param {any} args @param {import("../contract.js").Ctx} ctx */
  run(args, ctx) {
    const { id, result } = editArtifact(ctx.conv, args);
    return id ? { result, artifact: id } : result;
  }

  /** @override @param {any} a */
  summary(a) {
    const n = Array.isArray(a.edits) ? a.edits.length : 0;
    return [a.id, n ? n + " retouche" + (n > 1 ? "s" : "") : ""].filter(Boolean).join(" · ");
  }
}

class ReadArtifact extends PageTool {
  constructor() {
    super({
      name: TOOL.READ_ARTIFACT,
      description: "Rend la dernière version d'un artifact, telle quelle — avant de le retoucher "
        + "quand son texte exact n'est plus sûr (après plusieurs retouches).",
      params: { id: { type: "string", description: "L'identifiant de l'artifact." } },
      required: ["id"],
    });
  }

  /** @override @param {any} args @param {import("../contract.js").Ctx} ctx */
  run(args, ctx) { return readArtifact(ctx.conv, args); }

  /** @override @param {any} a */
  summary(a) { return a.id || ""; }
}

// ── déboguer la page affichée ───────────────────────────────────────────
const MAX_OUT = 20_000;

/** La console rendue au modèle : une ligne par entrée, les erreurs marquées.
 * @param {any[]} logs */
const consoleText = (logs) => (logs || []).map((l) => `[${l.level}] ${l.text}`).join("\n");

/** L'artifact visé, son débogage activé. @param {any} conv @param {any} id */
function target(conv, id) {
  const art = findArtifact(conv, id);
  if (!art) throw new Error(`artifact « ${id} » inconnu (artifacts : ${known(conv)}).`);
  if (!debugOn(art)) {
    throw new Error(`le débogage de l'artifact « ${art.id} » est coupé par l'utilisateur : `
      + "proposer de le rétablir avec request_artifact_debug.");
  }
  return art;
}

/** @param {any} conv */
const artifactsOf = (conv) => (conv && conv.artifacts) || [];

class RequestArtifactDebug extends PageTool {
  constructor() {
    super({
      name: TOOL.REQUEST_ARTIFACT_DEBUG,
      description: "Propose à l'utilisateur, sur une carte qu'il accepte ou refuse, de rétablir le "
        + "DÉBOGAGE d'un artifact qu'il a coupé : le modèle pourra alors lire sa console et ses "
        + "erreurs, y exécuter du code et le capturer (inspect_artifact, run_in_artifact). À proposer "
        + "quand l'utilisateur dit que cet artifact ne marche pas, plutôt que de deviner la cause.",
      params: {
        id: { type: "string", description: "L'identifiant de l'artifact." },
        reason: { type: "string", description: "Ce qui ne marche pas, en une phrase, montré à l'utilisateur." },
      },
      required: ["id"],
    });
  }

  /** @override */
  waits() { return true; }
  /** @override @param {any} a */
  summary(a) { return [a.id, a.reason].filter(Boolean).join(" · "); }

  /** @override @param {any} args @param {import("../contract.js").Ctx} ctx */
  prepare(args, ctx) {
    const art = findArtifact(ctx.conv, args.id);
    if (!art) return { error: failure(`artifact « ${args.id} » inconnu (artifacts : ${known(ctx.conv)}).`) };
    if (debugOn(art)) return { error: `Le débogage de « ${art.id} » est déjà activé : inspect_artifact et run_in_artifact sont disponibles.` };
    return { artId: art.id, title: art.title, reason: String(args.reason || "").trim().slice(0, 300) };
  }

  /** @override @param {import("../contract.js").Card} w */
  view(w) {
    return { title: `Le modèle propose de rétablir le débogage de « ${w.title} »`, detail: w.reason,
             note: "il pourra lire sa console, y exécuter du code et le capturer ; à couper d'un clic (Débogage, dans le panneau)",
             actions: [{ id: "on", label: "Activer le débogage", primary: true }, { id: "refuse", label: "Non merci" }] };
  }

  /** @override @param {import("../contract.js").Card} w @param {string} action @param {import("../contract.js").Ctx} ctx */
  act(w, action, ctx) {
    if (action === "refuse") return { result: "L'utilisateur a refusé le débogage de l'artifact : ne pas insister ; "
      + "lui demander de décrire le problème, ou de copier les erreurs de la console de son navigateur." };
    const art = findArtifact(ctx.conv, w.artId);
    if (!art) return { result: failure(`artifact « ${w.artId} » disparu.`) };
    art.debug = true;
    return { result: `Débogage de « ${art.id} » activé par l'utilisateur : inspect_artifact et run_in_artifact `
      + "sont disponibles (la page a été rechargée : la console part du chargement)." };
  }
}

/** @param {any} art @param {number} version */
const which = (art, version) => `Artifact « ${art.id} », version ${version}`
  + (version < art.versions.length ? ` (affichée par l'utilisateur ; la dernière est la ${art.versions.length})` : "");

class InspectArtifact extends PageTool {
  constructor() {
    super({
      name: TOOL.INSPECT_ARTIFACT,
      description: "Inspecte un artifact tel qu'il tourne dans le panneau, sous les yeux de "
        + "l'utilisateur : sa console et ses erreurs depuis le chargement (scripts, ressources "
        + "bloquées, promesses rejetées), et sa page en texte (un élément par ligne : balise, id, "
        + "classes, texte, valeurs). `screenshot` : une capture de la page, jointe si le modèle lit "
        + "les images. À utiliser quand l'utilisateur signale qu'un artifact ne marche pas, avant "
        + "de le corriger, et après pour vérifier.",
      params: {
        id: { type: "string", description: "L'identifiant de l'artifact." },
        screenshot: { type: "boolean", description: "Joindre une capture de la page (facultatif)." },
      },
      required: ["id"],
    });
  }

  /** @override @param {any} a */
  summary(a) { return [a.id, a.screenshot ? "capture" : ""].filter(Boolean).join(" · "); }

  /** @override @param {any} args @param {import("../contract.js").Ctx} ctx */
  async run(args, ctx) {
    let art;
    try { art = target(ctx.conv, args.id); } catch (e) { return failure(e.message); }
    let r;
    try { r = await bridge.call(art.id, art.versions.length, "inspect"); } catch (e) { return failure(e.message + "."); }
    if (r.error) return failure("inspection impossible : " + r.error);
    const errors = (r.logs || []).filter((l) => l.level === "error").length;
    const out = [`${which(art, r.version)} — fenêtre ${r.size[0]} × ${r.size[1]} px, page de ${r.scroll[2]} px de haut`
                 + (r.title ? `, titre « ${r.title} »` : "") + ".",
                 r.logs.length ? `Console (${r.logs.length} entrées, dont ${errors} erreur${errors > 1 ? "s" : ""}) :\n${consoleText(r.logs)}`
                   : "Console : vide, aucune erreur.",
                 "Page :\n" + r.outline];
    let image = "";
    if (args.screenshot) {
      try {
        const shot = await bridge.call(art.id, art.versions.length, "shot");
        if (shot.error) out.push("Capture impossible : " + shot.error);
        else if (!ctx.vision) out.push("Capture prise, non jointe : le modèle de la conversation ne lit pas les images.");
        else { image = shot.image; out.push(`Capture de la page (${shot.size[0]} × ${shot.size[1]} px) jointe à ce résultat.`); }
      } catch (e) {
        out.push("Capture impossible : " + e.message);
      }
    }
    const result = clip(out.join("\n\n"), MAX_OUT);
    return image ? { result, image, name: `${art.id}.jpg` } : result;
  }
}

class RunInArtifact extends PageTool {
  constructor() {
    super({
      name: TOOL.RUN_IN_ARTIFACT,
      description: "Exécute du JavaScript DANS un artifact tel qu'il tourne dans le panneau (l'état "
        + "que voit l'utilisateur), pour reproduire un bug ou lire un état : le corps d'une fonction "
        + "async, `return` la valeur voulue. Aides : click(sélecteur), type(sélecteur, texte), "
        + "key(sélecteur, touche), wait(ms), text(sélecteur), all(sélecteur) ; le DOM et les "
        + "variables globales de la page sont accessibles. Rend la valeur, et la console et les "
        + "erreurs survenues pendant l'exécution. Ne corrige rien : pour corriger, edit_artifact, "
        + "puis revérifier.",
      params: {
        id: { type: "string", description: "L'identifiant de l'artifact." },
        code: { type: "string", description: "Le corps de la fonction async (avec `return`)." },
      },
      required: ["id", "code"],
    });
  }

  /** @override @param {any} a */
  summary(a) { return [a.id, String(a.code || "").replace(/\s+/g, " ").slice(0, 100)].filter(Boolean).join(" · "); }

  /** @override @param {any} args @param {import("../contract.js").Ctx} ctx */
  async run(args, ctx) {
    const code = String(args.code || "");
    if (!code.trim()) return failure("`code` attendu.");
    let art;
    try { art = target(ctx.conv, args.id); } catch (e) { return failure(e.message); }
    let r;
    try { r = await bridge.call(art.id, art.versions.length, "run", { code }); } catch (e) { return failure(e.message + "."); }
    const out = [which(art, r.version) + " :"];
    if (r.error) out.push(failure(r.error));
    else out.push(r.value === undefined ? "(aucune valeur rendue)" : "Résultat : " + r.value);
    if (r.logs && r.logs.length) out.push("Console pendant l'exécution :\n" + consoleText(r.logs));
    return clip(out.join("\n"), MAX_OUT);
  }
}

export const FAMILIES = [defineFamily({
  key: FAMILY.ARTIFACTS, label: "Artifacts", hint: "le panneau latéral", fixed: true,
  tools: [new ShowArtifact(), new EditArtifact(), new ReadArtifact()],
}), defineFamily({
  key: FAMILY.ARTIFACT_DEBUG_OFFER, label: "Proposer le débogage", hint: "une carte pour le rétablir", fixed: true,
  enabled: (ctx) => artifactsOf(ctx.conv).some((a) => !debugOn(a)),
  guidance: (ctx) => `Débogage coupé par l'utilisateur pour : ${artifactsOf(ctx.conv).filter((a) => !debugOn(a)).map((a) => `« ${a.id} »`).join(", ")}. `
    + "S'il signale qu'un de ces artifacts ne marche pas (un bouton sans effet, une page blanche, "
    + "une erreur) : proposer de rétablir le débogage avec request_artifact_debug plutôt que de "
    + "deviner la cause.",
  tools: [new RequestArtifactDebug()],
}), defineFamily({
  key: FAMILY.ARTIFACT_DEBUG, label: "Déboguer un artifact", hint: "tester la page affichée", fixed: true,
  enabled: (ctx) => artifactsOf(ctx.conv).some(debugOn),
  guidance: (ctx) => `Débogage actif pour : ${artifactsOf(ctx.conv).filter(debugOn).map((a) => `« ${a.id} »`).join(", ")}. `
    + "Quand un de ces artifacts ne marche pas, ne pas deviner : l'inspecter (inspect_artifact : "
    + "console, erreurs, page), reproduire le problème (run_in_artifact), corriger (edit_artifact), "
    + "puis revérifier de la même façon.",
  tools: [new InspectArtifact(), new RunInArtifact()],
})];
