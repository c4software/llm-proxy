// Appelé UNE fois, à la construction de l'image (voir Dockerfile) : télécharge
// omp et l'extension du provider de llmsetup, vérifie le sha256 de chacun, et
// s'arrête au premier écart en affichant celui du fichier reçu. Node seul
// (fetch, crypto) : node:22-slim n'a ni curl ni wget.
//
//   node install.mjs <OMP_VERSION> <LLMSETUP_REV> <sha256 llm-proxy.ts>
import { createHash } from "node:crypto";
import { chmodSync, mkdirSync, writeFileSync } from "node:fs";

const [version, rev, sumProvider] = process.argv.slice(2);

async function get(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`HTTP ${r.status} pour ${url}`);
  return Buffer.from(await r.arrayBuffer());
}

// Le fichier de `url`, s'il a bien le sha256 `sum`.
async function verified(url, sum) {
  const body = await get(url);
  const got = createHash("sha256").update(body).digest("hex");
  if (got !== sum) throw new Error(`sha256 reçu ${got}, attendu ${sum} (${url})`);
  return body;
}

// ── omp ──────────────────────────────────────────────────────────────────
// Le binaire autonome des releases GitHub (can1357/oh-my-pi), celui que pose
// le script d'installation officiel (https://omp.sh/install, install_binary) :
// `omp-linux-x64` ou `omp-linux-arm64`, la variante glibc — node:22-slim est
// une Debian (la variante `omp-linux-musl-*` est pour Alpine). Son sha256 est
// lu dans le SHA256SUMS.txt de la MÊME release : il garantit le
// téléchargement, pas la release (une version donnée est en revanche toujours
// le même binaire). `latest` suit la redirection releases/latest/download de
// GitHub.
const arch = { x64: "x64", arm64: "arm64" }[process.arch];
if (!arch) throw new Error(`pas de binaire omp pour l'architecture ${process.arch}`);
const asset = `omp-linux-${arch}`;
const release = "https://github.com/can1357/oh-my-pi/releases/"
  + (version === "latest" ? "latest/download" : `download/v${version}`);
const sums = (await get(`${release}/SHA256SUMS.txt`)).toString();
const line = sums.split("\n").map((l) => l.trim().split(/\s+/)).find((l) => l[1] === asset);
if (!line) throw new Error(`${asset} absent de ${release}/SHA256SUMS.txt`);
writeFileSync("/usr/local/bin/omp", await verified(`${release}/${asset}`, line[0]));
chmodSync("/usr/local/bin/omp", 0o755);
console.log(`omp : ${release}/${asset} (sha256 ${line[0]})`);

// ── l'extension du provider ─────────────────────────────────────────────────
// Dépôt public c4software/llmsetup, à une révision ÉPINGLÉE — le commit
// entier dans l'URL raw de GitHub, donc un contenu qui ne peut pas changer.
const raw = `https://raw.githubusercontent.com/c4software/llmsetup/${rev}/tools`;
mkdirSync("/omp/extensions", { recursive: true });

// llm-proxy.ts, le provider. À la révision épinglée, la version VERSIONNÉE a
// l'adresse du proxy et sa clé en dur (« Valeurs en dur pour le test —
// repasser sur process.env avant de committer », dit son propre commentaire).
// Le fichier est vérifié tel qu'il est versionné, PUIS ces deux lignes — et
// elles seules — sont remplacées par celles de la copie installée sur le
// poste (~/.omp/agent/extensions/llm-proxy.ts, non versionnée), qui lit
// LLM_PROXY_URL et LLM_PROXY_API_KEY : le reste du provider (découverte par
// /v1/models, réglages de raisonnement, nom « albert ») est celui du dépôt.
// Une ligne introuvable arrête la construction : le fichier épinglé a changé,
// et s'il lit désormais l'environnement, cette substitution est à retirer.
let provider = (await verified(`${raw}/llm-proxy.ts`, sumProvider)).toString();
for (const [from, to] of [
  ['const ENDPOINT = "http://llmproxy";', 'const ENDPOINT = process.env.LLM_PROXY_URL ?? "http://llmproxy";'],
  ['const API_KEY = "unused";', 'const API_KEY = process.env.LLM_PROXY_API_KEY ?? "unused";'],
]) {
  if (provider.split(from).length !== 2) throw new Error(`llm-proxy.ts : ligne attendue une fois, « ${from} »`);
  provider = provider.replace(from, to);
}
writeFileSync("/omp/extensions/llm-proxy.ts", provider);
console.log(`extension : llmsetup ${rev}, tools/llm-proxy.ts (adresse et clé lues dans l'environnement)`);
