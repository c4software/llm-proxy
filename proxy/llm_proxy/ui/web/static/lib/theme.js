/*
  Le THÈME de la page : « auto » (celui du système), « clair » ou
  « sombre », choisi dans le menu latéral et gardé dans ce navigateur.

  Il se pose en `data-theme` sur <html> (absent = auto) : la feuille de
  style prend les couleurs sombres sous `prefers-color-scheme: dark` sauf
  `data-theme="light"`, et toujours sous `data-theme="dark"`. Le gabarit
  pose l'attribut avant le premier rendu (script en tête de page) : pas
  d'éclair de l'autre thème au chargement.
*/
const { computed, watch } = Vue;

const THEME_KEY = "llm-proxy-theme";
const ORDER = ["auto", "light", "dark"];
const LABELS = { auto: "auto", light: "clair", dark: "sombre" };
const BAR = { light: "#faf9f5", dark: "#1a1917" };


function apply(theme) {
  const root = document.documentElement;
  if (theme === "auto") delete root.dataset.theme;
  else root.dataset.theme = theme;
  // La couleur de la barre du navigateur (application installée) suit.
  for (const meta of document.querySelectorAll('meta[name="theme-color"]')) {
    const scheme = (meta.getAttribute("media") || "").includes("dark") ? "dark" : "light";
    meta.setAttribute("content", BAR[theme === "auto" ? scheme : theme]);
  }
}

export function useTheme() {
  // Gardé par useLocalStorage (« auto » compris : le script de la page
  // l'ignore), suivi d'un onglet à l'autre.
  const theme = VueUse.useLocalStorage(THEME_KEY, "auto");
  if (!ORDER.includes(theme.value)) theme.value = "auto";
  watch(theme, apply, { immediate: true });
  const themeLabel = computed(() => LABELS[theme.value]);
  function cycleTheme() {
    theme.value = ORDER[(ORDER.indexOf(theme.value) + 1) % ORDER.length];
  }
  return { theme, themeLabel, cycleTheme };
}
