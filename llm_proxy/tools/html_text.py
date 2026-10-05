"""
HTML → texte lisible par un modèle, avec la seule bibliothèque standard
(le proxy n'a que fastapi, uvicorn et httpx en dépendances). Pas un
navigateur : pas de JavaScript, pas de mise en page. Ce qui est gardé —
titres, paragraphes, listes, liens, blocs de code — suffit pour lire une
documentation, un article ou une page de dépôt.
"""

import re
from html.parser import HTMLParser

# Contenu jamais montré au lecteur. `form` n'y est pas : des sites
# entiers (ASP.NET WebForms) tiennent dans un seul <form>, et la page
# sortait vide ; ses boutons et listes, eux, restent ignorés.
SKIP = {"script", "style", "noscript", "svg", "template", "iframe",
        "head", "button", "select", "nav", "footer"}
BLOCK = {"p", "div", "section", "article", "main", "header", "aside",
         "table", "tr", "ul", "ol", "dl", "dt", "dd", "blockquote",
         "figure", "figcaption", "hr", "br"}
HEADINGS = {"h1": "# ", "h2": "## ", "h3": "### ", "h4": "#### ",
            "h5": "##### ", "h6": "###### "}
VOID = {"br", "hr", "img", "input", "meta", "link", "source", "wbr", "area",
        "base", "col", "embed", "param", "track"}


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.title = ""
        self._skip = 0
        self._pre = 0
        self._in_title = False
        self._title_done = False
        self._href: str | None = None
        self._link: list[str] = []

    def _emit(self, text: str) -> None:
        (self._link if self._href is not None else self.out).append(text)

    def _close_link(self) -> None:
        """Rend le lien en cours : «libellé (URL)»."""
        if self._href is None:
            return
        label = "".join(self._link).strip()
        href, self._href = self._href, None
        if label and label != href:
            self.out.append(f"{label} ({href})")
        else:
            self.out.append(href)

    def _break_link(self, tag) -> None:
        """Un bloc DANS un lien sépare les mots du libellé (le saut de
        ligne, lui, part dans `out`) : sans quoi ils se collaient."""
        if self._href is not None and (
                tag in BLOCK or tag in HEADINGS or tag in ("li", "td", "th")):
            self._link.append(" ")

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            # Le premier seulement : un <svg><title>Icône</title> plus bas
            # ne s'ajoute pas au titre de la page.
            self._in_title = not self._title_done
            return
        if tag == "body":
            # </head> est facultatif en HTML : sans lui, <head> restait
            # « ouvert » et tout le corps de la page était ignoré.
            self._skip = 0
            return
        if tag in SKIP and tag not in VOID:
            self._skip += 1
            return
        if self._skip:
            return
        self._break_link(tag)
        if tag == "pre":
            self._pre += 1
            self.out.append("\n```\n")
        elif tag in HEADINGS:
            self.out.append("\n\n" + HEADINGS[tag])
        elif tag == "li":
            self.out.append("\n- ")
        elif tag in ("td", "th"):
            self.out.append(" | ")
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            # Un <a> jamais fermé retenait tout le texte qui suit : le
            # lien suivant (ou la fin du document) le ferme.
            self._close_link()
            if href.startswith(("http://", "https://")):
                self._href, self._link = href, []
        elif tag in BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._title_done = self._title_done or self._in_title
            self._in_title = False
            return
        if tag in SKIP:
            self._skip = max(self._skip - 1, 0)
            return
        if self._skip:
            return
        self._break_link(tag)
        if tag == "pre":
            self._pre = max(self._pre - 1, 0)
            self.out.append("\n```\n")
        elif tag == "a":
            self._close_link()
        elif tag in HEADINGS or tag in BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self._emit(data if self._pre else re.sub(r"\s+", " ", data))


def html_to_text(html: str) -> tuple[str, str]:
    """(titre, texte). Les espaces sont resserrés, les lignes vides
    multiples réduites à une ; le contenu des <pre> est gardé tel quel."""
    parser = _Text()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass  # HTML tordu : on rend ce qui a été lu jusque-là.
    parser._close_link()
    lines, blank, fenced = [], True, False
    for line in "".join(parser.out).split("\n"):
        if line.strip() == "```":
            # Le saut de ligne qui précède </pre> n'est pas du contenu.
            while fenced and lines[-1] != "```" and not lines[-1].strip():
                lines.pop()
            fenced = not fenced
            lines.append("```")
            blank = False
            continue
        if fenced:  # <pre> : tel quel, lignes vides comprises
            if not line.strip() and lines[-1] == "```":
                continue  # ni celui qui suit <pre>
            lines.append(line)
            blank = False
            continue
        line = re.sub(r" {2,}", " ", line.strip())
        if not line:
            if not blank:
                lines.append("")
            blank = True
            continue
        lines.append(line)
        blank = False
    return re.sub(r"\s+", " ", parser.title).strip(), "\n".join(lines).strip()
