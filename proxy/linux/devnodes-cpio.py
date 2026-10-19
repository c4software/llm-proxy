#!/usr/bin/env python3
# Un fragment cpio (newc) avec /dev/console et /dev/null, ajouté à
# l'initramfs : `mknod` n'est pas permis dans un `docker build`.
import sys
def ent(name, mode, rmaj=0, rmin=0, ino=1):
    name_b = name.encode() + b"\0"
    hdr = "070701" + "".join("%08X" % v for v in (ino, mode, 0, 0, 1, 0, 0, 0, 0, rmaj, rmin, len(name_b), 0))
    out = hdr.encode() + name_b
    out += b"\0" * ((4 - len(out) % 4) % 4)
    return out
data = ent("dev", 0o040755, ino=900001) + ent("dev/console", 0o020600, 5, 1, 900002) + ent("dev/null", 0o020666, 1, 3, 900003) + ent("TRAILER!!!", 0, ino=0)
data += b"\0" * ((512 - len(data) % 512) % 512)
sys.stdout.buffer.write(data)
