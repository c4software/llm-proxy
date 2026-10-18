// @ts-check
/*
  SQL : un script SQLite exécuté dans le bac (bac.js), en WASM (sql.js),
  sur la BASE DE LA CONVERSATION (sqlbase.js) — gardée d'un appel à
  l'autre dans ce navigateur, jamais envoyée au proxy.

  `files` IMPORTE des fichiers en tables, avant le script : ceux du
  dossier ouvert, ou les pièces jointes texte du fil. Ils sont lus par la
  page et passés au bac : leurs données ne partent pas au modèle, seul ce
  que rendent les requêtes lui revient.
    .csv, .tsv, .txt    séparateur deviné (« , », « ; », tabulation),
                        première ligne = noms des colonnes, types déduits
                        (INTEGER, REAL, sinon TEXT ; « 3,14 » vaut 3.14
                        sous « ; »)
    .json, .jsonl       un tableau d'objets, ou un objet par ligne
    .sqlite, .db        ses tables, index et vues, copiés
  Une table importée porte le nom du fichier, ses colonnes ceux de
  l'en-tête (minuscules, sans accents, « _ ») ; elle remplace celle du
  même nom.
*/
import { TOOL, FAMILY, defineTool, defineFamily, clip, failure, oneLine } from "../contract.js";
import { exec } from "./bac.js";
import * as sqlbase from "./sqlbase.js";
import { FILES_PARAM, paths as pathsOf, inputs } from "./files.js";

const TIMEOUT = 60_000;              // un gros import compris
const MAX_OUT = 20_000;
const MAX_ROWS = 200;                // lignes montrées par résultat

