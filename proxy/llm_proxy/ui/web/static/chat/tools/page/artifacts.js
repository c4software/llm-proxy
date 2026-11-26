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

  Un PROJET (type `project`) : plusieurs fichiers {path, content} dans une
  version — chemins relatifs, sans doublon à la casse près, aucun fichier
  qui soit aussi un dossier ; au plus `max_files` fichiers et `max_chars`
  caractères en tout (settings, côté proxy). `path` y désigne le fichier
  que lisent, cherchent ou retouchent les outils ; read_artifact sans
  `path` en rend la liste, grep_artifact sans `path` cherche partout ;
  edit_artifact avec `content` crée ou remplace un fichier entier, avec
  `delete` le retire. Le débogage vise l'aperçu de son entrée (entryOf).

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
import * as text from "../text.js";
import * as bridge from "../../artifact-bridge.js";
import { debugOn, shotImage, entryOf } from "../../artifacts.js";
import { prompt } from "../../../lib/prompts.js";

// L'identifiant d'un artifact : minuscules, sans accents, des tirets.
const slug = (s) => String(s || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase()
  .replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "artifact";

// Les bornes (caractères au plus, fichiers au plus d'un projet) viennent
// de la déclaration, côté proxy (tools/page/page.py : settings).
const tooLong = (n) => failure(`artifact trop long (${n} caractères au plus, tous ses fichiers comptés).`);
const size = (v) => (v.type === "project" ? v.files.reduce((n, f) => n + f.content.length, 0) : v.content.length);

// Le chemin d'un fichier d'un PROJET : relatif, des segments de lettres,
// chiffres, « . », « _ », « - », séparés par « / » ; ni « . » ni « .. ».
const SEGMENT = /^[A-Za-z0-9._-]+$/;
function badPath(path) {
  if (typeof path !== "string" || !path) return "chemin vide";
  const parts = path.split("/");
  return parts.every((p) => SEGMENT.test(p) && p !== "." && p !== "..") ? ""
    : `chemin « ${path} » invalide (relatif, segments [A-Za-z0-9._-] séparés par « / », ni « . » ni « .. »)`;
}

// Un chemin qui en heurte un autre du projet : le même à la casse près
// (deux fichiers qu'un système de fichiers confondrait), ou un fichier qui
// serait aussi un dossier (« a » et « a/b »). "" sinon.
function clash(path, others) {
  const low = path.toLowerCase();
  for (const o of others) {
    const l = o.toLowerCase();
    if (l === low) return `fichier « ${path} » en double (avec « ${o} », à la casse près).`;
    if (l.startsWith(low + "/") || low.startsWith(l + "/")) {
      return `« ${path} » et « ${o} » : un fichier ne peut pas être aussi un dossier.`;
    }
  }
  return "";
}

// Les fichiers d'un projet, vérifiés : [{path, content}] ou {error}.
function projectFiles(files, { max_files }) {
  if (!Array.isArray(files) || !files.length) return { error: "`files` : la liste des fichiers du projet ({path, content})." };
  if (files.length > max_files) return { error: `trop de fichiers (${max_files} au plus).` };
  const out = [];
  for (const f of files) {
    const path = f && f.path;
    const bad = badPath(path) || clash(path, out.map((o) => o.path));
    if (bad) return { error: bad.endsWith(".") ? bad : bad + "." };
    if (typeof f.content !== "string") return { error: `fichier « ${path} » : \`content\` est un texte.` };
    out.push({ path, content: f.content });
  }
  return { files: out };
}

// show_artifact : la version rejoint conv.artifacts ; rend l'id montré.
function showArtifact(conv, args, settings) {
  const type = settings.types.includes(args.type) ? args.type : "";
  if (!type) return { result: failure(`\`type\` doit être l'un de ${settings.types.join(", ")}.`) };
  let version;
  if (type === "project") {
    const got = projectFiles(args.files, settings);
    if ("error" in got) return { result: failure(got.error) };
    version = { type, files: got.files, at: Date.now() };
  } else {
    const content = String(args.content ?? "");
    if (!content.trim()) return { result: failure("`content` est vide.") };
    version = { type, content, language: args.language || "", at: Date.now() };
  }
  if (size(version) > settings.max_chars) return { result: tooLong(settings.max_chars) };
  const title = String(args.title || "Sans titre").trim().slice(0, 120);
  const id = slug(args.id || title);
  const list = conv.artifacts || (conv.artifacts = []);
  let art = list.find((a) => a.id === id);
  if (!art) { list.push({ id, title, versions: [] }); art = list[list.length - 1]; }
  art.title = title;
  art.versions.push(version);
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
const latest = (art) => art.versions[art.versions.length - 1];
const pathsOf = (v) => v.files.map((f) => `« ${f.path} »`).join(", ");

// Le texte que lit ou retouche un outil : celui de l'artifact, ou, pour un
// projet, celui du fichier `path` → {content, file?} ou {error}.
function textOf(art, v, path) {
  if (v.type !== "project") return { content: v.content };
  if (path === undefined || path === "") return { error: `« ${art.id} » est un projet : \`path\` désigne le fichier (${pathsOf(v)}).` };
  const file = v.files.find((f) => f.path === path);
  return file ? { content: file.content, file } : { error: `fichier « ${path} » absent du projet « ${art.id} » (${pathsOf(v)}).` };
}

// edit_artifact : les remplacements, tous ou aucun → une nouvelle version.
// Pour un projet, dans le fichier `path` ; ou ce fichier créé / remplacé
// entier (`content`), ou retiré (`delete`).
function editArtifact(conv, args, settings) {
  const art = findArtifact(conv, args.id);
  if (!art) return { result: failure(`artifact « ${args.id} » inconnu (artifacts : ${known(conv)}).`) };
  const last = latest(art);
  const project = last.type === "project";
  if (!project && (args.path !== undefined || args.content !== undefined || args.delete)) {
    return { result: failure(`« ${art.id} » n'est pas un projet : \`path\`, \`content\` et \`delete\` sont pour un projet ; `
      + "ici, `edits`.") };
  }
  if (args.content !== undefined && args.edits !== undefined) {
    return { result: failure("`content` (le fichier entier) ou `edits` (des passages), pas les deux.") };
  }
  if (args.content !== undefined && typeof args.content !== "string") return { result: failure("`content` est un texte.") };
  let next, what;
  if (project && (args.delete || args.content !== undefined)) {
    const bad = badPath(args.path);
    if (bad) return { result: failure(bad + ".") };
    const others = last.files.filter((f) => f.path !== args.path);
    if (args.delete) {
      if (others.length === last.files.length) return { result: failure(`fichier « ${args.path} » absent du projet « ${art.id} » (${pathsOf(last)}).`) };
      if (!others.length) return { result: failure("un projet garde au moins un fichier.") };
      next = { ...last, files: others };
      what = `fichier « ${args.path} » retiré`;
    } else {
      const added = others.length === last.files.length;
      if (added && last.files.length >= settings.max_files) return { result: failure(`trop de fichiers (${settings.max_files} au plus).`) };
      const hit = clash(args.path, others.map((f) => f.path));
      if (hit) return { result: failure(hit) };
      const file = { path: args.path, content: args.content };
      next = { ...last, files: added ? [...last.files, file] : last.files.map((f) => (f.path === args.path ? file : f)) };
      what = `fichier « ${args.path} » ${added ? "créé" : "remplacé"}`;
    }
  } else {
    const t = textOf(art, last, args.path);
    if ("error" in t) return { result: failure(t.error) };
    const edits = Array.isArray(args.edits) ? args.edits : [];
    const done = text.applyEdits(t.content, edits,
      { grep: "grep_artifact", read: "read_artifact", at: `dans la version ${art.versions.length}` });
    if ("error" in done) return { result: done.error };
    next = t.file ? { ...last, files: last.files.map((f) => (f === t.file ? { ...f, content: done.content } : f)) }
      : { ...last, content: done.content };
    what = "retouché " + text.editCount(edits.length, done.passages) + (t.file ? ` dans « ${t.file.path} »` : "");
  }
  if (size(next) > settings.max_chars) return { result: tooLong(settings.max_chars) };
  if (args.title) art.title = String(args.title).trim().slice(0, 120);
  art.versions.push({ ...next, at: Date.now() });
  return { id: art.id, result: `Artifact « ${art.id} » ${what} : version ${art.versions.length}, affichée dans le panneau.` };
}

// read_artifact : la dernière version, entière (telle quelle), ou une
// tranche de lignes numérotées. Un projet sans `path` : ses fichiers.
function readArtifact(conv, args, settings) {
  const art = findArtifact(conv, args.id);
  if (!art) return failure(`artifact « ${args.id} » inconnu (artifacts : ${known(conv)}).`);
  const last = latest(art);
  if (last.type === "project" && !args.path) {
    return `Projet « ${art.id} », version ${art.versions.length}, ${last.files.length} fichiers `
      + "(read_artifact avec `path` pour en lire un) :\n"
      + last.files.map((f) => `${f.path} — ${f.content.split("\n").length} lignes, ${f.content.length} caractères`).join("\n");
  }
  const t = textOf(art, last, args.path);
  if ("error" in t) return failure(t.error);
  const head = `Artifact « ${art.id} »${t.file ? `, fichier « ${t.file.path} »` : ""}, version ${art.versions.length} `
    + `(${t.file ? "projet" : last.type}, ${t.content.length} caractères`;
  if (args.start_line === undefined && args.end_line === undefined) return `${head}) :\n${t.content}`;
  const s = text.slice(t.content, args.start_line, args.end_line, settings);
  if ("error" in s) return failure(`l'artifact ${s.error}`);
  return `${head}, ${s.total} lignes) — lignes ${s.start} à ${s.end} (le numéro et « → » ne font pas `
    + `partie du texte, pour old_string) :\n${s.body}`;
}

// grep_artifact : les lignes trouvées, numérotées, avec leur contexte
// (« → » : trouvée ; « · » : contexte ; « … » : saut). Un projet : dans
// chacun de ses fichiers (ou `path`), chaque bloc précédé de son chemin.
function grepArtifact(conv, args, settings) {
  const art = findArtifact(conv, args.id);
  if (!art) return failure(`artifact « ${args.id} » inconnu (artifacts : ${known(conv)}).`);
  const p = text.pattern(args);
  if ("error" in p) return failure(p.error);
  const last = latest(art);
  const at = `« ${art.id} », version ${art.versions.length}`;
  if (last.type !== "project" || args.path) {
    const t = textOf(art, last, args.path);
    if ("error" in t) return failure(t.error);
    const g = text.grep(t.content, p.re, text.contextOf(args, settings));
    const where = t.file ? `${at}, fichier « ${t.file.path} »` : at;
    if (!g.hits) return `Aucune ligne de ${where} (${g.total} lignes) ne contient « ${args.pattern} ».`;
    return `${where} (${g.total} lignes) : ${text.found(g)}`
      + ` (le numéro et la marque ne font pas partie du texte) :\n${g.lines.join("\n")}`;
  }
  const blocks = last.files.map((f) => ({ f, g: text.grep(f.content, p.re, text.contextOf(args, settings)) }))
    .filter(({ g }) => g.hits);
  if (!blocks.length) return `Aucune ligne de ${at} (${last.files.length} fichiers) ne contient « ${args.pattern} ».`;
  return `${at} : trouvé dans ${blocks.length} fichier${blocks.length > 1 ? "s" : ""} sur ${last.files.length}`
    + " (le numéro et la marque ne font pas partie du texte) :\n"
    + blocks.map(({ f, g }) => `${f.path} — ${text.found(g)} :\n${g.lines.join("\n")}`).join("\n\n");
}

class ShowArtifact extends PageTool {
  constructor() {
    super({ name: TOOL.SHOW_ARTIFACT });
  }

  /** @override @param {any} args @param {import("../contract.js").Ctx} ctx */
  run(args, ctx) {
    const { id, result } = showArtifact(ctx.conv, args, this.settings);
    return id ? { result, artifact: id } : result;
  }

  /** @override @param {any} a */
  summary(a) { return [a.title, a.type].filter(Boolean).join(" · "); }

}

class EditArtifact extends PageTool {
  constructor() {
    super({ name: TOOL.EDIT_ARTIFACT });
  }

  /** @override @param {any} args @param {import("../contract.js").Ctx} ctx */
  run(args, ctx) {
    const { id, result } = editArtifact(ctx.conv, args, this.settings);
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
    super({ name: TOOL.READ_ARTIFACT });
  }

  /** @override @param {any} args @param {import("../contract.js").Ctx} ctx */
  run(args, ctx) { return readArtifact(ctx.conv, args, this.settings); }

  /** @override @param {any} a */
  summary(a) { return [a.id, a.start_line ? `lignes ${a.start_line}–${a.end_line || "…"}` : ""].filter(Boolean).join(" · "); }
}

class GrepArtifact extends PageTool {
  constructor() {
    super({ name: TOOL.GREP_ARTIFACT });
  }

  /** @override @param {any} args @param {import("../contract.js").Ctx} ctx */
  run(args, ctx) { return grepArtifact(ctx.conv, args, this.settings); }

  /** @override @param {any} a */
  summary(a) { return [a.id, a.pattern].filter(Boolean).join(" · "); }
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
  // Un projet : l'aperçu de son fichier HTML d'entrée (entryOf).
  if (latest(art).type === "project" && !entryOf(latest(art))) {
    throw new Error(`le projet « ${art.id} » n'a pas de fichier HTML à afficher : rien à inspecter.`);
  }
  return art;
}

/** @param {any} conv */
const artifactsOf = (conv) => (conv && conv.artifacts) || [];

class RequestArtifactDebug extends PageTool {
  constructor() {
    super({ name: TOOL.REQUEST_ARTIFACT_DEBUG });
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
    super({ name: TOOL.INSPECT_ARTIFACT });
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
        // La réponse vient du cadre (non fiable) : une image vérifiée, ou rien.
        const ok = shot.error ? null : shotImage(shot);
        if (shot.error) out.push("Capture impossible : " + String(shot.error).slice(0, 500));
        else if (!ok) out.push("Capture refusée : l'artifact n'a pas rendu une image JPEG ou PNG valide.");
        else if (!ctx.vision) out.push("Capture prise, non jointe : le modèle de la conversation ne lit pas les images.");
        else { image = ok.image; out.push(`Capture de la page (${ok.size[0]} × ${ok.size[1]} px) jointe à ce résultat.`); }
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
    super({ name: TOOL.RUN_IN_ARTIFACT });
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
  key: FAMILY.ARTIFACTS,
  // Leur liste, et leur version : l'état, joint au tour (l'historique garde
  // les anciens contenus tels qu'ils ont été vus ; la version actuelle,
  // c'est celle-ci). Comment les modifier : la consigne, déclarée côté proxy.
  state: (ctx) => {
    const arts = artifactsOf(ctx.conv);
    if (!arts.length) return "";
    return prompt("family.artifacts.state", { list: arts.map((a) => {
      const v = latest(a);
      return v.type === "project"
        ? prompt("family.artifacts.state_item_project",
                 { id: a.id, files: v.files.length, version: a.versions.length, chars: size(v) })
        : prompt("family.artifacts.state_item",
                 { id: a.id, type: v.type, version: a.versions.length, chars: size(v) });
    }).join(", ") });
  },
  tools: [new ShowArtifact(), new EditArtifact(), new ReadArtifact(), new GrepArtifact()],
}), defineFamily({
  key: FAMILY.ARTIFACT_DEBUG_OFFER,
  // Toujours déclarée (les outils ne changent pas d'un tour à l'autre) :
  // request_artifact_debug répond lui-même si le débogage est déjà actif.
  state: (ctx) => {
    const off = artifactsOf(ctx.conv).filter((a) => !debugOn(a));
    return off.length ? prompt("family.artifact_debug_offer.state",
      { list: off.map((a) => prompt("family.artifact_debug_offer.state_item", { id: a.id })).join(", ") }) : "";
  },
  tools: [new RequestArtifactDebug()],
}), defineFamily({
  key: FAMILY.ARTIFACT_DEBUG,
  // Toujours déclarée : inspect_artifact et run_in_artifact refusent un
  // artifact au débogage coupé (target), en renvoyant à request_artifact_debug.
  tools: [new InspectArtifact(), new RunInArtifact()],
})];
