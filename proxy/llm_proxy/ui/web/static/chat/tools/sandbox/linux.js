// @ts-check
/*
  LINUX : un script bash exécuté dans la VM Linux de la page (linuxvm.js) —
  une Alpine i386 émulée en WASM par v86, sans réseau, que l'utilisateur
  voit aussi dans le panneau Terminal. Le dossier courant est /work, gardé
  avec la conversation ; les fichiers de `files` y sont copiés à leur
  chemin ; ce que le script écrit sous /out pendant l'appel est rendu sous
  la réponse (files.js), et y reste : /out est /work/out, gardé avec /work
  — le terminal montre ce que la réponse présente.

  Le panneau Terminal ouvert (il s'ouvre au premier appel), la commande
  est TAPÉE dans le terminal de l'utilisateur — un script de plusieurs
  lignes en un bloc `( … )` : il la voit partir, s'exécuter, et peut
  l'interrompre ; le modèle en reçoit la sortie et le code. Une commande
  qui dure au-delà du délai (un serveur) y reste, laissée à l'utilisateur.

  Pour ÉDITER les fichiers de la VM, le chemin des artifacts (text.js) :
    shell_grep   chercher dans un fichier ou un dossier : les lignes
                 trouvées, numérotées, avec leur contexte
    shell_read   un fichier, entier ou une tranche de lignes numérotées
    shell_edit   des remplacements de passages exacts (ou d'une expression
                 régulière), tous ou aucun — sans réécrire le fichier
    shell_write  un fichier entier (créé, ou remplacé)
  Les chemins : absolus, ou relatifs à /work. Chacun ouvre le terminal,
  comme run_shell.
*/
import { TOOL, FAMILY, PageTool, defineFamily, failure, oneLine } from "../contract.js";
import { FILES_PARAM, paths as pathsOf, inputs, deliver } from "./files.js";
import * as linux from "./linuxvm.js";
import * as text from "../text.js";

const TIMEOUT = 60;                  // secondes, par défaut
const MAX_TIMEOUT = 600;
const HEAD = 6_000, TAIL = 14_000;   // caractères de sortie gardés : le début, et surtout la fin

// Une longue sortie : son début et sa fin (une erreur est souvent à la fin).
function cut(/** @type {string} */ s) {
  if (s.length <= HEAD + TAIL) return s;
  return s.slice(0, HEAD) + `\n… (${s.length - HEAD - TAIL} caractères coupés) …\n` + s.slice(-TAIL);
}

class RunShell extends PageTool {
  constructor() {
    super({
      name: TOOL.RUN_SHELL,
      description: "Exécute un script bash dans une VM Linux du navigateur (Alpine, x86 32 bits, "
        + "émulée en WASM), sans réseau sauf si l'utilisateur l'ouvre : bash, les outils GNU (coreutils, "
        + "findutils, grep, sed, diff, patch), python3 et sa bibliothèque standard, git, jq, sqlite3, make, "
        + "bc, curl, htop, less, tree, xz, et busybox. Rien ne s'installe (ni apk, ni pip). "
        + "Le dossier courant est "
        + "/work, gardé avec la conversation d'un appel à l'autre ; l'utilisateur voit et utilise la même "
        + "machine dans le panneau Terminal. Les fichiers de `files` (dossier ouvert ou pièces jointes, "
        + "qui restent sur l'appareil) sont copiés sous /work à leur chemin ; les fichiers que le script "
        + "écrit sous /out pendant l'appel (texte, ou image png/jpeg/webp/gif) sont rendus sous la "
        + "réponse, et restent dans la VM (/out est /work/out, gardé avec /work). Rend la sortie "
        + "(stdout et stderr mêlés) et le code de sortie. Quand l'utilisateur a ouvert le panneau "
        + "Terminal, la commande y est tapée sous ses yeux (« lance … », « montre-moi … ») — un script de "
        + "plusieurs lignes en un bloc ( … ), un sous-shell : il la voit s'exécuter ; une commande qui dure "
        + "au-delà du délai (un serveur, un programme interactif) reste alors dans son terminal. "
        + "Émulée, la machine est lente : quelques secondes pour un gros calcul Python.",
      params: {
        script: { type: "string", description: "Le script bash, exécuté dans /work." },
        ...FILES_PARAM,
        timeout: { type: "number", description: `Délai en secondes (défaut ${TIMEOUT}, ${MAX_TIMEOUT} au plus) ; le script est tué au-delà.` },
        reset: { type: "boolean", description: "Repartir d'une machine neuve et d'un /work vide (défaut : non)." },
        terminal: { type: "boolean", description: "Taper la commande dans le terminal de l'utilisateur s'il est ouvert (défaut : oui) ; false : l'exécuter sans la montrer." },
      },
      required: ["script"],
    });
  }

