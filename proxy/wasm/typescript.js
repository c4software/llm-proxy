// Le vérificateur TypeScript du bac (chat/device/sandbox/typescript.js) :
// le compilateur TypeScript 6 (le dernier écrit en JavaScript) et ses
// bibliothèques standard (lib.*.d.ts), sans disque ni réseau. Les
// fichiers sont passés par l'appelant.
import ts from "typescript";
import LIBS from "./ts-libs.json";

const LIB_DIR = "/lib/";
const MAX_DIAGNOSTICS = 100;

// `files` : {chemin: texte} (tout le projet utile) ; `roots` : ceux à
// vérifier ; `tsconfig` : le texte du tsconfig.json, ou "". Rend
// {diagnostics: [{file, line, col, code, category, message}], total, checked}.
export function check({ files, roots, tsconfig }) {
  const options = { strict: true, target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext,
    moduleResolution: ts.ModuleResolutionKind.Bundler, jsx: ts.JsxEmit.Preserve, noEmit: true,
    skipLibCheck: true, allowJs: true, esModuleInterop: true, allowImportingTsExtensions: true };
  const notes = [];
  if (tsconfig) {
    const parsed = ts.parseConfigFileTextToJson("tsconfig.json", tsconfig);
    if (parsed.error) notes.push("tsconfig.json illisible : " + ts.flattenDiagnosticMessageText(parsed.error.messageText, " "));
    else {
      const { options: o, errors } = ts.convertCompilerOptionsFromJson(parsed.config.compilerOptions || {}, "/");
      Object.assign(options, o, { noEmit: true });
      for (const e of errors) notes.push("tsconfig.json : " + ts.flattenDiagnosticMessageText(e.messageText, " "));
    }
  }
  // Les modules des paquets (pas de node_modules ici) : des `any`.
  const shim = "/__modules.d.ts";
  const all = { ...Object.fromEntries(Object.entries(files).map(([k, v]) => ["/" + k, v])), [shim]: 'declare module "*";' };
  const host = {
    getSourceFile(name, version) {
      const text = name.startsWith(LIB_DIR) ? LIBS[name.slice(LIB_DIR.length)] : all[name];
      return text === undefined ? undefined : ts.createSourceFile(name, text, version, true);
    },
    getDefaultLibFileName: (o) => LIB_DIR + ts.getDefaultLibFileName(o),
    getDefaultLibLocation: () => LIB_DIR,
    writeFile() {},
    getCurrentDirectory: () => "/",
    getDirectories: (dir) => {
      const d = dir.endsWith("/") ? dir : dir + "/";
      return [...new Set(Object.keys(all).filter((k) => k.startsWith(d)).map((k) => k.slice(d.length).split("/"))
        .filter((p) => p.length > 1).map((p) => p[0]))];
    },
    fileExists: (name) => name in all || (name.startsWith(LIB_DIR) && name.slice(LIB_DIR.length) in LIBS),
    readFile: (name) => all[name] ?? (name.startsWith(LIB_DIR) ? LIBS[name.slice(LIB_DIR.length)] : undefined),
    directoryExists: (dir) => { const d = dir.endsWith("/") ? dir : dir + "/"; return d === "/" || Object.keys(all).some((k) => k.startsWith(d)); },
    getCanonicalFileName: (n) => n,
    useCaseSensitiveFileNames: () => true,
    getNewLine: () => "\n",
  };
  const rootNames = [...roots.map((r) => "/" + r), shim];
  const program = ts.createProgram({ rootNames, options, host });
  const wanted = new Set(roots.map((r) => "/" + r));
  const diags = [...program.getOptionsDiagnostics(), ...program.getGlobalDiagnostics()];
  for (const sf of program.getSourceFiles()) {
    if (wanted.has(sf.fileName)) diags.push(...program.getSyntacticDiagnostics(sf), ...program.getSemanticDiagnostics(sf));
  }
  const out = diags.map((d) => {
    const pos = d.file && d.start !== undefined ? d.file.getLineAndCharacterOfPosition(d.start) : null;
    return { file: d.file ? d.file.fileName.slice(1) : "", line: pos ? pos.line + 1 : 0, col: pos ? pos.character + 1 : 0,
             code: d.code, category: ts.DiagnosticCategory[d.category].toLowerCase(),
             message: ts.flattenDiagnosticMessageText(d.messageText, "\n") };
  });
  return { diagnostics: out.slice(0, MAX_DIAGNOSTICS), total: out.length, checked: roots.length, notes, version: ts.version };
}
