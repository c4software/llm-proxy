// Le runtime PostgreSQL du bac (chat/tools/sandbox/postgres.js) : PGlite,
// PostgreSQL compilé en WASM, en un seul processus. Ses binaires
// (pglite.wasm, initdb.wasm) et son système de fichiers (pglite.data)
// sont passés par l'appelant : le bac n'a pas le droit de les télécharger.
export { PGlite } from "@electric-sql/pglite";
