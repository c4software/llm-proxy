// Le runtime PHP du bac (chat/device/sandbox/php.js) : PHP 8.3 en WASM
// (build asyncify, tous navigateurs), sans serveur ni réseau. Le binaire
// est passé par l'appelant : le bac n'a pas le droit de le télécharger.
import { PHP, PHPResponse, loadPHPRuntime, setPhpIniEntries } from "@php-wasm/universal";
import * as loader from "./node_modules/@php-wasm/web-8-3/asyncify/php_8_3.js";

export async function runPHP(code, wasmBinary) {
  const php = new PHP(await loadPHPRuntime(loader, { wasmBinary }));
  try {
    await setPhpIniEntries(php, { html_errors: "0", log_errors: "0", allow_url_fopen: "Off" });
    const r = await PHPResponse.fromStreamedResponse(await php.runStream({ code }));
    return { stdout: r.text, stderr: r.errors, exitCode: r.exitCode };
  } finally {
    php.exit();
  }
}