  /** @override @param {string} convId */
  forget(convId) { return linux.forget(convId); }

  /** @override @param {any} a */
  summary(a) { return oneLine([...(a.files || []), a.script].filter(Boolean).join(" · ")); }

  /** @override @param {any} args @param {import("../contract.js").Ctx} ctx */
  async run(args, ctx) {
    const script = String(args.script || "");
    if (!script.trim()) return failure("`script` attendu.");
    const conv = ctx.conv;
    if (!conv || !conv.id) return failure("pas de conversation.");
    const paths = pathsOf(args);
    const bad = paths.find((p) => p.split("/").includes(".."));
    if (bad) return failure(`chemin « ${bad} » : « .. » n'est pas permis.`);
    let files;
    try { files = await inputs(ctx, paths); } catch (e) { return failure(e.message + "."); }
    const timeout = Math.min(MAX_TIMEOUT, Math.max(1, Number(args.timeout) || TIMEOUT));
    let r;
    try {
      if (args.reset) await linux.restart(conv, { wipe: true });
      r = await linux.exec(conv, script, { timeout, inputs: files, out: true, terminal: args.terminal !== false });
    } catch (e) {
      return failure(`VM Linux : ${e.message}.`);
    }
    const text = new TextDecoder().decode(r.log);
    const out = [];
    if (r.running) {
      out.push(`Tapée dans le terminal de l'utilisateur, et toujours en cours après ${timeout} s : elle y reste, `
        + "sous ses yeux (Ctrl-C l'arrête). Tant qu'elle tourne, les appels suivants passent hors du terminal.");
    } else {
      if (r.terminal) out.push("Tapée dans le terminal de l'utilisateur, qui l'a vue s'exécuter.");
      else if (r.busy) out.push("Terminal de l'utilisateur occupé (une commande en cours, ou une ligne qu'il tape) : exécutée hors du terminal.");
      else if (r.long) out.push("Script trop long pour être tapé dans le terminal (ou avec des tabulations) : exécuté hors du terminal.");
      out.push(r.exitCode === 137 && !r.terminal ? `Tué : délai de ${timeout} s dépassé (code 137).` : `Code de sortie : ${r.exitCode}`);
    }
    out.push(text ? "Sortie :\n" + cut(text) : "(aucune sortie)");
    const got = await deliver(ctx, r.outputs);
    out.push(...got.lines);
    const saved = await linux.save().catch((e) => `/work non gardé : ${e.message}`);
    if (saved) out.push(saved + ".");
    const result = out.join("\n");
    return got.image ? { result, image: got.image } : result;
  }
}

// ── éditer les fichiers de la VM ────────────────────────────────────────
const MAX_WHOLE = 60_000;            // caractères d'un fichier rendu entier
const PATH = { type: "string", description: "Le chemin du fichier : absolu, ou relatif à /work." };
const wrap = async (/** @type {() => Promise<string>} */ f) => {
  try { return await f(); } catch (e) { return failure(`VM Linux : ${e.message}.`); }
};
// Ce que l'historique garde d'une lecture ou d'une recherche passée : le
// fichier a pu changer depuis.
/** @param {{arguments: string, result: string}} call @param {string} again */
const staleCall = (call, again) => {
  const head = call.result.split("\n")[0];
  return { arguments: call.arguments, result: call.result.includes("\n") && !call.result.startsWith("Error")
    ? head + `\n[contenu d'un appel passé, non rejoué : ${again}]` : call.result };
};

