"""
Les BACS À SABLE de l'exécuteur : des conteneurs podman SANS ROOT, un par
session, où tourne le code écrit par un modèle.

Un bac = UN conteneur gardé en vie (`sleep infinity` sous un init) le
temps d'une session ; chaque exécution est un `podman exec` dedans, un
processus neuf. L'état gardé d'un appel au suivant est donc le système de
fichiers de /work (un tmpfs borné) — pas les variables d'un interpréteur.

Rien ne passe par un chemin de l'hôte : le code entre par l'entrée
standard, les fichiers produits sortent par un `tar` lu sur la sortie
standard. Aucun volume, aucun montage de l'extérieur.

Ce qui borne un bac, du plus sûr au moins sûr ICI (podman imbriqué dans
un conteneur Docker, sans systemd, donc souvent sans cgroups délégués) :
  * toujours : pas de réseau (--network none), racine en lecture seule,
    aucune capacité, no-new-privileges, un uid sans droit PROPRE au bac,
    /work et /tmp en tmpfs de taille fixe, RLIMIT_NPROC et RLIMIT_NOFILE,
    la durée d'une exécution (`timeout` DANS le bac, puis un délai de
    garde ici qui détruit le bac), la durée de vie du conteneur
    (--timeout, tenu par podman même si ce processus meurt), la taille de
    la sortie, le nombre et la taille des fichiers rendus ;
  * si les cgroups le permettent — `probe` l'ESSAIE au démarrage, drapeau
    par drapeau, plutôt que de le déduire : mémoire (--memory), CPU
    (--cpus), processus (--pids-limit). Un drapeau que podman refuse
    n'est plus posé, et l'état le dit (`Sandboxes.cgroup`) : le plafond
    du conteneur exécuteur lui-même (docker-compose : mem_limit, cpus,
    pids_limit) est alors le seul filet pour cette ressource.

Tout ce qui revient d'un bac (sortie, liste de fichiers, archive) est une
donnée HOSTILE : lue sous plafond, jamais interprétée.

Ce qu'un bac ne borne PAS : un programme peut y laisser des processus
derrière lui (`setsid`, que `timeout` ne rattrape pas) — ils tournent
jusqu'à la fin du bac, dans les mêmes bornes que lui, et, sous le même
uid, peuvent se mêler des appels suivants de CE bac (c'est pourquoi la
récolte ne croit rien de ce qu'elle lit). Ils ne sortent pas du bac.

Ce module ne connaît ni HTTP ni le proxy : server.py le sert.
"""

import asyncio
import io
import math
import os
import tarfile
import time
import uuid
from dataclasses import dataclass, field, fields

LABEL = "llm-proxy.sandbox"
WORKDIR = "/work"
STAMP = ".sbx_stamp"            # dans /work : repère « avant l'exécution »
# Premier uid des bacs, DANS l'espace d'utilisateurs de podman : le bac
# n° i tourne sous UID_BASE + i. Un uid par bac, pour que RLIMIT_NPROC
# (compté par uid) borne chaque bac à part, cgroups ou non.
UID_BASE = 20000



def _compiled(ext: str, build: str) -> tuple:
    """Un langage COMPILÉ : le source arrive sur l'entrée standard, est
    écrit dans $d (sous /tmp : ni le source ni le binaire ne sont des
    fichiers produits), compilé, puis exécuté depuis le dossier de
    travail. Compilation et exécution partagent le délai de l'appel. Une
    erreur de compilation est le code de sortie du compilateur, et sa
    sortie. Le dossier est refait à chaque appel : /tmp est petit."""
    return ("sh", "-c",
            f'd="${{TMPDIR:-/tmp}}/.build/{ext}" && rm -rf "$d" '
            f'&& mkdir -p "$d" && cat > "$d/main.{ext}" && {build} '
            f'&& exec "$d/main"')


