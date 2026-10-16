/*
  Le rendu Markdown du chat : un sous-ensemble de CommonMark + GFM, écrit
  ici pour garder la page sans dépendance ni étape de compilation.

  SÛR PAR CONSTRUCTION : tout le texte est échappé d'abord, puis seules
  des balises connues sont produites — aucun HTML du modèle ne passe.
  Les liens et images n'acceptent que http(s), mailto et les chemins du
  proxy (/v1/files/… : les fichiers rendus par les outils).

  Couvert : titres, paragraphes (un saut de ligne simple reste un saut de
  ligne, comme dans toute interface de chat), listes à puces et
  numérotées imbriquées, citations, blocs de code (```lang, avec bouton
  « Copier »), tableaux, règles, code en ligne, gras, italique, barré,
  liens, images, URL nues. Un bloc de code pas encore fermé (réponse en
  cours de flux) s'affiche déjà comme du code.

  Markdown.render(texte) → chaîne HTML, pour v-html.
*/
window.Markdown = (() => {
  const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");

  // URL déjà ÉCHAPPÉE (le texte l'est avant toute analyse).
  function safeUrl(u) {
    u = u.trim();
    if (/^(https?:|mailto:)/i.test(u) || /^\/(?!\/)/.test(u) || /^#/.test(u)) {
      return u;
    }
    return null;
  }

  // ── en ligne ──────────────────────────────────────────────────────────
  function inline(src) {
    const held = [];
    const hold = (html) => "\u0000" + (held.push(html) - 1) + "\u0000";
    // Le code en ligne d'abord : rien de ce qu'il contient n'est interprété.
    let s = src.replace(/(`+)([\s\S]*?[^`])\1(?!`)/g,
      (_, __, code) => hold("<code>" + esc(code.replace(/^ (.*) $/, "$1")) + "</code>"));
    s = esc(s);
    // Images, puis liens : leur texte passe à son tour par l'emphase.
    s = s.replace(/!\[([^\]]*)\]\(\s*([^)\s]+)(?:\s+&quot;[^]*?&quot;)?\s*\)/g,
      (all, alt, url) => {
        const ok = safeUrl(url);
        return ok ? hold(`<a href="${ok}" target="_blank" rel="noopener noreferrer">`
          + `<img src="${ok}" alt="${alt}" loading="lazy"></a>`) : all;
      });
    s = s.replace(/\[([^\]]+)\]\(\s*([^)\s]+)(?:\s+&quot;[^]*?&quot;)?\s*\)/g,
      (all, text, url) => {
        const ok = safeUrl(url);
        return ok ? hold(`<a href="${ok}" target="_blank" rel="noopener noreferrer">`
          + emphasis(text) + "</a>") : all;
      });
    // URL nues ; la ponctuation finale reste hors du lien.
    s = s.replace(/\bhttps?:\/\/[^\s<]*[^\s<.,:;!?)\]'"*_~]/g, (url) => {
      // `&quot;` et `&#39;` finaux viennent de guillemets : hors du lien.
      const m = url.match(/^(.*?)((?:&quot;|&#39;|&gt;)+.*)?$/);
      const href = m[1], rest = m[2] || "";
      return hold(`<a href="${href}" target="_blank" rel="noopener noreferrer">${href}</a>`) + rest;
    });
    s = emphasis(s);
    return s.replace(/\u0000(\d+)\u0000/g, (_, i) => held[+i])
      .replace(/\u0000(\d+)\u0000/g, (_, i) => held[+i]);
  }

  function emphasis(s) {
    return s
      .replace(/\*\*(?=\S)([\s\S]*?\S)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^\w])__(?=\S)([\s\S]*?\S)__(?!\w)/g, "$1<strong>$2</strong>")
      .replace(/(^|[^*\w])\*(?=[^\s*])([\s\S]*?[^\s*])\*(?!\*)/g, "$1<em>$2</em>")
      .replace(/(^|[^\w])_(?=[^\s_])([\s\S]*?[^\s_])_(?!\w)/g, "$1<em>$2</em>")
      .replace(/~~(?=\S)([\s\S]*?\S)~~/g, "<del>$1</del>");
  }

  // ── blocs ─────────────────────────────────────────────────────────────
  const FENCE = /^ {0,3}(`{3,}|~{3,})\s*([^`\s]*)/;
  const HEADING = /^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
  const RULE = /^ {0,3}([-*_])(\s*\1){2,}\s*$/;
  const ITEM = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
  const QUOTE = /^ {0,3}>\s?(.*)$/;
  const DIVIDER = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;

  const indentOf = (line) => line.match(/^\s*/)[0].replace(/\t/g, "    ").length;

  function startsBlock(line, next) {
    return FENCE.test(line) || HEADING.test(line) || RULE.test(line)
      || ITEM.test(line) || QUOTE.test(line) || isTable(line, next);
  }

  function isTable(line, next) {
    return line.includes("|") && next !== undefined && DIVIDER.test(next)
      && next.includes("-");
  }

  function cells(line) {
    let s = line.trim();
    if (s.startsWith("|")) s = s.slice(1);
    if (s.endsWith("|") && !s.endsWith("\\|")) s = s.slice(0, -1);
    return s.split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, "|"));
  }

  function codeBlock(lang, body) {
    return `<div class="code"><div class="code-head"><span>${esc(lang || "code")}</span>`
      + `<button type="button" class="copy">Copier</button></div>`
      + `<pre><code>${esc(body)}</code></pre></div>`;
  }

  function blocks(lines) {
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i++; continue; }

      let m = line.match(FENCE);
      if (m) {
        const fence = m[1], body = [];
        const close = new RegExp("^ {0,3}" + fence[0] + "{" + fence.length + ",}\\s*$");
        const pad = indentOf(line);
        i++;
        while (i < lines.length && !close.test(lines[i])) {
          body.push(lines[i].replace(new RegExp("^ {0," + pad + "}"), ""));
          i++;
        }
        i++;   // la clôture (ou la fin du texte, en cours de flux)
        out.push(codeBlock(m[2], body.join("\n")));
        continue;
      }
      if ((m = line.match(HEADING))) {
        const n = m[1].length;
        out.push(`<h${n}>${inline(m[2])}</h${n}>`);
        i++;
        continue;
      }
      if (RULE.test(line)) { out.push("<hr>"); i++; continue; }
      if (isTable(line, lines[i + 1])) {
        const head = cells(line);
        const align = cells(lines[i + 1]).map((c) =>
          c.startsWith(":") && c.endsWith(":") ? "center"
            : c.endsWith(":") ? "right" : c.startsWith(":") ? "left" : "");
        const style = (k) => align[k] ? ` style="text-align:${align[k]}"` : "";
        let html = "<div class=\"table\"><table><thead><tr>"
          + head.map((c, k) => `<th${style(k)}>${inline(c)}</th>`).join("")
          + "</tr></thead><tbody>";
        i += 2;
        while (i < lines.length && lines[i].trim() && lines[i].includes("|")) {
          const row = cells(lines[i]);
          html += "<tr>" + head.map((_, k) =>
            `<td${style(k)}>${inline(row[k] || "")}</td>`).join("") + "</tr>";
          i++;
        }
        out.push(html + "</tbody></table></div>");
        continue;
      }
      if (QUOTE.test(line)) {
        const body = [];
        while (i < lines.length && lines[i].trim()) {
          const q = lines[i].match(QUOTE);
          body.push(q ? q[1] : lines[i]);
          i++;
        }
        out.push("<blockquote>" + blocks(body).join("") + "</blockquote>");
        continue;
      }
      if (ITEM.test(line)) {
        const r = list(lines, i);
        out.push(r.html);
        i = r.next;
        continue;
      }
      // Paragraphe : jusqu'à une ligne vide ou le début d'un autre bloc.
      const para = [line.trim()];
      i++;
      while (i < lines.length && lines[i].trim()
             && !startsBlock(lines[i], lines[i + 1])) {
        para.push(lines[i].trim());
        i++;
      }
      out.push("<p>" + para.map(inline).join("<br>") + "</p>");
    }
    return out;
  }

  // Une liste, imbriquée par l'indentation : un élément garde les lignes
  // plus indentées que sa puce (sous-listes, code, paragraphes suivants).
  function list(lines, i) {
    const first = lines[i].match(ITEM);
    const base = indentOf(first[1]);
    const ordered = /\d/.test(first[2]);
    const start = ordered ? parseInt(first[2], 10) : 1;
    const items = [];
    let loose = false;
    while (i < lines.length) {
      const line = lines[i];
      const m = line.match(ITEM);
      if (m && indentOf(m[1]) <= base + 1 && /\d/.test(m[2]) === ordered) {
        items.push({ width: indentOf(m[1]) + m[2].length + 1, body: [m[3]] });
        i++;
        continue;
      }
      if (m && indentOf(m[1]) <= base + 1) break;   // l'autre sorte de liste
      if (!line.trim()) {
        // Une ligne vide ne clôt la liste que si rien ne la prolonge.
        let j = i + 1;
        while (j < lines.length && !lines[j].trim()) j++;
        const next = lines[j];
        if (next === undefined) break;
        const nm = next.match(ITEM);
        const sibling = nm && indentOf(nm[1]) <= base + 1
          && /\d/.test(nm[2]) === ordered;
        if (!sibling && indentOf(next) <= base) break;
        if (sibling) loose = true;
        items[items.length - 1].body.push("");
        i++;
        continue;
      }
      const item = items[items.length - 1];
      if (indentOf(line) > base) {
        const cut = Math.min(indentOf(line), item.width);
        item.body.push(line.replace(/\t/g, "    ").slice(cut));
        i++;
        continue;
      }
      if (startsBlock(line, lines[i + 1])) break;
      item.body.push(line.trim());   // continuation paresseuse
      i++;
    }
    const tag = ordered ? "ol" : "ul";
    const attr = ordered && start !== 1 ? ` start="${start}"` : "";
    const html = `<${tag}${attr}>` + items.map((it) => {
      const inner = blocks(it.body);
      // Liste serrée : un paragraphe seul se rend sans <p>.
      const tight = !loose && inner.length && inner[0].startsWith("<p>")
        ? inner[0].slice(3, -4) + inner.slice(1).join("") : inner.join("");
      return "<li>" + tight + "</li>";
    }).join("") + `</${tag}>`;
    return { html, next: i };
  }

  function render(text) {
    if (!text) return "";
    return blocks(String(text).replace(/\r\n?/g, "\n").split("\n")).join("");
  }

  return { render, escape: esc };
})();