class ShellRead extends PageTool {
  constructor() {
    super({
      name: TOOL.SHELL_READ,
      description: "Lit un fichier texte de la VM Linux : entier, ou une tranche de lignes numérotées "
        + "(`start_line`, `end_line`) — à préférer pour un gros fichier, après shell_grep qui dit où "
        + "chercher. Avant shell_edit quand le texte exact n'est plus sûr.",
      params: {
        path: PATH,
        start_line: { type: "integer", description: "Première ligne (à partir de 1) ; absente avec end_line : le fichier entier." },
        end_line: { type: "integer", description: `Dernière ligne, incluse (défaut : 80 lignes ; ${text.MAX_SLICE} au plus).` },
      },
      required: ["path"],
    });
  }

  /** @override @param {any} args @param {import("../contract.js").Ctx} ctx */
  run(args, ctx) {
    return wrap(async () => {
      const f = linux.absolute(args.path);
      const content = await linux.readFile(ctx.conv, f);
      const lines = content.split("\n").length;
      const head = `Fichier « ${f} » (${content.length} caractères, ${lines} ligne${lines > 1 ? "s" : ""})`;
      if (args.start_line === undefined && args.end_line === undefined) {
        if (content.length <= MAX_WHOLE) return `${head} :\n${content}`;
        args = { start_line: 1, end_line: text.MAX_SLICE };
      }
      const t = text.slice(content, args.start_line, args.end_line);
      if ("error" in t) return failure(`le fichier ${t.error}`);
      return `${head} — lignes ${t.start} à ${t.end}${t.end < t.total ? ` (suite : start_line ${t.end + 1})` : ""} `
        + `(le numéro et « → » ne font pas partie du texte, pour old_string) :\n${t.body}`;
    });
  }

  /** @override @param {any} a */
  summary(a) { return [a.path, a.start_line ? `lignes ${a.start_line}–${a.end_line || "…"}` : ""].filter(Boolean).join(" · "); }

  /** @override @param {{arguments: string, result: string}} call */
  historic(call) { return staleCall(call, "shell_read rend le fichier actuel"); }
}

class ShellGrep extends PageTool {
  constructor() {
    super({
      name: TOOL.SHELL_GREP,
      description: "Cherche dans les fichiers texte de la VM Linux, sans les relire en entier : rend "
        + "les lignes qui contiennent `pattern` (texte, ou expression régulière JavaScript avec "
        + "`regex`), numérotées, avec `context` lignes autour, fichier par fichier. `path` : un "
        + "fichier, ou un dossier (récursif, fichiers et dossiers cachés exclus ; défaut /work). Pour "
        + "retrouver un passage avant de le lire (shell_read sur ses lignes) puis de le retoucher (shell_edit).",
      params: {
        pattern: { type: "string", description: "Le texte cherché (ou une expression régulière si `regex`)." },
        path: { type: "string", description: "Un fichier ou un dossier : absolu, ou relatif à /work (défaut : /work)." },
        regex: { type: "boolean", description: "`pattern` est une expression régulière (JavaScript)." },
        ignore_case: { type: "boolean", description: "Sans tenir compte de la casse (défaut : oui)." },
        context: { type: "integer", description: "Lignes de contexte avant et après (défaut 2, 10 au plus)." },
      },
      required: ["pattern"],
    });
  }

