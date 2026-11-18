"""
Les DATA URL que la page envoie (images jointes, retouchées, audio) :
`data:<type>;base64,<données>`. Le type annoncé n'est pas cru : qui
reçoit les octets les reconnaît lui-même (files.sniff).
"""

import base64
import binascii
import re

DATA_URL = re.compile(r"data:([\w.+-]+/[\w.+-]+)?(?:;[\w=.+-]+)*;base64,([A-Za-z0-9+/=\s]+)\Z")


def decode(value) -> bytes | None:
    """Les octets d'une data URL en base64 ; None si `value` n'en est pas
    une, ou illisible."""
    found = DATA_URL.match(str(value or ""))
    if found is None:
        return None
    try:
        return base64.b64decode(found.group(2))
    except (binascii.Error, ValueError):
        return None
