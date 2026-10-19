// @ts-check
/*
  Les fichiers de vendor/ (les runtimes des bacs, la VM Linux), demandés
  par la page : hors du préfixe versionné (ui_static.py) — l'empreinte de
  la page change à chaque mise à jour, un runtime presque jamais.

  Chacun est demandé avec ?v=<son empreinte>, lue dans le versions.json
  de son dossier (écrit au build : proxy/wasm/build.mjs, proxy/linux/
  state.mjs) : un cache devant la page (un proxy qui garde les .js une
  demi-heure, quoi que demande le navigateur) ne peut pas servir un
  runtime périmé ; inchangé, il garde son adresse. Toujours revalidé
  auprès du serveur (304 s'il n'a pas changé).

    vendorFetch(chemin, "text" | "arrayBuffer")
    vendorUrl(chemin)        son adresse, pour un import()
*/
const VENDOR = new URL("/ui/static/vendor/", location.href);

/** dossier → promesse de {fichier: empreinte} ; un échec est oublié. @type {Map<string, Promise<Record<string, string>>>} */
const versions = new Map();

/** @param {string} dir */
function versionsOf(dir) {
  if (!versions.has(dir)) {
    const p = fetch(new URL(dir + "versions.json", VENDOR), { cache: "no-cache" })
      .then((r) => (r.ok ? r.json() : {}))
      .catch(() => { versions.delete(dir); return {}; });
    versions.set(dir, p);
  }
  return versions.get(dir);
}

/** L'adresse d'un fichier de vendor/, à son empreinte. @param {string} path */
export async function vendorUrl(path) {
  const dir = path.slice(0, path.lastIndexOf("/") + 1);
  const v = (await versionsOf(dir))[path.slice(dir.length)];
  return new URL(path + (v ? `?v=${v}` : ""), VENDOR).href;
}

/** Un fichier de vendor/ (« php.wasm », « linux/state.bin.zst »).
 * @param {string} path @param {"text" | "arrayBuffer"} as @returns {Promise<any>} */
export async function vendorFetch(path, as) {
  const r = await fetch(await vendorUrl(path), { cache: "no-cache" });
  if (!r.ok) throw new Error(`${path} : HTTP ${r.status}`);
  return r[as]();
}