  /** @override @param {any} args @param {import("../contract.js").Ctx} ctx */
  run(args, ctx) {
    return wrap(async () => {
      const p = text.pattern(args);
      if ("error" in p) return failure(p.error);
      const where = linux.absolute(args.path || ".");
      const files = await linux.readTexts(ctx.conv, where);
      const ctxLines = text.contextOf(args);
      const out = [];
      let hits = 0, left = text.MAX_HITS, matched = 0;
      for (const f of files) {
        const g = text.grep(f.text, p.re, ctxLines, Math.max(1, left));
        if (!g.hits) continue;
        matched++;
        hits += g.hits;
        if (left <= 0) continue;
        left -= g.shown;
        out.push(`── ${f.name} (${g.total} lignes) : ${text.found(g)}\n${g.lines.join("\n")}`);
      }
      if (!hits) return `Aucune ligne ne contient « ${args.pattern} » sous « ${where} » (${files.length} fichier${files.length > 1 ? "s" : ""} texte lu${files.length > 1 ? "s" : ""}).`;
      return `« ${args.pattern} » sous « ${where} » : ${hits} ligne${hits > 1 ? "s" : ""} dans ${matched} fichier${matched > 1 ? "s" : ""}`
        + (left <= 0 ? ` (les ${text.MAX_HITS} premières montrées)` : "")
        + ` (le numéro et la marque ne font pas partie du texte) :\n${out.join("\n")}`;
    });
  }

  /** @override @param {any} a */
  summary(a) { return [a.path, a.pattern].filter(Boolean).join(" · "); }

  /** @override @param {{arguments: string, result: string}} call */
  historic(call) { return staleCall(call, "relancer shell_grep sur les fichiers actuels"); }
}

class ShellEdit extends PageTool {
  constructor() {
    super({
      name: TOOL.SHELL_EDIT,
      description: "Retouche un fichier texte de la VM Linux SANS le réécrire : chaque `old_string`, un "
        + "passage exact et unique du fichier (espaces et retours à la ligne compris, assez long pour "
        + "être unique), est remplacé par `new_string` — ou, avec `regex`, une expression régulière (un "
        + "changement répétitif : `replace_all`). Toutes les retouches s'appliquent, ou aucune. Pour "
        + "trouver le passage : shell_grep, puis shell_read sur ses lignes. Plutôt que sed dans run_shell.",
      params: {
        path: PATH,
        edits: { type: "array", minItems: 1, items: {
          type: "object", required: ["old_string", "new_string"], properties: {
            old_string: { type: "string", description: "Le passage exact à remplacer." },
            new_string: { type: "string", description: "Ce qui le remplace (vide : le supprimer)." },
            replace_all: { type: "boolean", description: "Remplacer toutes ses occurrences (sinon il doit être unique)." },
            regex: { type: "boolean", description: "`old_string` est une expression régulière (JavaScript, multiligne) ; `new_string` peut citer ses groupes ($1). Pour un changement répétitif ; sinon, le texte exact." },
            ignore_case: { type: "boolean", description: "Avec `regex` : sans tenir compte de la casse." },
          } } },
      },
      required: ["path", "edits"],
    });
  }

  /** @override @param {any} args @param {import("../contract.js").Ctx} ctx */
  run(args, ctx) {
    return wrap(async () => {
      const f = linux.absolute(args.path);
      const edits = Array.isArray(args.edits) ? args.edits : [];
      const done = text.applyEdits(await linux.readFile(ctx.conv, f), edits,
        { grep: "shell_grep", read: "shell_read", at: `dans « ${f} »` });
      if ("error" in done) return done.error;
      await linux.writeFile(ctx.conv, f, done.content);
      const saved = await linux.save().catch(() => "");
      return `Fichier « ${f} » retouché ${text.editCount(edits.length, done.passages)}.` + (saved ? ` ${saved}.` : "");
    });
  }

  /** @override @param {any} a */
  summary(a) {
    const n = Array.isArray(a.edits) ? a.edits.length : 0;
    return [a.path, n ? n + " retouche" + (n > 1 ? "s" : "") : ""].filter(Boolean).join(" · ");
  }
}

