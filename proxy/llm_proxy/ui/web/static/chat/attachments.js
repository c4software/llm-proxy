/*
  Les PIÈCES JOINTES du chat : une image (data URL, au modèle de la lire),
  un fichier texte (joint au message entre balises <file name="…">), ou
  un audio (data URL, pour un modèle de transcription ; voice/transcribe.js).
  Le reste est refusé. readAttachment(fichier) → la pièce jointe, ou lève
  une Error dont le message se montre à côté de son nom.
*/
const MAX_TEXT_FILE = 256 * 1024;
const MAX_IMAGE = 8 * 1024 * 1024;
// Gardé avec la conversation comme une image : sous [ui].asset_max_bytes.
const MAX_AUDIO = 20 * 1000 * 1000;
const AUDIO_EXT = /\.(wav|mp3|m4a|aac|ogg|oga|opus|flac|webm)$/i;
const TEXT_EXT = /\.(txt|md|markdown|csv|tsv|json|jsonl|ya?ml|toml|ini|cfg|conf|log|xml|html?|css|js|mjs|ts|tsx|jsx|py|rb|go|rs|java|kt|c|h|cc|cpp|hpp|cs|php|sh|bash|zsh|sql|r|lua|swift|vue|svelte|tex|env|dockerfile|makefile)$/i;

const readAs = (file, how) => new Promise((resolve, reject) => {
  const r = new FileReader();
  r.onload = () => resolve(r.result);
  r.onerror = () => reject(r.error);
  r[how](file);
});

// Un fichier qui se lit comme du texte : par son type, ou son extension.
const isText = (file) => file.type.startsWith("text/") || TEXT_EXT.test(file.name)
  || /json|xml|yaml|javascript|toml|csv/.test(file.type);

export async function readAttachment(file) {
  if (file.type.startsWith("image/")) {
    if (file.size > MAX_IMAGE) throw new Error("image de plus de 8 Mo");
    return { kind: "image", name: file.name, type: file.type,
             size: file.size, data: await readAs(file, "readAsDataURL") };
  }
  if (file.type.startsWith("audio/") || AUDIO_EXT.test(file.name)) {
    if (file.size > MAX_AUDIO) throw new Error("audio de plus de 20 Mo");
    return { kind: "audio", name: file.name, type: file.type || "audio/*",
             size: file.size, data: await readAs(file, "readAsDataURL") };
  }
  if (isText(file)) {
    if (file.size > MAX_TEXT_FILE) throw new Error("fichier texte de plus de 256 Ko");
    return { kind: "text", name: file.name, type: file.type || "text/plain",
             size: file.size, data: await readAs(file, "readAsText") };
  }
  throw new Error("seuls les images, les audios et les fichiers texte sont acceptés");
}