# langage → la commande qui lit le programme sur son entrée standard
LANGS = {
    "python": ("python3", "-"),
    "bash": ("bash", "-s"),
    "sh": ("sh", "-s"),
    "node": ("node", "-"),
    "c": _compiled("c", 'gcc -O1 -o "$d/main" "$d/main.c" -lm'),
    "cpp": _compiled("cpp", 'g++ -std=c++20 -O1 -o "$d/main" "$d/main.cpp"'),
    # Un seul fichier, sans module : la bibliothèque standard seulement.
    "go": _compiled("go", '(cd "$d" && go build -o main main.go)'),
    "rust": _compiled("rs", 'rustc --edition 2021 -o "$d/main" "$d/main.rs"'),
}


@dataclass(frozen=True)
class Limits:
    """Les bornes, toutes réglables par l'environnement de l'exécuteur :
    SANDBOX_<NOM> (SANDBOX_MEMORY=1g, SANDBOX_MAX_SESSIONS=16…)."""
    cpus: float = 1.0
    memory: str = "512m"
    pids: int = 128
    timeout: float = 120.0          # une exécution, AU PLUS (s) : l'appelant
                                    # demande le sien, ramené sous celui-ci
    lifetime: int = 4 * 3600        # vie maximale d'un bac (s)
    idle: float = 1800.0            # bac sans appel → détruit (s)
    max_output: int = 20_000        # octets de sortie gardés (début et fin)
    work_size: str = "256m"         # tmpfs /work
    tmp_size: str = "128m"          # tmpfs /tmp (caches, compilations)
    max_files: int = 8              # fichiers rendus par exécution
    max_file_bytes: int = 5_000_000
    max_total_bytes: int = 10_000_000
    max_sessions: int = 8           # bacs ouverts, tous clients confondus
    max_sessions_per_client: int = 4

    @classmethod
    def from_env(cls, env=os.environ) -> "Limits":
        values = {}
        for f in fields(cls):
            raw = env.get("SANDBOX_" + f.name.upper(), "").strip()
            if raw:
                try:
                    values[f.name] = type(f.default)(raw)
                except ValueError:
                    raise SystemExit(f"SANDBOX_{f.name.upper()} : valeur "
                                     f"illisible ({raw!r})")
        return cls(**values)


@dataclass
class File:
    name: str                       # chemin relatif à /work
    data: bytes


@dataclass
class Outcome:
    output: str                     # stdout + stderr mêlés, bornés
    exit_code: int | None           # None : tué par le délai
    timed_out: bool = False
    truncated: bool = False         # la sortie a été coupée
    fresh: bool = False             # le bac vient d'être (re)créé
    reset: bool = False             # le bac n'a pas survécu à l'exécution
    files: list[File] = field(default_factory=list)
    # fichiers non rendus : {"name", "reason"}
    skipped: list[dict] = field(default_factory=list)
    seconds: float = 0.0


class SandboxError(Exception):
    """Le bac à sable lui-même est en panne (podman absent ou refusé,
    image manquante) — pas une erreur du code exécuté."""


class Busy(SandboxError):
    """Plus de place : tous les bacs ouverts sont en train d'exécuter."""


@dataclass
class _Session:
    slot: int                       # → son uid (UID_BASE + slot)
    created: float
    used: float
    name: str = ""                  # le conteneur ; «» tant qu'il n'existe pas
    dead: bool = False              # sa création a échoué
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _tail(raw: bytes, chars: int = 400) -> str:
    return raw.decode("utf-8", "replace").strip()[-chars:]


