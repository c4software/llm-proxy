// Le runtime SQLite du bac (chat/device/sandbox/sql.js) : sql.js, SQLite
// compilé en WASM. Le binaire est passé par l'appelant (`wasmBinary`).
export { default as initSqlJs } from "sql.js/dist/sql-wasm-browser.js";
