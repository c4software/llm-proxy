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

  Les artifacts sont gardés avec la conversation (conv.artifacts) ; leur
  rendu isolé est dans chat/artifacts.js.
*/
import { TOOL, FAMILY, PageTool, defineFamily, failure } from "../contract.js";

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

export const FAMILIES = [defineFamily({
  key: FAMILY.ARTIFACTS, label: "Artifacts", hint: "le panneau latéral", fixed: true,
  tools: [new ShowArtifact(), new EditArtifact(), new ReadArtifact()],
})];