class ShellWrite extends PageTool {
  constructor() {
    super({
      name: TOOL.SHELL_WRITE,
      description: "Écrit un fichier texte entier dans la VM Linux : le crée (dossiers compris), ou le "
        + "remplace. Pour un nouveau fichier, ou une réécriture complète ; pour une modification, "
        + "shell_edit. Sans les guillemets et l'échappement d'un heredoc dans run_shell.",
      params: {
        path: PATH,
        content: { type: "string", description: "Le contenu complet du fichier." },
      },
      required: ["path", "content"],
    });
  }

  /** @override @param {any} args @param {import("../contract.js").Ctx} ctx */
  run(args, ctx) {
    return wrap(async () => {
      const w = await linux.writeFile(ctx.conv, linux.absolute(args.path), String(args.content ?? ""));
      const saved = await linux.save().catch(() => "");
      return `Écrit : « ${w.path} » (${w.lines} ligne${w.lines > 1 ? "s" : ""}, ${w.bytes} octets).` + (saved ? ` ${saved}.` : "");
    });
  }

  /** @override @param {any} a */
  summary(a) { return a.path || ""; }

  // Une écriture passée : son contenu n'est plus forcément celui du fichier.
  /** @override @param {{arguments: string, result: string}} call */
  historic(call) {
    let a;
    try { a = JSON.parse(call.arguments); } catch { return call; }
    if (typeof a.content !== "string") return call;
    a.content = `[contenu écrit par un appel passé (${a.content.length} caractères), non rejoué : shell_read rend le fichier actuel]`;
    return { arguments: JSON.stringify(a), result: call.result };
  }
}

export const FAMILIES = [defineFamily({
  key: FAMILY.LINUX, label: "Linux",
  hint: "Une VM Linux en WASM (Alpine : bash, python3, git, outils GNU…), sans réseau, partagée avec le panneau Terminal",
  available: () => typeof WebAssembly === "object" && typeof Worker === "function",
  guidance: (ctx) => "run_shell exécute du bash dans une VM Linux du navigateur. Ne "
    + "s'en servir que si la demande exige d'exécuter quelque chose : lancer ou tester un script "
    + "shell ou Python (bibliothèque standard seulement), traiter des fichiers déjà dans la VM. "
    + "Pas pour rédiger une réponse, des notes, un tableau ou un document : répondre directement, "
    + "ou passer par un artifact. /work est gardé avec la "
    + "conversation ; l'utilisateur voit la même machine dans le panneau Terminal. Pour modifier "
    + "un fichier de la VM : shell_grep (où ?), shell_read sur ses lignes, shell_edit — plutôt que "
    + "sed ou un heredoc dans run_shell ; shell_write pour un fichier neuf."
    + (ctx && ctx.conv && ctx.conv.linuxNetwork
      ? linux.vm.relay
        ? " L'utilisateur a OUVERT le réseau de la VM : un vrai accès à Internet (https, apk add, "
          + "pip install, git clone, ssh), sauf vers les réseaux privés."
        : " L'utilisateur a OUVERT le réseau de la VM : des requêtes web seulement, en écrivant les "
          + "adresses en http:// (le navigateur les fait en https), vers les sites qui permettent CORS "
          + "(API publiques, pypi.org/pypi/<paquet>/json, raw.githubusercontent.com, api.github.com…) ; "
          + "pas de https:// direct, ni ssh, ni apk, ni pip install."
      : " La VM n'a pas de réseau (l'utilisateur peut l'ouvrir dans le panneau Terminal).")
    + (ctx && ctx.conv && linux.visible(ctx.conv)
      ? " Le panneau Terminal est OUVERT : ce que run_shell exécute y est tapé, sous les yeux de "
        + "l'utilisateur. Pour un programme qui ne s'arrête pas seul (serveur, boucle), donner un "
        + "`timeout` court : il continue dans le terminal."
      : ""),
  tools: [new RunShell(), new ShellRead(), new ShellGrep(), new ShellEdit(), new ShellWrite()],
})];
