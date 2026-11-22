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
import { paths as pathsOf, inputs, deliver } from "./files.js";
import * as linux from "./linuxvm.js";
import * as text from "../text.js";

const HEAD = 6_000, TAIL = 14_000;   // caractères de sortie gardés : le début, et surtout la fin

// Une longue sortie : son début et sa fin (une erreur est souvent à la fin).
function cut(/** @type {string} */ s) {
  if (s.length <= HEAD + TAIL) return s;
  return s.slice(0, HEAD) + `\n… (${s.length - HEAD - TAIL} caractères coupés) …\n` + s.slice(-TAIL);
}

class RunShell extends PageTool {
  constructor() {
    super({ name: TOOL.RUN_SHELL });
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
    const { timeout: byDefault, max_timeout: most } = this.settings;
    const timeout = Math.min(most, Math.max(1, Number(args.timeout) || byDefault));
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
const wrap = async (/** @type {() => Promise<string>} */ f) => {
  try { return await f(); } catch (e) { return failure(`VM Linux : ${e.message}.`); }
};

class ShellRead extends PageTool {
  constructor() {
    super({ name: TOOL.SHELL_READ });
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
        args = { start_line: 1, end_line: this.settings.max_slice };
      }
      const t = text.slice(content, args.start_line, args.end_line, this.settings);
      if ("error" in t) return failure(`le fichier ${t.error}`);
      return `${head} — lignes ${t.start} à ${t.end}${t.end < t.total ? ` (suite : start_line ${t.end + 1})` : ""} `
        + `(le numéro et « → » ne font pas partie du texte, pour old_string) :\n${t.body}`;
    });
  }

  /** @override @param {any} a */
  summary(a) { return [a.path, a.start_line ? `lignes ${a.start_line}–${a.end_line || "…"}` : ""].filter(Boolean).join(" · "); }
}

class ShellGrep extends PageTool {
  constructor() {
    super({ name: TOOL.SHELL_GREP });
  }

  /** @override @param {any} args @param {import("../contract.js").Ctx} ctx */
  run(args, ctx) {
    return wrap(async () => {
      const p = text.pattern(args);
      if ("error" in p) return failure(p.error);
      const where = linux.absolute(args.path || ".");
      const files = await linux.readTexts(ctx.conv, where);
      const ctxLines = text.contextOf(args, this.settings);
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
}

class ShellEdit extends PageTool {
  constructor() {
    super({ name: TOOL.SHELL_EDIT });
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
    super({ name: TOOL.SHELL_WRITE });
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
}

export const FAMILIES = [defineFamily({
  key: FAMILY.LINUX,
  available: () => typeof WebAssembly === "object" && typeof Worker === "function",
  // Le réseau, que l'utilisateur ouvre ou ferme dans le panneau Terminal :
  // l'état, joint au tour (le prompt système reste figé).
  state: (ctx) => (ctx && ctx.conv && ctx.conv.linuxNetwork
    ? linux.vm.relay
      ? "Réseau de la VM Linux OUVERT par l'utilisateur : un vrai accès à Internet (https, apk add, "
        + "pip install, git clone, ssh), sauf vers les réseaux privés."
      : "Réseau de la VM Linux OUVERT par l'utilisateur : des requêtes web seulement, en écrivant les "
        + "adresses en http:// (le navigateur les fait en https), vers les sites qui permettent CORS "
        + "(API publiques, pypi.org/pypi/<paquet>/json, raw.githubusercontent.com, api.github.com…) ; "
        + "pas de https:// direct, ni ssh, ni apk, ni pip install."
    : "La VM Linux n'a pas de réseau (l'utilisateur peut l'ouvrir dans le panneau Terminal).")
    + (ctx && ctx.conv && linux.visible(ctx.conv)
      ? " Le panneau Terminal est OUVERT : ce que run_shell exécute y est tapé, sous les yeux de "
        + "l'utilisateur. Pour un programme qui ne s'arrête pas seul (serveur, boucle), donner un "
        + "`timeout` court : il continue dans le terminal."
      : ""),
  tools: [new RunShell(), new ShellRead(), new ShellGrep(), new ShellEdit(), new ShellWrite()],
})];