// Exécutée dans le Worker du bac : elle ne voit que ses arguments.
async function driver({ sql, maxRows, base, files }, { mod, wasm }) {
  const SQL = await mod.initSqlJs({ wasmBinary: wasm });
  const db = base ? new SQL.Database(base) : new SQL.Database();
  const out = [];
  const q = (s) => '"' + String(s).replace(/"/g, '""') + '"';
  const plural = (n, w) => `${n} ${w}${n > 1 ? "s" : ""}`;

  // Un identifiant prévisible : minuscules, sans accents, « _ ».
  const ident = (s, lead) => String(s ?? "").trim().normalize("NFD").replace(/[\u0300-\u036f]/g, "")
    .toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/^_+|_+$/g, "").replace(/^(\d)/, lead + "_$1");
  const tableOf = (name) => ident(name.split("/").pop().replace(/\.[^.]*$/, ""), "t") || "t";
  function columnsOf(names) {
    const seen = new Map();
    return names.map((n, i) => {
      let c = ident(n, "c") || `col_${i + 1}`;
      if (seen.has(c)) { seen.set(c, seen.get(c) + 1); c = `${c}_${seen.get(c)}`; } else seen.set(c, 1);
      return c;
    });
  }

  // Une table remplie de `rows` (tableaux de valeurs JS), ses types déduits.
  function fill(table, columns, rows, comma) {
    const types = columns.map((_, j) => {
      let t = "INTEGER", any = false;
      for (const r of rows) {
        const v = r[j];
        if (v === null || v === undefined || v === "") continue;
        any = true;
        if (typeof v === "number") { if (!Number.isInteger(v)) t = t === "TEXT" ? t : "REAL"; continue; }
        if (typeof v === "boolean") continue;
        const s = String(v).trim();
        const n = comma ? s.replace(/^(-?\d+),(\d+)$/, "$1.$2") : s;
        if (/^-?(0|[1-9]\d{0,14})$/.test(n)) continue;
        if (/^-?\d*\.?\d+(e[+-]?\d+)?$/i.test(n) && !/^-?0\d/.test(n)) { if (t !== "TEXT") t = "REAL"; continue; }
        t = "TEXT";
        break;
      }
      return any ? t : "TEXT";
    });
    const value = (v, t) => {
      if (v === null || v === undefined || v === "") return null;
      if (typeof v === "boolean") return v ? 1 : 0;
      if (typeof v === "object") return JSON.stringify(v);
      if (t === "TEXT") return typeof v === "string" ? v : String(v);
      return typeof v === "number" ? v : Number(comma ? String(v).trim().replace(",", ".") : String(v).trim());
    };
    db.run(`DROP TABLE IF EXISTS ${q(table)}`);
    db.run(`CREATE TABLE ${q(table)} (${columns.map((c, j) => `${q(c)} ${types[j]}`).join(", ")})`);
    const ins = db.prepare(`INSERT INTO ${q(table)} VALUES (${columns.map(() => "?").join(", ")})`);
    db.run("BEGIN");
    for (const r of rows) ins.run(columns.map((_, j) => value(r[j], types[j])));
    db.run("COMMIT");
    ins.free();
    return `${table} : ${plural(rows.length, "ligne")} — ${columns.map((c, j) => `${c} ${types[j]}`).join(", ")}`;
  }

  // Un CSV : séparateur deviné sur la première ligne, guillemets RFC 4180.
  function csv(text) {
    text = text.replace(/^﻿/, "");
    const first = text.slice(0, text.indexOf("\n") + 1 || text.length);
    const count = (ch) => first.split(ch).length;
    const sep = ["\t", ";", ","].reduce((a, b) => (count(b) > count(a) ? b : a), ",");
    const rows = [];
    let row = [], cell = "", quoted = false;
    for (let i = 0; i < text.length; i++) {
      const ch = text[i];
      if (quoted) {
        if (ch === '"') { if (text[i + 1] === '"') { cell += '"'; i++; } else quoted = false; } else cell += ch;
      } else if (ch === '"' && cell === "") quoted = true;
      else if (ch === sep) { row.push(cell); cell = ""; }
      else if (ch === "\n" || ch === "\r") {
        if (ch === "\r" && text[i + 1] === "\n") i++;
        row.push(cell); cell = "";
        if (row.length > 1 || row[0] !== "") rows.push(row);
        row = [];
      } else cell += ch;
    }
    if (cell !== "" || row.length) { row.push(cell); rows.push(row); }
    if (!rows.length) throw new Error("fichier vide");
    const columns = columnsOf(rows[0]);
    const body = rows.slice(1).map((r) => columns.map((_, j) => r[j]));
    return { columns, rows: body, comma: sep === ";" };
  }

  function json(text, lines) {
    const items = lines ? text.split(/\r?\n/).filter((l) => l.trim()).map((l) => JSON.parse(l)) : JSON.parse(text);
    const list = Array.isArray(items) ? items : [items];
    if (!list.every((o) => o && typeof o === "object" && !Array.isArray(o))) {
      throw new Error("un tableau d'objets est attendu");
    }
    const keys = [];
    for (const o of list) for (const k of Object.keys(o)) if (!keys.includes(k)) keys.push(k);
    return { columns: columnsOf(keys), rows: list.map((o) => keys.map((k) => o[k])) };
  }

  // Une base SQLite : ses tables (avec leurs lignes), puis index, vues, déclencheurs.
  function sqlite(bytes) {
    const src = new SQL.Database(bytes);
    const done = [];
    try {
      const objs = src.exec("SELECT type, name, sql FROM sqlite_schema WHERE sql IS NOT NULL "
        + "AND name NOT LIKE 'sqlite_%' ORDER BY type <> 'table'")[0];
      for (const [type, name, ddl] of objs ? objs.values : []) {
        try {
          db.run(`DROP ${type.toUpperCase()} IF EXISTS ${q(name)}`);
          db.run(ddl);
          if (type !== "table") continue;
          const sel = src.prepare(`SELECT * FROM ${q(name)}`);
          const n = sel.getColumnNames().length;
          const ins = db.prepare(`INSERT INTO ${q(name)} VALUES (${Array(n).fill("?").join(", ")})`);
          let rows = 0;
          db.run("BEGIN");
          while (sel.step()) { ins.run(sel.get()); rows++; }
          db.run("COMMIT");
          sel.free(); ins.free();
          done.push(`${name} : ${plural(rows, "ligne")}`);
        } catch (e) {
          try { db.run("ROLLBACK"); } catch { /* pas de transaction */ }
          done.push(`${type} ${name} non copié : ${e.message}`);
        }
      }
    } finally {
      src.close();
    }
    return done.length ? done.join("\n") : "aucune table";
  }

  for (const f of files) {
    try {
      const ext = (f.name.match(/\.([^./]+)$/) || [])[1]?.toLowerCase() || "";
      const head = new TextDecoder().decode(f.bytes.slice(0, 16));
      let line;
      if (head.startsWith("SQLite format 3")) line = sqlite(f.bytes);
      else {
        const text = new TextDecoder().decode(f.bytes);
        const t = ext === "json" ? json(text, false) : ext === "jsonl" || ext === "ndjson" ? json(text, true) : csv(text);
        line = fill(tableOf(f.name), t.columns, t.rows, t.comma);
      }
      out.push(`Importé ${f.name} →\n${line}`);
    } catch (e) {
      try { db.run("ROLLBACK"); } catch { /* pas de transaction */ }
      out.push(`Error: import de ${f.name} : ${e.message}`);
    }
  }

  const cell = (v) => (v === null ? "NULL" : v instanceof Uint8Array ? `<blob ${v.length} octets>`
    : String(v).replace(/\r?\n/g, "\\n").replace(/\|/g, "\\|"));
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
        out.push(`#${n} : ${plural(total, "ligne")}\n` + [head, sep, ...body, ...more].join("\n"));
      } else if (/^\s*(insert|update|delete|replace)\b/i.test(stmt.getSQL())) {
        const changed = db.getRowsModified();
        out.push(`#${n} : ${plural(changed, "ligne")} touchée${changed > 1 ? "s" : ""}`);
      }
      stmt.free();
    }
  } catch (e) {
    out.push(`Error: requête #${n + 1} : ${e.message}`);
  }
  const tables = db.exec("SELECT count(*) FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")[0].values[0][0];
  const bytes = db.export();
  db.close();
  return { text: out.join("\n\n"), statements: n, tables, base: bytes };
}

