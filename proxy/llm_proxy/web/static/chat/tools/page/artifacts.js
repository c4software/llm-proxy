// @ts-check
/*
  Les ARTIFACTS : ce que le modèle montre dans le panneau latéral du chat
  (ArtifactPanel.js) — une page, un document, un schéma, un SVG, du code.

    show_artifact  une nouvelle version (un même `id` : la suivante)
    edit_artifact  une RETOUCHE : des remplacements de passages exacts dans
                   la dernière version, qui en font une nouvelle — sans
                   réécrire (ni re-streamer) tout l'artifact
    read_artifact  la dernière version, telle qu'elle est (après des
                   retouches, le modèle ne l'a plus en entier sous les yeux),
                   ou une tranche de lignes numérotées
    grep_artifact  chercher dans la dernière version : les lignes trouvées,
                   numérotées, avec leur contexte — retrouver un passage
                   sans tout relire

  Le chemin d'une retouche : grep_artifact (où ?), read_artifact sur la
  tranche (le texte exact), edit_artifact (le remplacer).

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

// Ce que l'historique garde du contenu d'une version passée.
/** @param {number} chars */
const stale = (chars) => `[contenu d'une version passée (${chars} caractères), non rejoué : read_artifact rend la version actuelle]`;

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
  let passages = 0;                      // passages remplacés, toutes retouches
  for (const [k, e] of edits.entries()) {
    const from = String((e && e.old_string) ?? "");
    const to = String((e && e.new_string) ?? "");
    const where = edits.length > 1 ? `retouche ${k + 1} : ` : "";
    if (!from) return { result: `Error: ${where}\`old_string\` est vide.` };
    if (e.regex) {
      // Une expression régulière (JavaScript) ; `new_string` peut citer
      // ses groupes ($1, $<nom>). Unique, sauf replace_all.
      let re;
      try { re = new RegExp(from, "g" + (e.ignore_case ? "i" : "") + "m"); } catch (err) {
        return { result: `Error: ${where}expression invalide : ${err.message}. Rien n'a été modifié.` };
      }
      const hits = [...content.matchAll(re)].length;
      if (!hits) {
        return { result: `Error: ${where}l'expression ne trouve rien dans la version ${art.versions.length} `
          + "(grep_artifact pour vérifier). Rien n'a été modifié." };
      }
      if (hits > 1 && !e.replace_all) {
        return { result: `Error: ${where}l'expression trouve ${hits} passages ; la préciser, ou \`replace_all\`. `
          + "Rien n'a été modifié." };
      }
      content = content.replace(re, to);
      passages += e.replace_all ? hits : 1;
      continue;
    }
    const count = content.split(from).length - 1;
    if (!count) {
      return { result: `Error: ${where}passage introuvable dans la version ${art.versions.length}. `
        + "Le texte doit être EXACT (espaces, retours à la ligne) ; grep_artifact pour le retrouver, "
        + "read_artifact sur ses lignes pour le relire. Rien n'a été modifié." };
    }
    if (count > 1 && !e.replace_all) {
      return { result: `Error: ${where}passage présent ${count} fois ; l'allonger pour qu'il soit `
        + "unique, ou `replace_all`. Rien n'a été modifié." };
    }
    content = e.replace_all ? content.split(from).join(to) : content.replace(from, () => to);
    passages += e.replace_all ? count : 1;
  }
  if (content.length > MAX_ARTIFACT) return { result: `Error: artifact trop long (${MAX_ARTIFACT} caractères au plus).` };
  if (args.title) art.title = String(args.title).trim().slice(0, 120);
  art.versions.push({ ...last, content, at: Date.now() });
  const n = art.versions.length;
  const plural = (k, w) => `${k} ${w}${k > 1 ? "s" : ""}`;
  return { id: art.id, result: `Artifact « ${art.id} » retouché (${plural(edits.length, "retouche")}, `
    + `${plural(passages, "passage")} remplacé${passages > 1 ? "s" : ""}) : version ${n}, affichée dans le panneau.` };
}

const MAX_LINE = 300;           // caractères d'une ligne rendue par grep
const MAX_HITS = 60;            // lignes trouvées par grep, au plus
const MAX_SLICE = 400;          // lignes d'une tranche de read

/** Une ligne numérotée : `   42→ texte` (le numéro ne fait pas partie du texte).
 * @param {number} n @param {string} line @param {string} [mark] */
const numbered = (n, line, mark = "→") => `${String(n).padStart(5)}${mark} ${line}`;

