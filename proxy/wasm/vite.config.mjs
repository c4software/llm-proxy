// La PAGE du proxy (web/static, composants .vue) construite par Vite dans
// web/dist, que le proxy sert sous /ui (ui/static_files.py) ; et ses tests
// (Vitest, tests/). Usage : npm run build | npm run dev | npm run check.
//
//   - `vue` est le build qui embarque le compilateur de gabarits : le
//     composant d'une extension, chargé à l'exécution, peut encore donner
//     un `template` texte (docs/extensions.md) ;
//   - les fonctions envoyées en TEXTE aux bacs et aux cadres (String(fn) :
//     les `driver` des bacs, le pont des artifacts) doivent sortir du build
//     telles quelles : cible es2022 (aucune aide de transpilation) et pas
//     de keepNames (qui injecterait `__name`) ; tests/serialized.test.mjs
//     les vérifie dans la source et dans le bundle construit ;
//   - vendor/ (runtimes des bacs, VM Linux) et les modules des extensions
//     restent hors du bundle : servis tels quels, chargés par URL.
//
// En développement, `npm run dev` sert la page sur http://localhost:5173/ui/static/
// et relaie le reste (/ui/…, /v1/…, /hooks/…, /healthz) au proxy local —
// LLM_PROXY_URL, http://127.0.0.1:8000 par défaut.
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

const here = (p) => fileURLToPath(new URL(p, import.meta.url));
const ROOT = here("../llm_proxy/ui/web/static");
const PROXY = process.env.LLM_PROXY_URL || "http://127.0.0.1:8000";

export default defineConfig({
  // vendor/, sous la racine, n'est pas une ressource de la page : rien ne
  // l'importe, il reste hors du build (servi à part par le proxy).
  root: ROOT,
  // Le cache de Vite ici, pas dans les sources.
  cacheDir: here("node_modules/.vite"),
  base: "/ui/static/",
  publicDir: "public",
  plugins: [vue()],
  resolve: {
    // Les sources sont hors de ce dossier : leurs imports nus se résolvent
    // ici, dans node_modules de proxy/wasm.
    alias: [
      { find: /^vue$/, replacement: here("node_modules/vue/dist/vue.esm-bundler.js") },
      { find: /^@vueuse\/core$/, replacement: here("node_modules/@vueuse/core/dist/index.js") },
      // Le rendu serveur : seulement pour charger les modules hors du
      // navigateur (check-tools.mjs, qui n'en rend aucun).
      { find: /^vue\/server-renderer$/, replacement: here("node_modules/vue/server-renderer/index.mjs") },
    ],
    dedupe: ["vue"],
  },
  define: {
    __VUE_OPTIONS_API__: "true",
    __VUE_PROD_DEVTOOLS__: "false",
    __VUE_PROD_HYDRATION_MISMATCH_DETAILS__: "false",
  },
  build: {
    outDir: here("../llm_proxy/ui/web/dist"),
    emptyOutDir: true,
    target: "es2022",
  },
  server: {
    port: 5173,
    strictPort: true,
    fs: { allow: [ROOT, here(".")] },
    proxy: {
      "^/ui(?!/static/)": { target: PROXY, changeOrigin: true, ws: true },
      "/v1": { target: PROXY, changeOrigin: true },
      "/hooks": { target: PROXY, changeOrigin: true },
      "/healthz": { target: PROXY, changeOrigin: true },
    },
  },
  test: {
    root: here("."),
    include: ["tests/**/*.test.mjs"],
    environment: "./tests/page-env.mjs",
  },
});
