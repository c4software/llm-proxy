// @ts-check
/*
  PostgreSQL : un script SQL exécuté dans le bac (bac.js) par PGlite,
  PostgreSQL compilé en WASM (≈ 17 Mo au premier appel), sur la BASE DE
  LA CONVERSATION — son dossier de données, compressé, gardé d'un appel à
  l'autre dans ce navigateur (sqlbase.js, sous « pg:<id> »), jamais
  envoyé au proxy.

  `files` IMPORTE des fichiers avant le script, comme run_sql :
    .csv, .tsv, .txt,   une table (tables.js) : bigint, double precision
    .json, .jsonl       ou text, remplie par COPY ; elle remplace celle
                        du même nom
    .sql                exécuté tel quel (un schéma, un dump en INSERT)

  Le script part en une seule requête : ses instructions forment une
  transaction implicite (sauf BEGIN/COMMIT explicites), une erreur annule
  tout le script.
*/
import { TOOL, FAMILY, PageTool, defineFamily, clip, failure, oneLine } from "../contract.js";
import { exec } from "./bac.js";
import * as sqlbase from "./sqlbase.js";
import { FILES_PARAM, paths as pathsOf, inputs } from "./files.js";
import { tabular } from "./tables.js";

const TIMEOUT = 60_000;              // le premier appel compile PostgreSQL
const MAX_OUT = 20_000;
const MAX_ROWS = 200;                // lignes montrées par résultat
const key = (/** @type {string} */ id) => `pg:${id}`;

