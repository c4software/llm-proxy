"""
Le script que l'outil `repository` (repository.py) fait tourner DANS UN BAC
de l'exécuteur : tout ce qui touche au contenu d'un dépôt — décompression,
arborescence, lecture, recherche — se passe là, sans réseau, hors du
proxy. Le proxy n'en envoie que le texte, précédé de ses arguments :

    ARGS = '<json>'        (repository._code)

  ARGS : {"dir": nom du dépôt dans le bac, "command": tree | read | grep,
          "archive": nom du fichier d'entrée à décompresser d'abord (ou
          absent), "path", "depth", "offset", "limit", "pattern",
          "include", et les bornes "max_unpacked", "max_file",
          "max_entries", "keep"}

Sortie : le texte rendu au modèle. Code de sortie : 0 rendu ; 2 erreur
(« code: message », un code du contrat des outils) ; 3 le dépôt n'est pas
dans ce bac (neuf, ou purgé) : le proxy le redépose avec `archive`.

Le dépôt vit sous .repos/<dir>/ (un dossier caché : l'exécuteur ne le
renvoie pas à la récolte), avec .index.json : chaque chemin et sa taille,
-1 pour un fichier listé mais non gardé (plus gros que max_file). Seuls
les fichiers réguliers sont écrits, chacun sous le dossier du dépôt ; liens,
chemins absolus et `..` sont ignorés. Le bac garde les `keep` dépôts les
plus récents, pour tenir dans /work.

Ce module n'importe rien du proxy : il est lu comme un texte, et exécuté
tel quel par le Python du bac.
"""

import fnmatch
import json
import os
import re
import shutil
import signal
import sys
import tarfile
import time

ROOT = ".repos"
READ_CHARS = 15_000
LINE_CHARS = 2000
TREE_LINES = 300
GREP_MATCHES = 60
GREP_LINE = 300
GREP_FILE_SECONDS = 1.0
GREP_BUDGET = 10.0


