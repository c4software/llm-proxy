// @ts-check
/*
  Un FICHIER DE DONNÉES lu en table, pour les imports de run_sql et
  run_postgres : passé au bac en texte (exec({lib: {tabular}})), il ne
  voit rien d'autre que ses arguments — ni import, ni variable du module.

  tabular(nom, octets) → {table, columns, types, rows}
    .csv, .tsv, .txt    séparateur deviné sur la première ligne (« , »,
                        « ; », tabulation), guillemets RFC 4180, première
                        ligne = noms des colonnes
    .json               un tableau d'objets (ou un objet seul)
    .jsonl, .ndjson     un objet par ligne ; les valeurs imbriquées sont
                        gardées en JSON
  `table` : le nom du fichier, `columns` ceux de l'en-tête — minuscules,
  sans accents, « _ », sans doublon. `types` : INTEGER, REAL ou TEXT,
  déduits des valeurs (« 3,14 » vaut 3.14 sous « ; », « 007 » reste du
  texte). `rows` : les valeurs converties (null, nombre, texte ; un
  booléen vaut 1 ou 0).
*/

/** @param {string} name @param {Uint8Array} bytes */
export function tabular(name, bytes) {
  // Un identifiant prévisible : minuscules, sans accents, « _ ».
  const ident = (/** @type {any} */ s, /** @type {string} */ lead) => String(s ?? "").trim().normalize("NFD")
    .replace(/[̀-ͯ]/g, "").toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/^_+|_+$/g, "")
    .replace(/^(\d)/, lead + "_$1");
  /** @param {any[]} names */
  function columnsOf(names) {
    const seen = new Map();
    return names.map((n, i) => {
      let c = ident(n, "c") || `col_${i + 1}`;
      if (seen.has(c)) { seen.set(c, seen.get(c) + 1); c = `${c}_${seen.get(c)}`; } else seen.set(c, 1);
      return c;
    });
  }

  // Un CSV : séparateur deviné sur la première ligne, guillemets RFC 4180.
  /** @param {string} text */
  function csv(text) {
    text = text.replace(/^﻿/, "");
    const first = text.slice(0, text.indexOf("\n") + 1 || text.length);
    const count = (/** @type {string} */ ch) => first.split(ch).length;
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

  /** @param {string} text @param {boolean} lines */
  function json(text, lines) {
    const items = lines ? text.split(/\r?\n/).filter((l) => l.trim()).map((l) => JSON.parse(l)) : JSON.parse(text);
    const list = Array.isArray(items) ? items : [items];
    if (!list.every((o) => o && typeof o === "object" && !Array.isArray(o))) {
      throw new Error("un tableau d'objets est attendu");
    }
    /** @type {string[]} */
    const keys = [];
    for (const o of list) for (const k of Object.keys(o)) if (!keys.includes(k)) keys.push(k);
    return { columns: columnsOf(keys), rows: list.map((o) => keys.map((k) => o[k])), comma: false };
  }

  const ext = (name.match(/\.([^./]+)$/) || [])[1]?.toLowerCase() || "";
  const text = new TextDecoder().decode(bytes);
  const { columns, rows, comma } = ext === "json" ? json(text, false)
    : ext === "jsonl" || ext === "ndjson" ? json(text, true) : csv(text);

  // Les types, déduits des valeurs de chaque colonne.
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
  /** @param {any} v @param {string} t */
  const value = (v, t) => {
    if (v === null || v === undefined || v === "") return null;
    if (typeof v === "boolean") return v ? 1 : 0;
    if (typeof v === "object") return JSON.stringify(v);
    if (t === "TEXT") return typeof v === "string" ? v : String(v);
    return typeof v === "number" ? v : Number(comma ? String(v).trim().replace(",", ".") : String(v).trim());
  };
  const table = ident(name.split("/").pop()?.replace(/\.[^.]*$/, ""), "t") || "t";
  return { table, columns, types, rows: rows.map((r) => r.map((v, j) => value(v, types[j]))) };
}