class Sandboxes:
    """Les bacs ouverts, par (client, session).

    `rootfs` : un système de fichiers DÉPLIÉ, lu par `podman run
    --rootfs` — celui que l'image de l'exécuteur embarque (Dockerfile) :
    aucune image à tirer ni à charger, donc aucun réseau au démarrage.
    `image` : à la place, une image du magasin local de podman
    (`--pull never`). L'un des deux."""

    def __init__(self, rootfs: str = "", image: str = "",
                 limits: Limits = Limits(), podman=("podman",),
                 uid_base: int = UID_BASE):
        self.rootfs, self.image, self.limits = rootfs, image, limits
        self.podman = tuple(podman)
        # Premier uid de SES bacs : deux jeux de bacs sur le même podman
        # (le serveur, et validate.py à côté de lui) ne partagent pas
        # d'uid, donc pas de compte RLIMIT_NPROC.
        self.uid_base = uid_base
        # Les bornes de cgroups que podman accepte ici : posées par
        # probe(). Vide tant qu'il n'a pas tourné — aucune n'est demandée.
        self.cgroup: frozenset[str] = frozenset()
        # (client, session) → bac. Le client fait partie de la CLÉ : un
        # identifiant de session présenté par un autre client ne trouve
        # rien, comme tools.Memory du proxy.
        self._sessions: dict[tuple[str, str], _Session] = {}

    # ── podman ──────────────────────────────────────────────────────────
    async def _run(self, *args, stdin: bytes = b"", timeout: float,
                   cap: int = 1_000_000, ends: bool = False):
        """Lance `podman args`. Rend (code, sortie, octets écartés) : la
        sortie est bornée à `cap` octets — le début seul, ou, avec `ends`,
        le début ET la fin (une trace d'erreur est à la fin). Le surplus
        est lu et jeté : le processus ne se bloque pas sur un tube plein.
        Avec `ends`, la sortie est le couple (début, fin).
        Lève TimeoutError au-delà de `timeout`."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.podman, *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT)
        except OSError as exc:
            raise SandboxError(f"podman introuvable : {exc}") from exc

        async def feed():
            try:
                proc.stdin.write(stdin)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                proc.stdin.close()

        async def read():
            first = cap - cap // 2 if ends else cap
            head, tail, size = bytearray(), bytearray(), 0
            while chunk := await proc.stdout.read(65536):
                size += len(chunk)
                room = first - len(head)
                if room > 0:
                    head += chunk[:room]
                    chunk = chunk[room:]
                if ends and chunk:
                    tail += chunk
                    del tail[:max(len(tail) - cap // 2, 0)]
            lost = size - len(head) - len(tail)
            return ((bytes(head), bytes(tail)) if ends else bytes(head)), lost

        try:
            async with asyncio.timeout(timeout):
                _, (out, lost) = await asyncio.gather(feed(), read())
                return await proc.wait(), out, lost
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    def _source(self) -> tuple:
        return ("--rootfs", self.rootfs) if self.rootfs \
            else ("--pull", "never", self.image)

    def _bounds(self, names) -> list[str]:
        """Les drapeaux de cgroups de ces ressources."""
        lim = self.limits
        flags = {
            "memory": ("--memory", lim.memory, "--memory-swap", lim.memory),
            "cpu": ("--cpus", str(lim.cpus)),
            "pids": ("--pids-limit", str(lim.pids)),
        }
        return [arg for name in sorted(names) for arg in flags[name]]

    async def probe(self) -> frozenset:
        """Au démarrage : ce que podman sait tenir ICI, par l'ESSAI. Un
        conteneur jetable sans aucune borne d'abord (podman démarre-t-il
        seulement ?), puis avec les trois bornes de cgroups, puis — si
        podman les refuse en bloc — une à une. `podman info` ne suffit
        pas : dans un conteneur, il liste des contrôleurs que l'on ne peut
        pas écrire. Et un drapeau ACCEPTÉ ne suffit pas non plus : sans
        cgroup délégué podman le prend sans rien borner (vu sur Docker
        29, cgroup v2 : `--memory 512m` accepté, 1 Go alloué). Une borne
        n'est donc dite tenue que si le bac LIT, dans son propre cgroup,
        une autre valeur que sans le drapeau. Lève SandboxError si rien
        ne démarre."""
        files = {"memory": "memory.max", "cpu": "cpu.max", "pids": "pids.max"}
        read = ("sh", "-c", "".join(
            f"echo {f} $(cat /sys/fs/cgroup/{f} 2>/dev/null);"
            for f in files.values()))

        async def trial(names) -> tuple[bool, bytes]:
            try:
                code, out, _ = await self._run(
                    "run", "--rm", "--network", "none", *self._bounds(names),
                    *self._source(), *read, timeout=90)
            except TimeoutError:
                return False, b"sans reponse apres 90 s"
            return code == 0, out

        def seen(out: bytes) -> dict:
            return dict(line.split(" ", 1) for line in
                        out.decode("utf-8", "replace").splitlines()
                        if " " in line and line.split(" ", 1)[0] in
                        files.values())

        ok, out = await trial(())
        if not ok:
            raise SandboxError("podman ne démarre aucun conteneur : "
                               + _tail(out))
        free = seen(out)
        held = set()
        every = tuple(files)
        ok, out = await trial(every)
        for names in ((every,) if ok else [(name,) for name in every]):
            if not ok:
                accepted, out = await trial(names)
                if not accepted:
                    continue
            bound = seen(out)
            held.update(name for name in names
                        if bound.get(files[name])
                        and bound[files[name]] != free.get(files[name]))
        self.cgroup = frozenset(held)
        return self.cgroup

    async def _create(self, slot: int) -> str:
        lim, name = self.limits, "sbx-" + uuid.uuid4().hex[:16]
        uid = str(self.uid_base + slot)
        # Fils d'exécution des bibliothèques et des compilateurs : sans
        # cgroup de CPU un bac VOIT tous les cœurs de l'hôte, et OpenBLAS
        # (numpy), Go ou cargo en lancent un par cœur — chacun compte dans
        # RLIMIT_NPROC, qu'un simple `import numpy` épuiserait sur une
        # machine à 64 cœurs.
        jobs = str(max(math.ceil(lim.cpus), 1))
        code, out, _ = await self._run(
            "run", "--detach", "--name", name, "--label", f"{LABEL}=1",
            "--network", "none",
            "--hostname", "sandbox",
            # Racine en lecture seule, et AUCUN tmpfs implicite : ceux que
            # podman monte de lui-même (/run, /var/tmp…) n'ont pas de
            # taille. Seuls /work et /tmp s'écrivent, bornés.
            "--read-only", "--read-only-tmpfs=false",
            # `exec` : podman monte un tmpfs en noexec, et un programme
            # compilé ici doit pouvoir tourner. Ce n'était pas une
            # barrière : un interpréteur exécute déjà ce qu'on lui écrit.
            "--tmpfs", f"{WORKDIR}:rw,exec,size={lim.work_size},mode=1777",
            "--tmpfs", f"/tmp:rw,exec,size={lim.tmp_size},mode=1777",
            "--shm-size", "16m",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--user", f"{uid}:{uid}",
            *self._bounds(self.cgroup),
            "--ulimit", f"nproc={lim.pids}:{lim.pids}",
            "--ulimit", "nofile=1024:1024", "--ulimit", "core=0",
            "--workdir", WORKDIR,
            # HOME hors de /work : les caches (matplotlib, npm…) ne sont
            # pas des fichiers produits.
            "--env", "HOME=/tmp", "--env", "MPLCONFIGDIR=/tmp/matplotlib",
            "--env", "MPLBACKEND=Agg",
            "--env", "PYTHONUNBUFFERED=1", "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--env", "LANG=C.UTF-8", "--env", "NO_COLOR=1",
            *(arg for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                               "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS",
                               "GOMAXPROCS", "CARGO_BUILD_JOBS")
              for arg in ("--env", f"{name}={jobs}")),
            # Go et cargo sans réseau : ni module ni crate à chercher, ni
            # chaîne d'outils à télécharger ; leurs caches dans /tmp.
            "--env", "GOCACHE=/tmp/.cache/go-build", "--env", "GOPATH=/tmp/go",
            "--env", "GOPROXY=off", "--env", "GOTOOLCHAIN=local",
            "--env", "GOFLAGS=-buildvcs=false",
            "--env", "CARGO_HOME=/tmp/.cargo",
            "--env", "CARGO_NET_OFFLINE=true",
            "--log-driver", "none",
            "--init", "--stop-timeout", "1",
            "--timeout", str(lim.lifetime),
            *self._source(), "sleep", "infinity",
            timeout=60)
        if code != 0:
            raise SandboxError("podman run a échoué : " + _tail(out))
        return name

    async def _alive(self, name: str) -> bool:
        try:
            code, out, _ = await self._run(
                "inspect", "--format", "{{.State.Running}}", name, timeout=15)
        except TimeoutError:
            return False
        return code == 0 and out.strip() == b"true"

    async def _remove(self, *names) -> None:
        try:
            await self._run("rm", "--force", "--time", "0", *names, timeout=30)
        except (TimeoutError, SandboxError):
            pass

    # ── sessions ────────────────────────────────────────────────────────
    def _expired(self, s: _Session, now: float) -> bool:
        # La marge d'une exécution sur la durée de vie : podman tue le
        # conteneur à `lifetime`, pas au milieu d'un appel.
        lim = self.limits
        return now - s.used > lim.idle \
            or now - s.created > lim.lifetime - lim.timeout

    async def _drop(self, key) -> bool:
        s = self._sessions.pop(key, None)
        if s is not None and s.name:
            await self._remove(s.name)
        return s is not None

    async def _make_room(self, client: str) -> bool:
        """De la place pour un bac de plus : les bacs expirés partent, puis
        — quota du client, puis quota commun — le bac le moins récemment
        utilisé qui n'exécute rien. Son propriétaire retrouvera un bac
        neuf, et le saura. False : tout est occupé."""
        await self.sweep()
        lim = self.limits

        def over():
            mine = [k for k in self._sessions if k[0] == client]
            # Le quota par client ne vaut que pour un client IDENTIFIÉ :
            # «» est le client unique d'un proxy ouvert, tout le monde.
            if client and len(mine) >= lim.max_sessions_per_client:
                return mine
            if len(self._sessions) >= lim.max_sessions:
                return list(self._sessions)
            return None

        while (keys := over()) is not None:
            idle = [k for k in keys if not self._sessions[k].lock.locked()]
            if not idle:
                return False
            await self._drop(min(idle, key=lambda k: self._sessions[k].used))
        return True

    async def _open(self, key) -> _Session:
        """Le bac de cette clé, RÉSERVÉ au besoin (son conteneur est créé
        par execute, sous son verrou)."""
        while True:
            s, now = self._sessions.get(key), time.monotonic()
            if s is not None and (s.lock.locked() or not self._expired(s, now)):
                return s
            if s is not None:
                await self._drop(key)
            elif not await self._make_room(key[0]):
                raise Busy("tous les bacs à sable sont occupés")
            elif key not in self._sessions:
                break       # rien n'a été créé pendant l'attente : à nous
        taken = {s.slot for s in self._sessions.values()}
        slot = next(i for i in range(len(taken) + 1) if i not in taken)
        s = self._sessions[key] = _Session(slot, now, now)
        return s

    async def sweep(self) -> int:
        """Détruit les bacs expirés (inactivité ou durée de vie). Appelé
        périodiquement par le serveur ; rend le nombre détruit."""
        now = time.monotonic()
        dead = [k for k, s in self._sessions.items()
                if not s.lock.locked() and self._expired(s, now)]
        for key in dead:
            await self._drop(key)
        return len(dead)

    async def destroy(self, client: str, session: str) -> bool:
        """Détruit le bac de cette session, même en pleine exécution."""
        return await self._drop((client, session))

    async def close(self) -> None:
        for key in list(self._sessions):
            await self._drop(key)

    async def reap_orphans(self) -> int:
        """Au démarrage : les conteneurs à notre étiquette laissés par un
        processus précédent."""
        try:
            code, out, _ = await self._run(
                "ps", "--all", "--filter", f"label={LABEL}=1",
                "--format", "{{.Names}}", timeout=30)
        except TimeoutError:
            return 0
        names = out.decode("utf-8", "replace").split() if code == 0 else []
        known = {s.name for s in self._sessions.values()}
        names = [n for n in names if n not in known]
        if names:
            await self._remove(*names)
        return len(names)

    def __len__(self) -> int:
        return len(self._sessions)

    # ── exécution ───────────────────────────────────────────────────────
    async def execute(self, client: str, session: str, language: str,
                      code: str, timeout: float | None = None) -> Outcome:
        """Exécute `code` dans le bac de (client, session), créé au
        besoin. `session` vide : un bac d'UN appel, détruit aussitôt.
        `timeout` : le délai demandé, ramené sous celui des bornes.
        Lève ValueError (langage inconnu), Busy, SandboxError."""
        if language not in LANGS:
            raise ValueError(f"langage inconnu : {language}")
        lim = self.limits
        limit = lim.timeout if timeout is None \
            else min(max(float(timeout), 1.0), lim.timeout)
        once = not session
        key = (client, "~" + uuid.uuid4().hex if once else session)
        while True:
            s = await self._open(key)
            await s.lock.acquire()  # une exécution à la fois par session
            if self._sessions.get(key) is s:
                break
            # Détruit pendant l'attente du verrou (place à faire, bac
            # perdu par l'exécution d'avant) : on repart d'un bac neuf
            # plutôt que d'exécuter dans un conteneur qui n'existe plus.
            s.lock.release()
        try:
            if s.dead:
                raise SandboxError("le bac à sable n'a pas pu être créé")
            started = time.monotonic()
            fresh = not s.name
            s.used = started
            try:
                if fresh:
                    await self._boot(key, s)
                out = await self._exec(s, language, code, limit)
                if out.exit_code == 125 and not fresh \
                        and self._sessions.get(key) is s \
                        and not await self._alive(s.name):
                    # 125 : podman n'a rien lancé, le conteneur a
                    # disparu (durée de vie, exécuteur relancé). Un
                    # bac neuf, et le programme une fois de plus.
                    await self._remove(s.name)
                    s.name, fresh = "", True
                    await self._boot(key, s)
                    out = await self._exec(s, language, code, limit)
            except TimeoutError:
                # Le `timeout` du bac n'a pas suffi (ou podman ne
                # répond plus) : on détruit, l'état est perdu.
                out = Outcome("", None, timed_out=True, reset=True)
            if (out.reset or out.exit_code in (125, 137)) \
                    and not (s.name and await self._alive(s.name)):
                out.reset = True
            if out.reset and self._sessions.get(key) is s:
                await self._drop(key)
            out.fresh, out.seconds = fresh, time.monotonic() - started
            s.used = time.monotonic()
            return out
        finally:
            s.lock.release()
            if once and self._sessions.get(key) is s:
                await self._drop(key)

    async def _boot(self, key, s: _Session) -> None:
        try:
            s.name = await self._create(s.slot)
            s.created = time.monotonic()
        except BaseException as exc:
            s.dead = True
            if self._sessions.get(key) is s:
                del self._sessions[key]
            if isinstance(exc, TimeoutError):
                raise SandboxError("podman run sans réponse") from exc
            raise

    async def _exec(self, s: _Session, language: str, code: str,
                    limit: float) -> Outcome:
        lim = self.limits
        t = max(int(limit), 1)
        started = time.monotonic()
        # Le repère, puis le programme sous `timeout` : TERM à t, KILL 2 s
        # plus tard. Le programme arrive sur l'entrée standard. Les 20 ms
        # entre les deux : l'horloge des fichiers avance par crans de
        # quelques millisecondes, et un fichier écrit dans le cran du
        # repère ne serait pas « plus récent » que lui.
        rc, (head, tail), lost = await self._run(
            "exec", "--interactive", "--workdir", WORKDIR, s.name,
            "sh", "-c",
            f'touch {STAMP} && sleep 0.02 && exec timeout -k 2 {t} "$@"', "sh",
            *LANGS[language],
            stdin=code.encode("utf-8", "replace"),
            timeout=t + 10, cap=lim.max_output, ends=True)
        # 124 : `timeout` a tué au TERM ; 137 après le délai : au KILL.
        # Un 137 AVANT le délai est un autre tueur (mémoire) : un code de
        # sortie comme un autre, que l'appelant commente.
        timed_out = rc == 124 or (rc == 137 and time.monotonic() - started >= t)
        text = head.decode("utf-8", "replace")
        if lost:
            text += f"\n[… {lost} bytes of output omitted …]\n"
        text += tail.decode("utf-8", "replace")
        out = Outcome(text, None if timed_out else rc, timed_out=timed_out,
                      truncated=bool(lost))
        if rc != 125:
            out.files, out.skipped = await self._harvest(s)
        return out

    async def _harvest(self, s: _Session):
        """Les fichiers de /work créés ou modifiés par l'exécution :
        liste, tri sous les plafonds, puis UNE archive tar lue sous
        plafond. Les fichiers et dossiers cachés (caches, le repère), les
        `__pycache__` et les `node_modules` ne sont pas des fichiers
        produits. « Modifié » se lit au ctime (-cnewer), que le programme
        ne peut pas antidater : un fichier sorti d'une archive avec sa
        vieille date compte. Rend (fichiers, écartés)."""
        lim = self.limits
        try:
            rc, raw, lost = await self._run(
                "exec", "--workdir", WORKDIR, s.name,
                "find", ".", "-xdev",
                "(", "-name", ".*", "-o", "-name", "__pycache__",
                "-o", "-name", "node_modules", ")", "!", "-name", ".", "-prune",
                "-o", "-type", "f", "-cnewer", STAMP, "-printf", r"%s %P\0",
                timeout=15, cap=200_000)
        except TimeoutError:
            return [], []
        if rc != 0:
            return [], []
        if lost:
            return [], [{"name": "*", "reason": "too many files changed"}]
        wanted, skipped, total = [], [], 0
        found = (entry.partition(" ") for entry in
                 raw.decode("utf-8", "surrogateescape").split("\0"))
        for name, size in sorted((name, size) for size, _, name in found):
            if not name or not size.isdigit():
                continue
            # Un nom qui n'est pas de l'UTF-8 ne se rend pas : il ne
            # tiendrait ni dans du JSON ni dans une URL.
            try:
                name.encode("utf-8")
            except UnicodeEncodeError:
                skipped.append({"name": name.encode(
                    "utf-8", "surrogateescape").decode("utf-8", "replace"),
                    "reason": "unreadable name"})
            else:
                if int(size) > lim.max_file_bytes:
                    skipped.append({"name": name, "reason": "too large"})
                elif len(wanted) >= lim.max_files:
                    skipped.append({"name": name, "reason": "too many files"})
                elif total + int(size) > lim.max_total_bytes:
                    skipped.append({"name": name, "reason": "total too large"})
                else:
                    wanted.append(name)
                    total += int(size)
        files = []
        if wanted:
            try:
                # «./» devant chaque nom : aucun ne se lit comme une option.
                rc, raw, lost = await self._run(
                    "exec", "--interactive", "--workdir", WORKDIR, s.name,
                    "tar", "-cf", "-", "--null", "--no-recursion",
                    "--hard-dereference", "-T", "-",
                    stdin=b"".join(b"./" + n.encode() + b"\0" for n in wanted),
                    timeout=30, cap=lim.max_total_bytes + 1_000_000)
            except TimeoutError:
                rc, raw, lost = 1, b"", 0
            if rc == 0 and not lost:
                ask = set(wanted)
                try:
                    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tar:
                        for m in tar:
                            # Fichiers ordinaires DEMANDÉS seulement, à la
                            # taille plafonnée : ni lien, ni périphérique,
                            # ni nom ajouté entre la liste et l'archive.
                            name = m.name.removeprefix("./")
                            if m.isreg() and name in ask \
                                    and m.size <= lim.max_file_bytes:
                                ask.discard(name)
                                files.append(
                                    File(name, tar.extractfile(m).read()))
                except (tarfile.TarError, EOFError, OSError):
                    pass
            got = {f.name for f in files}
            skipped += [{"name": n, "reason": "could not be read"}
                        for n in wanted if n not in got]
        return files, skipped[:50]
