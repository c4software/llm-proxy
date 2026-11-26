"""
Écrire un fichier d'un coup : un fichier temporaire dans le même dossier,
puis os.replace. Un lecteur voit l'ancien fichier ou le nouveau, jamais un
fichier à moitié écrit (core/config_file.py).
"""

import os
import tempfile


def write_text(path: str, text: str) -> None:
    """Écrit `text` dans `path`, atomiquement, en gardant ses droits."""
    folder = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(path)}-", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, os.stat(path).st_mode & 0o777)
        except FileNotFoundError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
