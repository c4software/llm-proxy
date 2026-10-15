"""
`memory` : une mémoire PERSISTANTE pour le modèle, faite de fichiers texte
sous `/memories`, rangés par client (voir memories.py). C'est la forme de
l'outil `memory` d'Anthropic — mêmes commandes, mêmes arguments :

  view         {path, view_range?}   un répertoire : ses fichiers ; un
                                     fichier : son contenu, lignes numérotées
  create       {path, file_text}     crée ou remplace un fichier
  str_replace  {path, old_str, new_str}   remplace UNE occurrence exacte
  insert       {path, insert_line, insert_text}   insère après la ligne N
                                     (0 = au début)
  delete       {path}                un fichier, ou un répertoire entier
  rename       {old_path, new_path}  renomme ou déplace

À ne pas confondre avec les mémoires de CONVERSATION (tools.Memory,
chat_api) : celle-ci survit aux conversations et au redémarrage, et c'est
le modèle qui décide de ce qu'il y range. L'utilisateur la voit et la
corrige dans le tableau de bord (/ui, section Mémoire).

Le client est `call.client` (tools.owner : le condensé STABLE de sa clé),
jamais un argument du modèle : il ne peut pas lire la mémoire d'un autre.
Sans liaison de protocole : exécutable par /v1/tools et le serveur MCP
(/mcp), présentable sur /v1/chat/completions (déclaré `{"type":
"memory"}`, ou nommé dans [chat].always). L'outil memory d'Anthropic, lui,
est exécuté par le CLIENT : la surface Messages n'a rien à remplacer.
"""

import asyncio
import time

from .. import config, memories
from .contract import Call, Result, Tool, ToolError

ENABLED = config.flag("tools.memory.enabled", False)
NAME = "memory"
# Lignes au plus rendues par `view` d'un fichier sans `view_range`.
VIEW_LINES = 2000
COMMANDS = ("view", "create", "str_replace", "insert", "delete", "rename")


