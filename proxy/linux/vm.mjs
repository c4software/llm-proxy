// Le PILOTE de la VM Linux (v86) : le même module au build (state.mjs,
// sous Node, qui démarre la VM et sauve son état) et dans la page (le
// Worker du cadre isolé, chat/linux.js), servi sous vendor/linux/vm.js.
//
// Tout est passé en ArrayBuffer : v86 ne reçoit jamais d'URL, il ne
// télécharge rien. La carte réseau (virtio) n'est reliée à rien, sauf
// `network` : son trafic HTTP passe alors par fetch() (le backend « fetch »
// de v86, sans relais), et vm.network() la configure dans l'invité.
//
//   createVM(V86, files, {memoryMB, network})
//                                      files : {wasm, bios, vgaBios} et
//                                      {state} (reprise) ou {bzimage, initrd}
//   vm.ready()                         la VM répond (après démarrage ou reprise)
//   vm.exec(script, {timeout, inputs, out})
//                                      → {log, exitCode, outputs}
//   vm.readFiles(chemin, {max})        un fichier, ou les fichiers d'un dossier
//                                      (récursif, cachés exclus) → [{name, bytes}]
//   vm.onConsole(fn), vm.type(octets)  le terminal de l'utilisateur (ttyS0)
//   vm.typeExec(ligne, {timeout})      une commande TAPÉE dans ce terminal,
//                                      sous les yeux de l'utilisateur
//                                      → {log, exitCode} | {busy} | {running, log}
//   vm.destroy()
//
// Le canal du modèle est ttyS1 (l'agent vm-agent, brut, sans écho) ; les
// scripts et ce qu'ils produisent passent par le dossier 9p de la VM
// (/mnt/host/.vm, la racine /.vm du système de fichiers 9p de v86), bien
// plus rapide que le port série (≈ 8 Ko/s).

// Le noyau : console sur ttyS0, silencieux ; `init_on_free` remet à zéro
// la mémoire libérée, ce qui réduit l'état sauvé une fois compressé.
export const CMDLINE = "console=ttyS0 quiet loglevel=3 tsc=reliable mitigations=off random.trust_cpu=on "
  + "init_on_free=on rdinit=/init";
export const MEMORY_MB = 256;        // 192 au moins : l'initramfs tient en mémoire

// Les options de v86. Une reprise d'état exige les MÊMES périphériques
// que la VM sauvée (uart1, filesystem, net_device.type) : ne pas les
// changer sans reconstruire l'état. Le backend réseau, lui, n'est pas dans
// l'état : relié ou non, c'est la même carte.
export function createVM(V86, files, { memoryMB = MEMORY_MB, network = false } = {}) {
  const options = {
    wasm_fn: async (env) => (await WebAssembly.instantiate(files.wasm, env)).instance.exports,
    bios: { buffer: files.bios },
    vga_bios: { buffer: files.vgaBios },
    memory_size: memoryMB * 1024 * 1024,
    vga_memory_size: 2 * 1024 * 1024,
    uart1: true,                     // ttyS1 : le canal du modèle
    filesystem: {},                  // 9p en mémoire, sans baseurl : rien n'est lu au réseau
    net_device: { type: "virtio", ...(network ? { relay_url: "fetch" } : {}) },
    disable_keyboard: true, disable_mouse: true, disable_speaker: true,
    autostart: true,
  };
  if (files.state) options.initial_state = { buffer: files.state };   // zstd, décompressé par v86
  else Object.assign(options, { bzimage: { buffer: files.bzimage }, initrd: { buffer: files.initrd }, cmdline: CMDLINE });
  return new VM(new V86(options), !!files.state);
}