// Exécutée dans le Worker du bac : elle ne voit que ses arguments.
async function driver({ sql, maxRows, base, files }, { mod, bins, lib }) {
  const [pgliteWasmModule, initdbWasmModule] = await Promise.all(
    [WebAssembly.compile(bins["pglite.wasm"]), WebAssembly.compile(bins["initdb.wasm"])]);
  const pg = await mod.PGlite.create({ pgliteWasmModule, initdbWasmModule, fsBundle: new Blob([bins["pglite.data"]]),
                                       ...(base ? { loadDataDir: new Blob([base]) } : {}) });
  const out = [];
  const q = (s) => '"' + String(s).replace(/"/g, '""') + '"';
  const plural = (n, w) => `${n} ${w}${n > 1 ? "s" : ""}`;
  const TYPES = { INTEGER: "bigint", REAL: "double precision", TEXT: "text" };
  // Dates, heures et JSON en texte, tels que PostgreSQL les écrit.
  const raw = (v) => v;
  const parsers = Object.fromEntries([1082, 1083, 1114, 1184, 1266, 114, 3802].map((oid) => [oid, raw]));
  const errorOf = (e) => [e.message, e.detail && `Détail : ${e.detail}`, e.hint && `Piste : ${e.hint}`,
    e.position && `Position : caractère ${e.position}`, e.where && `Contexte : ${e.where}`].filter(Boolean).join("\n");

  // Une table remplie d'un fichier lu (lib.tabular, tables.js), par COPY.
  async function fill({ table, columns, types, rows }) {
    const cell = (v) => (v === null ? "" : '"' + String(v).replace(/"/g, '""') + '"');
    const blob = new Blob([rows.map((r) => r.map(cell).join(",")).join("\n")]);
    const cols = columns.map((c, j) => `${q(c)} ${TYPES[types[j]]}`);
    await pg.exec(`DROP TABLE IF EXISTS ${q(table)}; CREATE TABLE ${q(table)} (${cols.join(", ")})`);
    if (rows.length) await pg.query(`COPY ${q(table)} FROM '/dev/blob' WITH (FORMAT csv)`, [], { blob });
    return `${table} : ${plural(rows.length, "ligne")} — ${cols.join(", ").replace(/"/g, "")}`;
  }

  for (const f of files) {
    try {
      const ext = (f.name.match(/\.([^./]+)$/) || [])[1]?.toLowerCase() || "";
      const head = new TextDecoder().decode(f.bytes.slice(0, 16));
      let line;
      if (head.startsWith("SQLite format 3")) throw new Error("une base SQLite s'importe avec run_sql");
      if (ext === "sql") {
        const r = await pg.exec(new TextDecoder().decode(f.bytes));
        line = `script exécuté : ${plural(r.length, "instruction")}`;
      } else line = await fill(lib.tabular(f.name, f.bytes));
      out.push(`Importé ${f.name} →\n${line}`);
    } catch (e) {
      out.push(`Error: import de ${f.name} : ${errorOf(e)}`);
    }
  }

  const cell = (v) => (v === null || v === undefined ? "NULL"
    : v instanceof Uint8Array ? `<bytea ${v.length} octets>`
    : typeof v === "object" ? JSON.stringify(v, (_, x) => (typeof x === "bigint" ? String(x) : x))
    : String(v)).replace(/\r?\n/g, "\\n").replace(/\|/g, "\\|");
  const notices = [];
  let n = 0;
  if (sql.trim()) {
    try {
      const results = await pg.exec(sql, { rowMode: "array", parsers,
        onNotice: (m) => notices.push(`${m.severity || "NOTICE"} : ${m.message}`) });
      for (const r of results) {
        n++;
        if (r.fields.length) {
          const rows = r.rows.slice(0, maxRows);
          const head = `| ${r.fields.map((f) => cell(f.name)).join(" | ")} |`;
          const sep = `| ${r.fields.map(() => "---").join(" | ")} |`;
          const body = rows.map((row) => `| ${row.map(cell).join(" | ")} |`);
          const more = r.rows.length > rows.length ? [`… ${r.rows.length - rows.length} lignes de plus`] : [];
          out.push(`#${n} : ${plural(r.rows.length, "ligne")}\n` + [head, sep, ...body, ...more].join("\n"));
        } else if (r.affectedRows) {
          out.push(`#${n} : ${plural(r.affectedRows, "ligne")} touchée${r.affectedRows > 1 ? "s" : ""}`);
        }
      }
    } catch (e) {
      out.push(`Error: ${errorOf(e)}\n(script annulé : rien n'est gardé, sauf ce qu'un COMMIT explicite a validé avant l'erreur)`);
    }
  }
  if (notices.length) out.push("Messages :\n" + notices.join("\n"));
  const tables = (await pg.query("SELECT count(*)::int AS n FROM pg_tables "
    + "WHERE schemaname NOT IN ('pg_catalog', 'information_schema')")).rows[0].n;
  // Une base sans objet de l'utilisateur n'est pas gardée : vide, elle pèse déjà ≈ 4 Mo.
  const SYSTEM = "('pg_catalog', 'information_schema', 'pg_toast')";
  const objects = (await pg.query(`SELECT (SELECT count(*) FROM pg_namespace WHERE nspname NOT IN ${SYSTEM} AND nspname <> 'public' AND nspname NOT LIKE 'pg_temp%' AND nspname NOT LIKE 'pg_toast_temp%')
    + (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname NOT IN ${SYSTEM})
    + (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname NOT IN ${SYSTEM})
    + (SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace WHERE n.nspname NOT IN ${SYSTEM})
    + (SELECT count(*) FROM pg_extension WHERE extname <> 'plpgsql') AS n`)).rows[0].n;
  const dump = objects ? new Uint8Array(await (await pg.dumpDataDir("gzip")).arrayBuffer()) : null;
  await pg.close();
  return { text: out.join("\n\n"), statements: n, tables, base: dump };
}

class RunPostgres extends PageTool {
  constructor() {
    super({
      name: TOOL.RUN_POSTGRES,
      description: "Exécute un script SQL PostgreSQL (PGlite : PostgreSQL complet en WASM, PL/pgSQL, "
        + "JSONB, CTE, fonctions fenêtres, types…) dans un bac isolé du navigateur, sur la base "
        + "PostgreSQL de la conversation : ses tables restent d'un appel à l'autre (`reset` repart "
        + "d'une base vide). `files` importe d'abord des fichiers du dossier ouvert ou des pièces "
        + "jointes : CSV/TSV, JSON, JSONL en tables (nom du fichier ; colonnes de l'en-tête en "
        + "minuscules sans accents ; bigint, double precision ou text déduits), ou un script .sql "
        + "exécuté tel quel — leurs données restent sur l'appareil, seuls les résultats reviennent. "
        + "Le script est une transaction implicite : une erreur l'annule (sauf COMMIT explicite). Rend chaque "
        + `résultat en tableau (${MAX_ROWS} lignes au plus) et les messages RAISE NOTICE. Revoir les `
        + "tables : SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'.",
      params: {
        sql: { type: "string", description: "Le script : une ou plusieurs instructions séparées par `;` (vide : seulement importer)." },
        files: { ...FILES_PARAM.files, description: "Fichiers à importer avant le script : chemins dans le dossier ouvert, ou noms de pièces jointes." },
        reset: { type: "boolean", description: "Vider la base PostgreSQL de la conversation avant (défaut : non)." },
      },
    });
  }

  /** @override @param {string} convId */
  forget(convId) { return sqlbase.drop(key(convId)); }

  /** @override @param {any} a */
  summary(a) { return oneLine([...(a.files || []), a.sql].filter(Boolean).join(" · ")); }

  /** @override @param {any} args @param {import("../contract.js").Ctx} ctx */
  async run(args, ctx) {
    const sql = String(args.sql || "");
    const paths = pathsOf(args);
    if (!sql.trim() && !paths.length) return failure("`sql` ou `files` attendu.");
    let files;
    try { files = await inputs(ctx, paths); } catch (e) { return failure(e.message + "."); }
    const conv = ctx.conv || {};
    const id = conv.id || "";
    const base = args.reset || !id ? null : await sqlbase.load(key(id));
    const r = await exec({ driver, input: { sql, maxRows: MAX_ROWS, base, files }, runtime: "pglite",
                           lib: { tabular }, timeout: TIMEOUT });
    if (!r.ok) return failure(r.error);
    const { text, statements, tables, base: after } = r.value;
    const out = [text || `${statements} instruction${statements > 1 ? "s" : ""} exécutée${statements > 1 ? "s" : ""}, sans résultat.`];
    if (id && !after) {
      await sqlbase.drop(key(id));
      out.push("Base PostgreSQL de la conversation : vide.");
    } else if (id) {
      const kept = await sqlbase.save(key(id), after, !conv.incognito);
      const size = after.length >= 1e6 ? `${(after.length / 1e6).toFixed(1)} Mo` : `${Math.ceil(after.length / 1e3)} Ko`;
      out.push(`Base PostgreSQL de la conversation : ${tables} table${tables > 1 ? "s" : ""}, ${size} compressée`
        + (kept ? "" : " (en mémoire seulement : perdue au rechargement)") + ".");
    }
    return clip(out.join("\n\n"), MAX_OUT);
  }
}

export const FAMILIES = [defineFamily({
  key: FAMILY.POSTGRES, label: "PostgreSQL",
  hint: "PostgreSQL en WASM (PGlite) : une base par conversation, import de CSV, JSON, .sql (≈ 17 Mo au premier appel)",
  available: () => typeof WebAssembly === "object",
  guidance: () => "run_postgres a sa propre base PostgreSQL par conversation, distincte de celle de "
    + "run_sql (SQLite) : l'utiliser quand l'utilisateur veut du PostgreSQL (son dialecte, PL/pgSQL, "
    + "JSONB, un schéma à tester avant de le déployer).",
  tools: [new RunPostgres()],
})];