class Fail(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _extract(base: str, archive: str, a: dict) -> None:
    """L'archive (UN dossier racine) → ses fichiers réguliers sous `base`."""
    tmp = base + ".part"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    index, total, entries = {}, 0, 0
    try:
        with tarfile.open(archive, mode="r:*") as tar:
            for m in tar:
                entries += 1
                if entries > a["max_entries"]:
                    raise Fail("unsupported", f"the project has more than "
                                              f"{a['max_entries']} entries.")
                total += m.size                   # gardé ou non : décompressé
                if total > a["max_unpacked"]:
                    raise Fail("unsupported", f"the project is larger than "
                                              f"{a['max_unpacked']} bytes once unpacked.")
                parts = m.name.split("/")[1:]
                if not m.isfile() or not parts or any(p in ("", ".", "..") for p in parts):
                    continue
                path = "/".join(parts)
                if m.size > a["max_file"]:
                    index[path] = -1
                    continue
                target = os.path.join(tmp, *parts)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                src = tar.extractfile(m)
                with open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                index[path] = m.size
    except (tarfile.TarError, EOFError, OSError) as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        if isinstance(exc, OSError) and exc.errno == 28:
            raise Fail("unsupported", "the project does not fit in the sandbox.")
        raise Fail("unsupported", "unreadable archive.")
    except Fail:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    finally:
        try:
            os.remove(archive)
        except OSError:
            pass
    with open(os.path.join(tmp, ".index.json"), "w") as fh:
        json.dump(index, fh)
    shutil.rmtree(base, ignore_errors=True)
    os.rename(tmp, base)


def _prune(keep: int, current: str) -> None:
    """Les `keep` dépôts les plus récents restent (l'actuel compris)."""
    others = sorted((d for d in os.listdir(ROOT) if d != current and not d.endswith(".part")),
                    key=lambda d: os.path.getmtime(os.path.join(ROOT, d)), reverse=True)
    for d in others[max(keep - 1, 0):]:
        shutil.rmtree(os.path.join(ROOT, d), ignore_errors=True)


def _index(base: str) -> dict:
    with open(os.path.join(base, ".index.json")) as fh:
        return json.load(fh)


def _text(base: str, path: str, size: int) -> str | None:
    if size < 0:
        return None
    with open(os.path.join(base, *path.split("/")), "rb") as fh:
        data = fh.read()
    if b"\0" in data[:8000]:
        return None
    return data.decode("utf-8", "replace")


def _under(path: str, folder: str) -> bool:
    return not folder or path == folder or path.startswith(folder + "/")


def _size(n: int, max_file: int) -> str:
    if n < 0:
        return f">{max_file // 1_000_000} MB, not kept"
    return f"{n} B" if n < 1000 else f"{n / 1000:.1f} kB" if n < 1_000_000 else f"{n / 1e6:.1f} MB"


def tree(index: dict, folder: str, depth: int, max_file: int) -> str:
    entries = sorted(p for p in index if _under(p, folder))
    if not entries:
        raise Fail("not_accessible", f"no file under {folder!r}.")
    start = len(folder.split("/")) if folder else 0
    out, counts = [], {}
    for p in entries:
        parts = p.split("/")
        if len(parts) - start > depth:
            d = "/".join(parts[:start + depth])
            if d not in counts:
                counts[d] = 0
                out.append(d)
            counts[d] += 1
        else:
            out.append(f"{p}  ({_size(index[p], max_file)})")
    out = [f"{x}/  ({counts[x]} files)" if x in counts else x for x in out]
    more = len(out) - TREE_LINES
    return f"{len(entries)} files\n" + "\n".join(out[:TREE_LINES]) + (
        f"\n[… {more} more entries; narrow with `path` or `depth`]" if more > 0 else "")


def lines_of(text: str) -> list[str]:
    """Découpé sur \\n seul (grep compte ainsi), sans la ligne vide qui suit
    le dernier saut."""
    lines = [line.rstrip("\r") for line in text.split("\n")]
    if lines and lines[-1] == "" and text.endswith("\n"):
        lines.pop()
    return lines


def read(base: str, index: dict, path: str, offset: int, limit: int, max_file: int) -> str:
    if path not in index:
        name = path.split("/")[-1]
        near = [p for p in index if p.split("/")[-1] == name]
        raise Fail("not_accessible", f"no file {path!r}" + (
            f"; did you mean: {', '.join(near[:5])}?" if near else "; use the tree command."))
    text = _text(base, path, index[path])
    if text is None:
        raise Fail("unsupported", f"{path} is binary or larger than {max_file} bytes.")
    lines = lines_of(text)
    first = max(1, offset)
    if lines and first > len(lines):
        raise Fail("invalid_input", f"{path} has {len(lines)} lines; `offset` is past its end.")
    body, used = [], 0
    for i, line in enumerate(lines[first - 1:first - 1 + limit], first):
        if len(line) > LINE_CHARS:                 # JS minifié, lockfile : coupé
            line = line[:LINE_CHARS] + f" [… {len(line) - LINE_CHARS} characters]"
        row = f"{i:>5}\t{line}"
        if body and used + len(row) > READ_CHARS:
            break                                  # au moins une ligne : on avance toujours
        used += len(row) + 1
        body.append(row)
    last = first + len(body) - 1
    tail = f"\n[lines {first}-{last} of {len(lines)}; continue with offset={last + 1}]" \
        if last < len(lines) else ""
    return f"{path} ({len(lines)} lines)\n" + "\n".join(body) + tail


def _clip(line: str, start: int, end: int) -> str:
    if len(line) <= GREP_LINE:
        return line
    margin = max((GREP_LINE - (end - start)) // 2, 0)
    a = max(start - margin, 0)
    b = min(a + GREP_LINE, len(line))
    a = max(b - GREP_LINE, 0)
    return ("…" if a else "") + line[a:b] + ("…" if b < len(line) else "")


class _Slow(Exception):
    pass


def _alarm(signum, frame):
    raise _Slow


def grep(base: str, index: dict, pattern: str, folder: str, include: str) -> str:
    """Chaque ligne qui répond, une fois, coupée autour de la correspondance.
    Un fichier où la recherche dépasse GREP_FILE_SECONDS est sauté et compté ;
    toute la recherche est bornée par GREP_BUDGET."""
    try:
        rx = re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    except re.error as exc:
        raise Fail("invalid_input", f"invalid regular expression ({exc}).")
    signal.signal(signal.SIGALRM, _alarm)
    deadline = time.monotonic() + GREP_BUDGET
    found, skipped, slow = [], 0, 0
    for p in sorted(index):
        if not _under(p, folder) or (include and not fnmatch.fnmatch(p.split("/")[-1], include)
                                     and not fnmatch.fnmatch(p, include)):
            continue
        text = _text(base, p, index[p])
        if text is None:
            skipped += 1
            continue
        left = deadline - time.monotonic()
        if left <= 0:
            raise Fail("failed", "the pattern took too long on this project; use a simpler "
                                 "expression, or narrow with `path` or `include`.")
        signal.setitimer(signal.ITIMER_REAL, min(GREP_FILE_SECONDS, left))
        try:
            pos, number = 0, 1
            while pos < len(text) or (pos == 0 and not text):
                m = rx.search(text, pos)
                if m is None or (m.start() == len(text) and text.endswith("\n")):
                    break
                begin = text.rfind("\n", 0, m.start()) + 1
                end = text.find("\n", m.start())
                end = len(text) if end < 0 else end
                number += text.count("\n", pos, begin)
                line = text[begin:end].rstrip("\r")
                found.append(f"{p}:{number}: "
                             f"{_clip(line, m.start() - begin, m.end() - begin).strip()}")
                if len(found) >= GREP_MATCHES:
                    return "\n".join(found) + (f"\n[stopped at {GREP_MATCHES} matches; "
                                               f"narrow with `path` or `include`]")
                pos, number = end + 1, number + 1  # une ligne n'est rendue qu'une fois
        except _Slow:
            slow += 1
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
    too_slow = f"{slow} files where the pattern was too slow, not searched" if slow else ""
    if not found:
        notes = ([f"{skipped} binary or large files not searched"] if skipped else []) \
            + ([too_slow] if too_slow else [])
        return "No match." + (f" ({'; '.join(notes)}.)" if notes else "")
    return "\n".join(found) + (f"\n[{too_slow}]" if too_slow else "")


def main(a: dict) -> str:
    os.makedirs(ROOT, exist_ok=True)
    base = os.path.join(ROOT, a["dir"])
    if a.get("archive"):
        _extract(base, a["archive"], a)
        _prune(a["keep"], a["dir"])
    if not os.path.isfile(os.path.join(base, ".index.json")):
        sys.exit(3)
    os.utime(base)                                 # le plus récent, pour _prune
    index = _index(base)
    folder = str(a.get("path") or "").strip("/")
    if a["command"] == "tree":
        return tree(index, folder, a["depth"], a["max_file"])
    if a["command"] == "read":
        return read(base, index, folder, a["offset"], a["limit"], a["max_file"])
    return grep(base, index, a["pattern"], folder, a.get("include") or "")


if __name__ == "__main__":
    try:
        print(main(json.loads(ARGS)))              # noqa: F821 — posé par repository._code
    except Fail as exc:
        print(f"{exc.code}: {exc}")
        sys.exit(2)