const nonce = () => Array.from(crypto.getRandomValues(new Uint8Array(8)), (b) => b.toString(36)).join("").slice(0, 12);
// Un nom pour bash, entre apostrophes.
export const quote = (s) => "'" + String(s).replace(/'/g, "'\\''") + "'";

// Le port série du modèle (ttyS1) : ce qu'il écrit, et l'attente d'un repère.
class Channel {
  constructor(emulator) {
    this.emulator = emulator;
    this.buf = "";
    this.waiters = [];
    emulator.add_listener("serial1-output-byte", (b) => {
      this.buf += String.fromCharCode(b);
      if (this.buf.length > 65536) this.buf = this.buf.slice(-4096);
      this.check();
    });
  }
  check() {
    this.waiters = this.waiters.filter((w) => {
      const m = w.re.exec(this.buf);
      if (!m) return true;
      this.buf = this.buf.slice(m.index + m[0].length);
      clearTimeout(w.timer);
      w.resolve(m);
      return false;
    });
  }
  // Envoie une ligne et attend le repère `re` (au plus `ms`).
  ask(line, re, ms) {
    const p = new Promise((resolve, reject) => {
      const w = { re, resolve };
      w.timer = setTimeout(() => {
        this.waiters = this.waiters.filter((x) => x !== w);
        reject(new Error(`la VM ne répond pas (${re.source})`));
      }, ms);
      this.waiters.push(w);
      this.check();
    });
    if (line) this.emulator.serial_send_bytes(1, Uint8Array.from(line, (c) => c.charCodeAt(0)));
    return p;
  }
}

// Le terminal de l'utilisateur (ttyS0), suivi : son bash émet des
// séquences OSC 633 que le terminal n'affiche pas (guest/bashrc, comme
// l'intégration shell de VS Code) — « C » quand une commande part (PS0),
// « D;<code> » quand l'invite revient (PROMPT_COMMAND). Libre = à
// l'invite, et rien de tapé depuis par l'utilisateur.
const OSC = /\x1b\]633;([CD])(?:;(\d+))?\x07/g;
class Console {
  constructor(emulator) {
    this.emulator = emulator;
    this.text = "";                  // les octets reçus (latin1), depuis le dernier repère
    this.busy = false;
    this.dirty = false;              // l'utilisateur a tapé depuis l'invite
    this.capture = null;             // une commande tapée par typeExec, attendue
    emulator.add_listener("serial0-output-byte", (b) => {
      this.text += String.fromCharCode(b);
      if (b === 7) this.scan();
      else if (!this.capture && this.text.length > 4096) this.text = this.text.slice(-64);
    });
  }
  scan() {
    OSC.lastIndex = 0;
    let m, end = 0;
    while ((m = OSC.exec(this.text))) {
      const c = this.capture;
      if (m[1] === "C") {
        this.busy = true;
        if (c && !c.started) { c.started = true; c.from = m.index + m[0].length; }
      } else {
        this.busy = false;
        this.dirty = false;
        if (c && c.started) {
          this.capture = null;
          c.resolve({ text: this.text.slice(c.from, m.index), exitCode: Number(m[2] || 0) });
        }
      }
      end = m.index + m[0].length;
    }
    const c = this.capture;
    const keep = c && c.started ? c.from : end;
    if (keep) { this.text = this.text.slice(keep); if (c && c.started) c.from = 0; }
  }
  idle() { return !this.busy && !this.dirty && !this.capture; }
}

// Ce qu'affiche un terminal → du texte : séquences d'échappement retirées,
// retours chariot normalisés.
function plain(latin1) {
  const bytes = Uint8Array.from(latin1, (c) => c.charCodeAt(0));
  return new TextDecoder().decode(bytes)
    .replace(/\x1b\][^\x07\x1b]*(\x07|\x1b\\)/g, "")
    .replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "").replace(/\x1b[@-Z\\-_]/g, "")
    .replace(/\r+\n/g, "\n").replace(/^\n/, "").replace(/[^\n]*\r(?!\n)/g, "");
}

class VM {
  constructor(emulator, restored) {
    this.emulator = emulator;
    this.restored = restored;
    this.channel = new Channel(emulator);
    this.console = new Console(emulator);
    this.queue = Promise.resolve();
  }

  // Démarrée : l'agent annonce qu'il est prêt. Reprise : il répond déjà,
  // mais l'horloge est celle du build et l'aléa celui de toutes les copies
  // de l'état — `init` les remet.
  async ready(ms = 120_000) {
    if (!this.restored) { await this.channel.ask("", /__AGENT_READY__\r?\n/, ms); return; }
    await new Promise((r) => this.emulator.add_listener("emulator-loaded", r));
    await this.channel.ask("ping\n", /__PONG__\r?\n/, ms);
    const hex = Array.from(crypto.getRandomValues(new Uint8Array(64)), (b) => b.toString(16).padStart(2, "0")).join("");
    await this.channel.ask(`init ${Math.floor(Date.now() / 1000)} ${hex}\n`, /__INIT_OK__\r?\n/, 20_000);
  }

  // Le réseau dans l'invité (VM créée avec `network`) : la carte, et les
  // adresses fixes de la passerelle `fetch` de v86 (192.168.86.1, qui
  // répond aussi au DNS) — sans DHCP, qui voudrait le module af_packet.
  // Rend vrai si la route est posée.
  async network() {
    const r = await this.exec("modprobe virtio_net 2>/dev/null; ip link set eth0 up "
      + "&& ip addr add 192.168.86.100/24 dev eth0 2>/dev/null; ip route replace default via 192.168.86.1 "
      + "&& echo 'nameserver 192.168.86.1' > /etc/resolv.conf", { timeout: 30 });
    return r.exitCode === 0;
  }