const runSql = defineTool({
  name: TOOL.RUN_SQL,
  description: "Exécute un script SQL (dialecte SQLite 3) dans un bac isolé du navigateur, sur la "
    + "base de la conversation : ses tables restent d'un appel à l'autre (`reset` repart d'une base "
    + "vide). `files` importe d'abord des fichiers en tables, depuis le dossier ouvert ou les pièces "
    + "jointes : CSV/TSV (en-tête, séparateur et types devinés), JSON (tableau d'objets), JSONL, "
    + "ou une base .sqlite/.db — leurs données restent sur l'appareil, seuls les résultats des "
    + "requêtes reviennent. Une table importée porte le nom du fichier, ses colonnes ceux de "
    + "l'en-tête (minuscules, sans accents, « _ ») ; l'import rend son schéma. Revoir les tables : SELECT name, sql FROM sqlite_schema. Rend "
    + `chaque résultat en tableau (${MAX_ROWS} lignes au plus) : agréger en SQL plutôt que tout lire.`,
  params: {
    sql: { type: "string", description: "Le script : une ou plusieurs instructions séparées par `;` (vide : seulement importer)." },
    files: { ...FILES_PARAM.files, description: "Fichiers à importer avant le script : chemins dans le dossier ouvert, ou noms de pièces jointes." },
    reset: { type: "boolean", description: "Vider la base de la conversation avant (défaut : non)." },
    download: { type: "boolean", description: "Proposer à l'utilisateur de télécharger la base (.sqlite) sous la réponse." },
  },
  summary: (a) => oneLine([...(a.files || []), a.sql].filter(Boolean).join(" · ")),
  async run(args, ctx) {
    const sql = String(args.sql || "");
    const paths = pathsOf(args);
    if (!sql.trim() && !paths.length && !args.download) return failure("`sql` ou `files` attendu.");
    let files;
    try { files = await inputs(ctx, paths); } catch (e) { return failure(e.message + "."); }
    const conv = ctx.conv || {};
    const id = conv.id || "";
    const base = args.reset || !id ? null : await sqlbase.load(id);
    const r = await exec({ driver, input: { sql, maxRows: MAX_ROWS, base, files }, runtime: "sql", timeout: TIMEOUT });
    if (!r.ok) return failure(r.error);
    const { text, statements, tables, base: after } = r.value;
    const out = [text || `${statements} instruction${statements > 1 ? "s" : ""} exécutée${statements > 1 ? "s" : ""}, sans résultat.`];
    if (id) {
      const kept = await sqlbase.save(id, after, !conv.incognito);
      const size = after.length >= 1e6 ? `${(after.length / 1e6).toFixed(1)} Mo` : `${Math.ceil(after.length / 1e3)} Ko`;
      out.push(`Base de la conversation : ${tables} table${tables > 1 ? "s" : ""}, ${size}`
        + (kept ? "" : " (en mémoire seulement : perdue au rechargement)") + ".");
      if (args.download && ctx.live) {
        const name = "conversation.sqlite";
        (ctx.live.files || (ctx.live.files = [])).push({ name, type: "application/vnd.sqlite3", size: after.length, base: id });
        out.push(`Base proposée au téléchargement sous la réponse (${name}).`);
      }
    }
    return clip(out.join("\n\n"), MAX_OUT);
  },
});

export const FAMILIES = [defineFamily({
  key: FAMILY.SQL, label: "SQL", hint: "SQLite en WASM : une base par conversation, import de CSV, JSON, .sqlite",
  available: () => typeof WebAssembly === "object",
  tools: [runSql],
})];