// read_artifact : la dernière version, entière (telle quelle), ou une
// tranche de lignes numérotées.
function readArtifact(conv, args) {
  const art = findArtifact(conv, args.id);
  if (!art) return failure(`artifact « ${args.id} » inconnu (artifacts : ${known(conv)}).`);
  const last = art.versions[art.versions.length - 1];
  const head = `Artifact « ${art.id} », version ${art.versions.length} (${last.type}, `
    + `${last.content.length} caractères`;
  if (args.start_line === undefined && args.end_line === undefined) return `${head}) :\n${last.content}`;
  const lines = last.content.split("\n");
  const start = Math.max(1, Math.floor(Number(args.start_line) || 1));
  if (start > lines.length) return failure(`l'artifact n'a que ${lines.length} lignes.`);
  const end = Math.min(lines.length, Math.floor(Number(args.end_line) || start + 80), start + MAX_SLICE - 1);
  const body = lines.slice(start - 1, end).map((l, k) => numbered(start + k, l));
  return `${head}, ${lines.length} lignes) — lignes ${start} à ${end} (le numéro et « → » ne font pas `
    + `partie du texte, pour old_string) :\n${body.join("\n")}`;
}

// grep_artifact : les lignes trouvées, numérotées, avec leur contexte
// (« → » : trouvée ; « · » : contexte ; « … » : saut).
function grepArtifact(conv, args) {
  const art = findArtifact(conv, args.id);
  if (!art) return failure(`artifact « ${args.id} » inconnu (artifacts : ${known(conv)}).`);
  const pattern = String(args.pattern || "");
  if (!pattern) return failure("`pattern` attendu.");
  const flags = args.ignore_case === false ? "" : "i";
  let re;
  try {
    re = new RegExp(args.regex ? pattern : pattern.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), flags);
  } catch (e) { return failure(`expression invalide : ${e.message}`); }
  const last = art.versions[art.versions.length - 1];
  const lines = last.content.split("\n");
  const ctx = Math.max(0, Math.min(10, Math.floor(Number(args.context ?? 2))));
  const hits = [];
  lines.forEach((l, i) => { if (re.test(l)) hits.push(i); });
  if (!hits.length) {
    return `Aucune ligne de « ${art.id} » (version ${art.versions.length}, ${lines.length} lignes) ne contient « ${pattern} ».`;
  }
  const shown = hits.slice(0, MAX_HITS);
  const keep = new Map();                    // ligne → trouvée ?
  for (const h of shown) {
    for (let i = Math.max(0, h - ctx); i <= Math.min(lines.length - 1, h + ctx); i++) keep.set(i, keep.get(i) || i === h);
  }
  const out = [];
  let prev = -2;
  for (const i of [...keep.keys()].sort((a, b) => a - b)) {
    if (prev >= 0 && i > prev + 1) out.push("    …");
    const l = lines[i].length > MAX_LINE ? lines[i].slice(0, MAX_LINE) + " …" : lines[i];
    out.push(numbered(i + 1, l, keep.get(i) ? "→" : "·"));
    prev = i;
  }
  return `« ${art.id} », version ${art.versions.length} (${lines.length} lignes) : ${hits.length} ligne`
    + `${hits.length > 1 ? "s" : ""} trouvée${hits.length > 1 ? "s" : ""}`
    + (hits.length > shown.length ? ` (les ${shown.length} premières)` : "")
    + ` (le numéro et la marque ne font pas partie du texte) :\n${out.join("\n")}`;
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

  // Une réponse passée : son contenu n'est plus la version actuelle (des
  // versions et des retouches ont pu suivre) — il n'est pas rejoué.
  /** @override @param {{arguments: string, result: string}} call */
  historic(call) {
    let a;
    try { a = JSON.parse(call.arguments); } catch { return call; }
    if (typeof a.content !== "string") return call;
    a.content = stale(a.content.length);
    return { arguments: JSON.stringify(a), result: call.result };
  }
}