  // Un fichier du dossier 9p (/.vm/<nom>), ou null s'il n'existe pas
  // (read_file refuse aussi un fichier vide : il vaut alors des octets vides).
  async read(name) {
    const fs = this.emulator.fs9p;
    if (fs.SearchPath(`/.vm/${name}`).id === -1) return null;
    try { return await this.emulator.read_file(`/.vm/${name}`); } catch { return new Uint8Array(0); }
  }

  /**
   * Un script bash, dans /work, sans entrée, tué après `timeout` secondes
   * (code 137). `inputs` ([{name, bytes}]) : posés sous /work avant, à leur
   * chemin. `out` : les fichiers qu'il a écrits sous /out pendant l'appel
   * sont rendus ({name, bytes}) — ils y RESTENT : /out est /work/out, gardé
   * avec /work. Un appel à la fois (les suivants attendent).
   *
   * `mark` : pour un script tapé dans le terminal (typeExec), l'appel est
   * en deux temps — un exec qui pose le repère `mark` (sans `out`), puis,
   * après la commande, un exec qui rend /out depuis ce repère (`out`).
   */
  exec(script, { timeout = 60, inputs = [], out = false, mark = "" } = {}) {
    const run = this.queue.then(() => this.run(script, timeout, inputs, out, mark));
    this.queue = run.catch(() => {});
    return run;
  }

  async run(script, timeout, inputs, out, mark) {
    const id = nonce();
    const enc = new TextEncoder();
    const since = `/tmp/.vm-mark-${(mark || id).replace(/[^a-z0-9]/g, "")}`;
    // Le relevé de /out : chaque fichier, sa date (à la nanoseconde) et sa
    // taille — `find -newer` de busybox compare à la seconde.
    const listing = "(cd /work/out && find . -type f -exec stat -c '%y|%s|%n' {} + 2>/dev/null)";
    // /out est /work/out : ce qui y est produit reste dans la VM, gardé.
    const lines = ["mkdir -p /work/out",
      "[ -L /out ] || { cp -a /out/. /work/out/ 2>/dev/null; rm -rf /out; ln -s /work/out /out; }",
      // (« # » : une ligne témoin — un relevé vide ferait tout filtrer à `grep -f`.)
      ...(!mark || !out ? [`{ echo '#'; ${listing}; } > ${since}`] : []), "cd /work"];
    for (const [k, f] of inputs.entries()) {
      await this.emulator.create_file(`/.vm/${id}.in${k}`, f.bytes);
      const dest = quote(`/work/${f.name}`);
      lines.push(`mkdir -p "$(dirname ${dest})" && mv /mnt/host/.vm/${id}.in${k} ${dest}`);
    }
    await this.emulator.create_file(`/.vm/${id}.user`, enc.encode(script));
    // Les erreurs du script vont au journal ; l'avis « Killed » de ce bash-ci, nulle part.
    lines.push(`{ timeout -s KILL ${Math.max(1, Math.round(timeout))} bash /mnt/host/.vm/${id}.user </dev/null 2>&1; } 2>/dev/null`,
               "rc=$?");
    if (out) {
      // Les fichiers de /out neufs ou changés depuis le relevé : rendus, et gardés.
      lines.push(`touch ${since}; ${listing} | grep -vxF -f ${since} | sed 's/^[^|]*|[^|]*|//' > ${since}.new; `
        + `[ -s ${since}.new ] && (cd /work/out && tar -cf /mnt/host/.vm/${id}.tar -T ${since}.new); `
        + `rm -f ${since} ${since}.new`);
    }
    lines.push("exit $rc");
    await this.emulator.create_file(`/.vm/${id}.sh`, enc.encode(lines.join("\n") + "\n"));
    let m;
    try {
      // L'agent laisse une marge : les fichiers fournis et /out passent hors délai.
      m = await this.channel.ask(`${id} ${Math.round(timeout) + 120}\n`, new RegExp(`__END_${id}_(\\d+)__\\r?\\n`),
                                 (timeout + 180) * 1000);
      const log = (await this.read(`${id}.log`)) || new Uint8Array(0);
      const tar = out ? await this.read(`${id}.tar`) : null;
      return { log, exitCode: Number(m[1]), outputs: tar ? untar(tar) : [] };
    } finally {
      if (m) await this.channel.ask(`done ${id}\n`, new RegExp(`__DONE_${id}__\\r?\\n`), 20_000).catch(() => {});
    }
  }

