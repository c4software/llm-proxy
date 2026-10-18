// Le runtime PHP du bac (chat/device/sandbox/php.js) : PHP 8.3 en WASM
// (build asyncify, tous navigateurs), sans serveur ni réseau. Le binaire
// est passé par l'appelant : le bac n'a pas le droit de le télécharger.
//
// `files` ({chemin, octets}) : posés sous /data, le dossier courant du
// script. Ce que le script écrit sous /out est rendu ({chemin, octets}).
import { PHP, PHPResponse, loadPHPRuntime, setPhpIniEntries } from "@php-wasm/universal";
import * as loader from "./node_modules/@php-wasm/web-8-3/asyncify/php_8_3.js";

const DATA = "/data";
const OUT = "/out";

function collect(php, dir, prefix, out) {
  for (const name of php.listFiles(dir)) {
    const path = `${dir}/${name}`;
    if (php.isDir(path)) collect(php, path, `${prefix}${name}/`, out);
    else out.push({ name: prefix + name, bytes: php.readFileAsBuffer(path) });
  }
  return out;
}

export async function runPHP(code, wasmBinary, files = []) {
  const php = new PHP(await loadPHPRuntime(loader, { wasmBinary }));
  try {
    await setPhpIniEntries(php, { html_errors: "0", log_errors: "0", allow_url_fopen: "Off" });
    php.mkdirTree(DATA);
    php.mkdirTree(OUT);
    for (const f of files) {
      const path = `${DATA}/${f.name}`;
      php.mkdirTree(path.slice(0, path.lastIndexOf("/")));
      php.writeFile(path, f.bytes);
    }
    php.chdir(DATA);
    const r = await PHPResponse.fromStreamedResponse(await php.runStream({ code }));
    return { stdout: r.text, stderr: r.errors, exitCode: r.exitCode, outputs: collect(php, OUT, "", []) };
  } finally {
    php.exit();
  }
}
