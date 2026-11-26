// L'environnement des tests de la page : Node, mais les modules compilés
// comme pour le NAVIGATEUR (viteEnvironment "client") — un .vue compilé
// pour le rendu serveur exigerait un contexte SSR à chaque setup(). Le
// minimum du navigateur (document, location…) est posé par setup.mjs.
export default {
  name: "page",
  viteEnvironment: "client",
  setup() {
    return { teardown() {} };
  },
};