class EditArtifact extends PageTool {
  constructor() {
    super({
      name: TOOL.EDIT_ARTIFACT,
      description: "Retouche un artifact déjà affiché SANS le réécrire : chaque `old_string`, "
        + "un passage exact et unique de sa dernière version (espaces et retours à la ligne "
        + "compris, assez long pour être unique), est remplacé par `new_string` — ou, avec "
        + "`regex`, une expression régulière (un changement répétitif : `replace_all`). À préférer à "
        + "show_artifact pour toute modification qui ne change pas l'essentiel. Toutes les "
        + "retouches s'appliquent, ou aucune. Pour trouver le passage : grep_artifact, puis "
        + "read_artifact sur ses lignes.",
      params: {
        id: { type: "string", description: "L'identifiant de l'artifact." },
        edits: { type: "array", minItems: 1, items: {
          type: "object", required: ["old_string", "new_string"], properties: {
            old_string: { type: "string", description: "Le passage exact à remplacer." },
            new_string: { type: "string", description: "Ce qui le remplace (vide : le supprimer)." },
            replace_all: { type: "boolean", description: "Remplacer toutes ses occurrences (sinon il doit être unique)." },
            regex: { type: "boolean", description: "`old_string` est une expression régulière (JavaScript, multiligne) ; `new_string` peut citer ses groupes ($1). Pour un changement répétitif ; sinon, le texte exact." },
            ignore_case: { type: "boolean", description: "Avec `regex` : sans tenir compte de la casse." },
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
      description: "Rend la dernière version d'un artifact : entière, telle quelle, ou une tranche "
        + "de lignes numérotées (`start_line`, `end_line`) — à préférer pour un gros artifact, après "
        + "grep_artifact qui dit où chercher. Avant de le retoucher quand son texte exact n'est plus "
        + "sûr.",
      params: {
        id: { type: "string", description: "L'identifiant de l'artifact." },
        start_line: { type: "integer", description: "Première ligne (à partir de 1) ; absente avec end_line : l'artifact entier." },
        end_line: { type: "integer", description: `Dernière ligne, incluse (défaut : 80 lignes ; ${MAX_SLICE} au plus).` },
      },
      required: ["id"],
    });
  }

  /** @override @param {any} args @param {import("../contract.js").Ctx} ctx */
  run(args, ctx) { return readArtifact(ctx.conv, args); }

  /** @override @param {any} a */
  summary(a) { return [a.id, a.start_line ? `lignes ${a.start_line}–${a.end_line || "…"}` : ""].filter(Boolean).join(" · "); }

  /** @override @param {{arguments: string, result: string}} call */
  historic(call) {
    const head = call.result.split("\n")[0];
    return { arguments: call.arguments, result: call.result.startsWith("Artifact « ")
      ? head + "\n" + stale(call.result.length - head.length - 1) : call.result };
  }
}

class GrepArtifact extends PageTool {
  constructor() {
    super({
      name: TOOL.GREP_ARTIFACT,
      description: "Cherche dans la dernière version d'un artifact, sans le relire en entier : rend "
        + "les lignes qui contiennent `pattern` (texte, ou expression régulière avec `regex`), "
        + "numérotées, avec `context` lignes autour. Pour retrouver un passage avant de le lire "
        + "(read_artifact sur ses lignes) puis de le retoucher (edit_artifact).",
      params: {
        id: { type: "string", description: "L'identifiant de l'artifact." },
        pattern: { type: "string", description: "Le texte cherché (ou une expression régulière si `regex`)." },
        regex: { type: "boolean", description: "`pattern` est une expression régulière (JavaScript)." },
        ignore_case: { type: "boolean", description: "Sans tenir compte de la casse (défaut : oui)." },
        context: { type: "integer", description: "Lignes de contexte avant et après (défaut 2, 10 au plus)." },
      },
      required: ["id", "pattern"],
    });
  }

  /** @override @param {any} args @param {import("../contract.js").Ctx} ctx */
  run(args, ctx) { return grepArtifact(ctx.conv, args); }

  /** @override @param {any} a */
  summary(a) { return [a.id, a.pattern].filter(Boolean).join(" · "); }

  // Une recherche passée : ses numéros de ligne ont pu changer depuis.
  /** @override @param {{arguments: string, result: string}} call */
  historic(call) {
    const head = call.result.split("\n")[0];
    return { arguments: call.arguments, result: call.result.includes("\n")
      ? head + "\n[lignes d'une version passée, non rejouées : relancer grep_artifact sur la version actuelle]" : call.result };
  }
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
  // Les artifacts de la conversation, et comment les modifier : l'historique
  // ne rejoue pas leurs anciens contenus (historic).
  guidance: (ctx) => {
    const arts = artifactsOf(ctx.conv);
    if (!arts.length) return "";
    const list = arts.map((a) => {
      const v = a.versions[a.versions.length - 1];
      return `« ${a.id} » (${v.type}, version ${a.versions.length}, ${v.content.length} caractères)`;
    }).join(", ");
    return `Artifacts de la conversation : ${list}. Pour en modifier un : grep_artifact pour trouver `
      + "le passage, read_artifact sur ses lignes (start_line, end_line) pour en avoir le texte exact, "
      + "puis edit_artifact ; ne pas le relire en entier sans besoin. show_artifact seulement pour un "
      + "nouvel artifact, ou une refonte que l'utilisateur demande.";
  },
  tools: [new ShowArtifact(), new EditArtifact(), new ReadArtifact(), new GrepArtifact()],
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