  /**
   * Un fichier (chemin absolu), ou les fichiers d'un dossier — récursif,
   * fichiers et dossiers cachés exclus, `maxFiles` au plus, de moins de
   * `maxSize` octets — en une archive tar passée par le 9p ; noms absolus.
   * Lève une Error si le chemin n'existe pas.
   */
  async readFiles(path, { maxFiles = 500, maxSize = 1_000_000 } = {}) {
    const q = quote(path);
    const r = await this.exec(`[ -e ${q} ] || { echo "« ${path.replace(/"/g, "")} » n'existe pas"; exit 2; }; `
      + `if [ -f ${q} ]; then tar -cf - ${q}; else find ${q} -type f -size -${Math.ceil(maxSize / 1024)}k `
      + `! -path '*/.*' | sort | head -${maxFiles} | tar -cf - -T -; fi 2>/dev/null`, { timeout: 120 });
    if (r.exitCode === 2) throw new Error(new TextDecoder().decode(r.log).trim());
    return untar(r.log).map((f) => ({ name: "/" + f.name.replace(/^\/+/, ""), bytes: f.bytes }));
  }

  // Le terminal de l'utilisateur : ttyS0, ce qu'il affiche et ce qu'il y tape.
  onConsole(fn) { this.emulator.add_listener("serial0-output-byte", fn); }
  type(bytes) {
    // Une ligne entamée (pas un simple Entrée) : le terminal n'est plus libre.
    if (bytes.some((b) => b !== 13 && b !== 10)) this.console.dirty = true;
    this.emulator.serial_send_bytes(0, bytes);
  }

  // Réafficher l'invite (Ctrl-L), sans compter comme une frappe.
  redraw() { this.emulator.serial_send_bytes(0, new Uint8Array([12])); }

  /**
   * Une commande (une ligne, ou un bloc `( … )` de plusieurs lignes),
   * TAPÉE dans le terminal de l'utilisateur, qui la voit partir et
   * s'exécuter. Rend {busy: true} si le terminal n'est
   * pas libre (une commande en cours, une ligne entamée) : rien n'est
   * tapé. Après `timeout` secondes, la commande continue dans le terminal
   * (un serveur, un programme interactif) : {running: true, log}.
   */
  async typeExec(line, { timeout = 60 } = {}) {
    const con = this.console;
    if (!con.idle()) return { busy: true };
    const done = new Promise((resolve) => { con.capture = { started: false, from: 0, resolve }; });
    // Tapée vite, mais visiblement : par petits morceaux.
    const bytes = new TextEncoder().encode(line);
    const step = Math.max(2, Math.ceil(bytes.length / 60));
    for (let i = 0; i < bytes.length; i += step) {
      this.emulator.serial_send_bytes(0, bytes.subarray(i, i + step));
      await new Promise((r) => setTimeout(r, 12));
    }
    this.emulator.serial_send_bytes(0, new Uint8Array([13]));
    let timer;
    const late = new Promise((r) => { timer = setTimeout(() => r(null), timeout * 1000); });
    const got = await Promise.race([done, late]);
    clearTimeout(timer);
    const enc = new TextEncoder();
    if (got) return { log: enc.encode(plain(got.text)), exitCode: got.exitCode };
    // Toujours en cours : laissée à l'utilisateur ; ce qu'elle a affiché jusque-là.
    const c = con.capture;
    con.capture = null;
    return { running: true, log: enc.encode(plain(c && c.started ? con.text.slice(c.from) : "")) };
  }

  destroy() { return this.emulator.destroy(); }
}

// Une archive tar (ustar, celle de busybox) → [{name, bytes}], fichiers seuls.
export function untar(buf) {
  const out = [];
  const dec = new TextDecoder();
  const str = (o, n) => dec.decode(buf.subarray(o, o + n)).replace(/\0.*$/s, "");
  for (let o = 0; o + 512 <= buf.length;) {
    const name = str(o, 100);
    if (!name) break;
    const size = parseInt(str(o + 124, 12).trim() || "0", 8);
    const type = String.fromCharCode(buf[o + 156] || 48);
    const prefix = str(o + 345, 155);
    const path = (prefix ? prefix + "/" : "") + name;
    if (type === "0") out.push({ name: path.replace(/^\.\//, ""), bytes: buf.slice(o + 512, o + 512 + size) });
    o += 512 + Math.ceil(size / 512) * 512;
  }
  return out;
}
