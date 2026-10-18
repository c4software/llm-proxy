/*
  Copier et télécharger un texte produit par la page.
*/
const { ref } = Vue;

// `copied` : le nom du dernier fichier copié, le temps d'afficher « Copié ».
export function useCopy() {
  const copied = ref("");
  async function copy(file) {
    try {
      await navigator.clipboard.writeText(file.content);
      copied.value = file.name;
      setTimeout(() => { if (copied.value === file.name) copied.value = ""; }, 1400);
    } catch { /* refusé par le navigateur : le texte reste sélectionnable */ }
  }
  return { copied, copy };
}

// {name, content, download?} : le navigateur l'enregistre.
export function download(file, type = "text/plain;charset=utf-8") {
  const blob = new Blob([file.content], { type });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = file.download || file.name.split("/").pop();
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}