def _date(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _numbered(text: str, first: int = 1) -> str:
    return "\n".join(f"{n:6}\t{line}" for n, line in
                     enumerate(text.split("\n"), first))


def _view(owner: str, args: dict) -> str:
    path = memories.normalize(args.get("path") or memories.ROOT, directory=True)
    content = None if path == memories.ROOT else memories.read(owner, path)
    if content is None:
        if not memories.is_dir(owner, path):
            raise memories.MemoryError_(f"{path} does not exist", "not_accessible")
        files = memories.list_files(owner, path)
        use = memories.usage(owner)
        head = (f"Directory {path}: {len(files)} file(s). Memory used: "
                f"{use['files']}/{use['max_files']} files, "
                f"{use['chars']}/{use['max_chars']} characters.")
        if not files:
            return head + "\n(empty: nothing remembered yet)"
        return head + "\n" + "\n".join(
            f"{f['chars']:>8} chars  {_date(f['updated'])}  {f['path']}"
            for f in files)
    lines = content.split("\n")
    first, last = 1, len(lines)
    rng = args.get("view_range")
    if rng is not None:
        if not (isinstance(rng, list) and len(rng) == 2
                and all(isinstance(x, int) and not isinstance(x, bool) for x in rng)):
            raise memories.MemoryError_("view_range must be [first, last], two integers")
        first, last = rng[0], (len(lines) if rng[1] == -1 else rng[1])
        if first < 1 or first > max(len(lines), 1) or last < first:
            raise memories.MemoryError_(
                f"view_range {rng} is outside the file (1-{len(lines)})")
        last = min(last, len(lines))
    elif len(lines) > VIEW_LINES:
        last = VIEW_LINES
    out = _numbered("\n".join(lines[first - 1:last]), first)
    note = (f"\n[lines {first}-{last} of {len(lines)}; pass view_range for more]"
            if last < len(lines) or first > 1 else "")
    return f"{path}:\n{out}{note}"


def _existing(owner: str, args: dict) -> tuple[str, str]:
    path = memories.normalize(args.get("path"))
    content = memories.read(owner, path)
    if content is None:
        raise memories.MemoryError_(f"{path} does not exist", "not_accessible")
    return path, content


def _create(owner: str, args: dict) -> str:
    text = args.get("file_text")
    if not isinstance(text, str):
        raise memories.MemoryError_("`file_text` is required")
    path = memories.normalize(args.get("path"))
    existed = memories.write(owner, path, text)
    return f"File {'overwritten' if existed else 'created'}: {path}"


def _str_replace(owner: str, args: dict) -> str:
    old, new = args.get("old_str"), args.get("new_str", "")
    if not isinstance(old, str) or not old:
        raise memories.MemoryError_("`old_str` is required")
    if not isinstance(new, str):
        raise memories.MemoryError_("`new_str` must be text")
    path, content = _existing(owner, args)
    n = content.count(old)
    if n == 0:
        raise memories.MemoryError_(
            f"`old_str` was not found in {path}; view the file first")
    if n > 1:
        raise memories.MemoryError_(
            f"`old_str` appears {n} times in {path}; include more context so "
            f"it is unique")
    memories.write(owner, path, content.replace(old, new, 1))
    return f"File edited: {path}"


def _insert(owner: str, args: dict) -> str:
    line, text = args.get("insert_line"), args.get("insert_text")
    if not isinstance(line, int) or isinstance(line, bool):
        raise memories.MemoryError_("`insert_line` must be an integer")
    if not isinstance(text, str):
        raise memories.MemoryError_("`insert_text` is required")
    path, content = _existing(owner, args)
    lines = content.split("\n")
    if line < 0 or line > len(lines):
        raise memories.MemoryError_(
            f"insert_line {line} is outside the file (0-{len(lines)})")
    lines[line:line] = text.split("\n")
    memories.write(owner, path, "\n".join(lines))
    return f"Text inserted after line {line}: {path}"


def _delete(owner: str, args: dict) -> str:
    path = memories.normalize(args.get("path"), directory=True)
    if path == memories.ROOT:
        raise memories.MemoryError_(
            f"{memories.ROOT} itself cannot be deleted; delete its files")
    n = memories.delete(owner, path)
    if not n:
        raise memories.MemoryError_(f"{path} does not exist", "not_accessible")
    return f"Deleted: {path}" + (f" ({n} files)" if n > 1 else "")


def _rename(owner: str, args: dict) -> str:
    n = memories.rename(owner, args.get("old_path"), args.get("new_path"))
    return (f"Renamed: {memories.normalize(args.get('old_path'))} → "
            f"{memories.normalize(args.get('new_path'))}"
            + (f" ({n} files)" if n > 1 else ""))


_RUN = {"view": _view, "create": _create, "str_replace": _str_replace,
        "insert": _insert, "delete": _delete, "rename": _rename}


def execute(owner: str, args: dict) -> str:
    """La commande, BLOQUANTE (SQLite) : à appeler dans un fil. Lève
    ToolError."""
    command = _RUN.get(args.get("command"))
    if command is None:
        raise ToolError("invalid_input", (
            f"unknown command {args.get('command')!r}; one of "
            f"{', '.join(COMMANDS)}."))
    try:
        return command(owner, args)
    except memories.MemoryError_ as exc:
        raise ToolError(exc.code, f"{exc}.")


class MemoryTool(Tool):
    name = NAME

    @property
    def enabled(self) -> bool:
        return ENABLED

    def prompt(self, present) -> str:
        return (
            "Persistent memory that survives across conversations: text "
            "files under /memories, private to the user. At the start of a "
            "task, view /memories to recall earlier progress, decisions and "
            "preferences. Record what is worth keeping (facts about the user "
            "or the project, decisions, progress on long tasks) as you go, "
            "keep it organized and up to date, and delete what is obsolete. "
            "Never store secrets. Commands: view (a directory or a file, "
            "with optional view_range [first, last]), create (path, "
            "file_text: creates or overwrites), str_replace (path, old_str, "
            "new_str: old_str must appear exactly once), insert (path, "
            "insert_line, insert_text: after line N, 0 = at the top), delete "
            "(path: a file or a directory), rename (old_path, new_path).")

    def parameters(self, present) -> dict:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "enum": list(COMMANDS)},
                "path": {"type": "string",
                         "description": "A path under /memories, e.g. "
                                        "/memories/project-notes.md."},
                "view_range": {"type": "array", "items": {"type": "integer"},
                               "description": "view: [first, last] line, "
                                              "1-based (last = -1 for the end)."},
                "file_text": {"type": "string", "description": "create: the content."},
                "old_str": {"type": "string",
                            "description": "str_replace: the exact text to replace."},
                "new_str": {"type": "string", "description": "str_replace: its replacement."},
                "insert_line": {"type": "integer",
                                "description": "insert: the line after which "
                                               "to insert (0 = top)."},
                "insert_text": {"type": "string", "description": "insert: the text to insert."},
                "old_path": {"type": "string", "description": "rename: the current path."},
                "new_path": {"type": "string", "description": "rename: the new path."},
            },
            "required": ["command"],
        }

    def summary(self, args: dict, result: Result | None = None) -> dict:
        return {"type": NAME, "command": str(args.get("command") or ""),
                "path": str(args.get("path") or args.get("old_path") or "")}

    async def run(self, args: dict, call: Call) -> Result:
        return Result(await asyncio.to_thread(execute, call.client, args))


TOOL = MemoryTool()
