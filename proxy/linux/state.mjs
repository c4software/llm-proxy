// L'ÉTAT DÉMARRÉ de la VM Linux, et ce que la page en charge (vendor/linux/).
// Lancé par l'étape `linux` de proxy/Dockerfile (proxy/linux/build.sh pour
// une machine de développement), depuis proxy/wasm, où v86 est installé :
//
//   node ../linux/state.mjs <invité> <sortie>
//
// <invité> : bzImage, initrd.cpio et packages.txt, produits par l'étape
// `linux-guest`. La VM y est démarrée une fois sous Node (≈ 4 s), son
// état sauvé (state.bin.zst), puis repris dans une VM neuve qui doit
// répondre : un état cassé fait échouer le build, pas la page.
//
// <sortie> reçoit :
//   v86.js, v86.wasm        l'émulateur (v86, BSD-2-Clause)
//   seabios.bin, vgabios.bin  le BIOS (SeaBIOS, LGPL-3.0), de bios/
//   state.bin.zst           la VM démarrée, compressée (≈ 22 Mo)
//   vm.js                   le pilote (vm.mjs), chargé par le Worker de la page
//   manifest.json           versions, mémoire, paquets, tailles
//   versions.json           l'empreinte de chaque fichier (?v= de la page)
//   packages.txt, LICENSE-* les paquets de l'invité, les licences
import { createHash } from "node:crypto";
import { copyFile, mkdir, readdir, readFile, writeFile, stat } from "node:fs/promises";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import zlib from "node:zlib";
import { createVM, MEMORY_MB } from "./vm.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const [guest, outArg] = process.argv.slice(2);
if (!guest || !outArg) {
  console.error("usage : node ../linux/state.mjs <invité> <sortie>   (depuis proxy/wasm)");
  process.exit(2);
}
const out = resolve(outArg);
await mkdir(out, { recursive: true });

const require = createRequire(join(process.cwd(), "/"));
const v86Dir = dirname(require.resolve("v86/package.json"));
const v86Version = JSON.parse(await readFile(join(v86Dir, "package.json"), "utf8")).version;
const ab = (b) => b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength);
const read = async (p) => ab(await readFile(p));

await copyFile(join(v86Dir, "build/libv86.mjs"), join(out, "v86.js"));
await copyFile(join(v86Dir, "build/v86.wasm"), join(out, "v86.wasm"));
await copyFile(join(v86Dir, "LICENSE"), join(out, "LICENSE-v86"));
for (const f of ["seabios.bin", "vgabios.bin", "LICENSE-seabios"]) await copyFile(join(HERE, "bios", f), join(out, f));
await copyFile(join(HERE, "vm.mjs"), join(out, "vm.js"));
await copyFile(join(guest, "packages.txt"), join(out, "packages.txt"));

const { V86 } = await import(pathToFileURL(join(out, "v86.js")).href);
const base = { wasm: await read(join(out, "v86.wasm")), bios: await read(join(out, "seabios.bin")),
               vgaBios: await read(join(out, "vgabios.bin")) };

// 1. Le démarrage à froid, puis l'état : caches vidés, pour qu'il se
// compresse mieux.
let t = performance.now();
const vm = createVM(V86, { ...base, bzimage: await read(join(guest, "bzImage")), initrd: await read(join(guest, "initrd.cpio")) });
// La console (ttyS0), montrée si la VM ne démarre pas : panique du noyau,
// mémoire trop juste (LINUX_PACKAGES), script de démarrage cassé…
let consoleTail = "";
vm.onConsole((b) => { consoleTail = (consoleTail + String.fromCharCode(b)).slice(-3000); });
await vm.ready().catch((e) => {
  console.error(`La VM ne démarre pas (${e.message}). Fin de sa console :\n${consoleTail}`);
  process.exit(1);
});
const bootMs = Math.round(performance.now() - t);
await vm.exec("sync; echo 3 > /proc/sys/vm/drop_caches");
const raw = new Uint8Array(await vm.emulator.save_state());
await vm.destroy();
const state = zlib.zstdCompressSync(raw, { params: { [zlib.constants.ZSTD_c_compressionLevel]: 19 } });
await writeFile(join(out, "state.bin.zst"), state);

// 2. La vérification : une VM reprise de cet état exécute un script, lit
// un fichier fourni et rend ce qu'elle écrit sous /out.
t = performance.now();
const check = createVM(V86, { ...base, state: ab(state) });
await check.ready();
const restoreMs = Math.round(performance.now() - t);
const r = await check.exec("python3 -c 'print(6 * 7)'; cat in/a.txt; uname -m; echo ok > /out/r.txt",
  { inputs: [{ name: "in/a.txt", bytes: new TextEncoder().encode("fourni\n") }], out: true });
await check.destroy();
const log = new TextDecoder().decode(r.log);
const got = r.outputs.map((f) => `${f.name}=${new TextDecoder().decode(f.bytes).trim()}`).join(",");
if (r.exitCode !== 0 || !/^42\nfourni\ni686\n$/.test(log) || got !== "r.txt=ok") {
  console.error(`VM reprise : vérification échouée (code ${r.exitCode}, sortie ${JSON.stringify(log)}, /out ${got})`);
  process.exit(1);
}

const size = async (f) => (await stat(join(out, f))).size;
const packages = (await readFile(join(guest, "packages.txt"), "utf8")).split("\n").filter((l) => / - /.test(l))
  .map((l) => l.split(" - ")[0]);
const manifest = {
  v86: v86Version, memoryMB: MEMORY_MB, builtAt: new Date().toISOString(),
  sizes: { "state.bin.zst": await size("state.bin.zst"), "v86.wasm": await size("v86.wasm"), "v86.js": await size("v86.js") },
  bootMs, restoreMs, packages,
};
await writeFile(join(out, "manifest.json"), JSON.stringify(manifest, null, 2) + "\n");
const versions = {};
for (const f of (await readdir(out)).sort()) {
  if (f !== "versions.json") versions[f] = createHash("sha256").update(await readFile(join(out, f))).digest("hex").slice(0, 12);
}
await writeFile(join(out, "versions.json"), JSON.stringify(versions, null, 2) + "\n");
console.log(`VM Linux → ${out} : démarrage ${bootMs} ms, reprise ${restoreMs} ms, `
  + `état ${(state.length / 1e6).toFixed(1)} Mo (${(raw.length / 1e6).toFixed(0)} Mo brut), ${packages.length} paquets`);
process.exit(0);
