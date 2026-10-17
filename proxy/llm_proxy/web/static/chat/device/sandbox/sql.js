// SQL : un script SQLite exécuté dans le bac (bac.js), en WASM (sql.js),
// sur une base vide en mémoire. Rend le résultat de chaque requête qui
// renvoie des lignes, et le nombre de lignes touchées des autres.
import { TOOL, FAMILY, defineTool, defineFamily, clip, failure, oneLine } from "../contract.js";
import { exec } from "./bac.js";

const TIMEOUT = 20_000;
const MAX_OUT = 20_000;
const MAX_ROWS = 200;                // lignes montrées par résultat

// Exécutée dans le Worker du bac : elle ne voit que ses arguments.
async function driver({ sql, maxRows }, { mod, wasm }) {
  const SQL = await mod.initSqlJs({ wasmBinary: wasm });
  const db = new SQL.Database();
  const cell = (v) => (v === null ? "NULL" : v instanceof Uint8Array ? `<blob ${v.length} octets>`
    : String(v).replace(/\r?\n/g, "\\n").replace(/\|/g, "\\|"));
  const out = [];
  let n = 0;
  try {
    for (const stmt of db.iterateStatements(sql)) {
      n++;
      const columns = stmt.getColumnNames();
      const rows = [];
      let total = 0;
      while (stmt.step()) { if (total++ < maxRows) rows.push(stmt.get()); }
      if (columns.length) {
        const head = `| ${columns.map(cell).join(" | ")} |`;
        const sep = `| ${columns.map(() => "---").join(" | ")} |`;
        const body = rows.map((r) => `| ${r.map(cell).join(" | ")} |`);
        const more = total > rows.length ? [`… ${total - rows.length} lignes de plus`] : [];
        out.push(`#${n} : ${total} ligne${total > 1 ? "s" : ""}\n` + [head, sep, ...body, ...more].join("\n"));
      } else {
        const changed = db.getRowsModified();
        if (/^\s*(insert|update|delete|replace)\b/i.test(stmt.getSQL())) {
          out.push(`#${n} : ${changed} ligne${changed > 1 ? "s" : ""} touchée${changed > 1 ? "s" : ""}`);
        }
      }
      stmt.free();
    }
  } catch (e) {
    out.push(`Error: requête #${n + 1} : ${e.message}`);
  } finally {
    db.close();
  }
  return { text: out.join("\n\n"), statements: n };
}

const runSql = defineTool({
  name: TOOL.RUN_SQL,
  description: "Exécute un script SQL (dialecte SQLite 3) dans un bac isolé du navigateur, sur une "
    + "base vide en mémoire : pour tester une requête, montrer son résultat, calculer sur un petit "
    + "jeu de données. Chaque appel part d'une base vide : mettre CREATE TABLE et INSERT dans le "
    + `même script que les SELECT. Rend chaque résultat en tableau (${MAX_ROWS} lignes au plus).`,
  params: { sql: { type: "string", description: "Le script : une ou plusieurs instructions séparées par `;`." } },
  required: ["sql"],
  summary: (a) => oneLine(a.sql),
  async run(args) {
    const sql = String(args.sql || "");
    if (!sql.trim()) return failure("`sql` attendu.");
    const r = await exec({ driver, input: { sql, maxRows: MAX_ROWS }, runtime: "sql", timeout: TIMEOUT });
    if (!r.ok) return failure(r.error);
    const { text, statements } = r.value;
    return clip(text || `${statements} instruction${statements > 1 ? "s" : ""} exécutée${statements > 1 ? "s" : ""}, sans résultat.`, MAX_OUT);
  },
});

export const FAMILIES = [defineFamily({
  key: FAMILY.SQL, label: "SQL", hint: "SQLite en WASM, dans un bac isolé",
  available: () => typeof WebAssembly === "object",
  tools: [runSql],
})];
