"""L'outil hébergé `code_execution` (llm_proxy/tools/code_execution.py)
contre un FAUX exécuteur — httpx.MockTransport, aucun réseau, aucun
conteneur — et le magasin des fichiers rendus (llm_proxy/files.py). La
configuration est posée par monkeypatch sur les constantes des modules.

Ce qui est vérifié : ce que l'outil envoie, le texte qu'il écrit pour le
modèle, ses codes d'erreur, les fichiers qu'il rend ; les bornes du
magasin et les en-têtes sous lesquels un fichier est servi. L'exécuteur
lui-même est dans test_executor.py."""

import asyncio
import base64
import json

import conftest  # noqa: F401 — pose CONFIG_PATH avant tout import du paquet
import httpx
import pytest

from llm_proxy import files, tools
from llm_proxy.tools import code_execution

PNG = b"\x89PNG\r\n\x1a\n" + bytes(40)
TOOL = code_execution.TOOL


def go(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def reglages(monkeypatch):
    monkeypatch.setattr(code_execution, "ENABLED", True)
    monkeypatch.setattr(code_execution, "URL", "http://executor.test:8080")
    monkeypatch.setattr(code_execution, "TOKEN", "jeton")
    monkeypatch.setattr(code_execution, "TIMEOUT", 30)
    monkeypatch.setattr(code_execution, "MAX_CALLS", 8)
    monkeypatch.setattr(code_execution, "MAX_OUTPUT_CHARS", 12_000)
    monkeypatch.setattr(files, "PUBLIC_URL", "https://proxy.test")
    monkeypatch.setattr(files, "STORE", files.Store(3600, 10_000, 4_000))
    monkeypatch.setattr(tools, "MAX_RESULT_CHARS", 24_000)


def answer(**doc):
    """Une réponse 200 de l'exécuteur ; `files` : {nom: octets}."""
    return {"exit_code": 0, "timed_out": False, "output": "", "truncated": False,
            "fresh": False, "reset": False, "seconds": 0.1, "skipped": [],
            **doc, "files": [
                {"name": n, "size": len(d), "data": base64.b64encode(d).decode()}
                for n, d in doc.get("files", {}).items()]}


def executor(reply, seen=None):
    """Le faux exécuteur : `reply` est un document (200), un statut, ou
    une exception à lever ; `seen` reçoit les requêtes."""
    def handler(request):
        if seen is not None:
            seen.append(request)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, int):
            return httpx.Response(reply, json={"error": {
                "code": "x", "message": "détail de podman"}})
        return httpx.Response(200, content=reply) if isinstance(reply, bytes) \
            else httpx.Response(200, json=reply)
    return httpx.MockTransport(handler)


def rendu(args, reply=None, session="conv", seen=None) -> tools.Result:
    """Ce que tools.Hosted fait d'un `run` appelé à la main."""
    call = tools.Call(client="clientA", session=session, endpoint="/v1/tools")
    try:
        return go(TOOL.run(args, call, transport=executor(
            answer() if reply is None else reply, seen)))
    except tools.ToolError as exc:
        return tools.failure(exc.code, exc.message)


CODE = {"language": "python", "code": "print(6 * 7)"}


def test_contrat_de_l_outil():
    assert (TOOL.name, TOOL.kinds) == ("code_execution", ("code_execution",))
    assert TOOL.responses is None and TOOL.anthropic is None
    # Son délai couvre celui du programme et le travail de l'exécuteur ;
    # ses appels sont comptés à part.
    assert TOOL.timeout == 150 and TOOL.max_calls == 8 and TOOL.enabled
    fn = TOOL.spec({"code_execution"})["function"]
    assert fn["name"] == "code_execution"
    assert fn["parameters"]["required"] == ["language", "code"]
    assert fn["parameters"]["properties"]["language"]["enum"] == [
        "python", "bash", "javascript", "c", "cpp", "go", "rust"]
    assert "NO network" in fn["description"] and "30 s" in fn["description"]
    # Compilés : dit au modèle, avec ce que l'absence de réseau interdit.
    assert "no Go module, no Rust crate" in fn["description"]
    assert TOOL.summary({"language": "bash"}) == {
        "type": "code_execution", "language": "bash"}


def test_requete_et_resultat():
    seen = []
    r = rendu({"language": " JavaScript ", "code": "console.log(42)"}, answer(
        output="42\n", fresh=True,
        files={"out/graphique.png": PNG, "table.csv": b"a,b\n1,2\n"},
        skipped=[{"name": "gros.bin", "reason": "too large"}]), seen=seen)
    (request,) = seen
    assert str(request.url) == "http://executor.test:8080/v1/execute"
    assert request.headers["authorization"] == "Bearer jeton"
    assert json.loads(request.content) == {
        "client": "clientA", "session": "conv", "language": "node",
        "code": "console.log(42)", "timeout": 30}
    assert r.error is None
    assert r.text == (
        "Exit code: 0\n"
        "Sandbox: new and empty — no file from an earlier call exists here.\n"
        "Files delivered to the user (shown with your answer; do not write "
        "links or paths to them):\n"
        "- out/graphique.png (image/png, 48 bytes)\n"
        "- table.csv (text/csv, 8 bytes)\n"
        "Files NOT delivered to the user:\n"
        "- gros.bin: too large\n"
        "Output:\n42\n")
    # Les fichiers : un nom sans chemin, le type lu dans les OCTETS pour
    # une image, les octets.
    assert r.files == (
        tools.Artifact("graphique.png", "image/png", PNG),
        tools.Artifact("table.csv", "text/csv", b"a,b\n1,2\n"))
    assert r.meta == {"exit_code": 0, "timed_out": False, "fresh": True,
                      "files": ["out/graphique.png", "table.csv"]}
    # Aucune URL, aucun jeton dans ce que lit (et que garde) le modèle.
    assert "http" not in r.text
    # Les langages compilés partent sous le nom de l'exécuteur.
    for said, sent in (("C++", "cpp"), ("c", "c"), ("golang", "go"),
                       ("Rust", "rust")):
        rendu({"language": said, "code": "x"}, seen=seen)
        assert json.loads(seen[-1].content)["language"] == sent, said


def test_un_programme_en_echec_est_un_resultat():
    """Code de sortie, délai, bac neuf ou perdu : du TEXTE, jamais une
    erreur de l'outil."""
    cases = [
        (dict(exit_code=1, output="Traceback…\nValueError: non\n"), "conv",
         "Exit code: 1\nOutput:\nTraceback…\nValueError: non\n"),
        (dict(exit_code=137), "conv",
         "Exit code: 137 (killed — most likely out of memory)\nOutput:\n(no output)"),
        (dict(exit_code=None, timed_out=True, output="début\n"), "conv",
         "Timed out: the program was killed after 30 s.\nOutput:\ndébut\n"),
        (dict(exit_code=None, timed_out=True, reset=True), "conv",
         "Timed out: the program was killed after 30 s.\nSandbox: destroyed "
         "by this run — its files are gone, the next call starts in an "
         "empty one.\nOutput:\n(no output)"),
        # Sans session (appel direct) : le bac ne vit qu'un appel.
        (dict(fresh=True, output="1\n"), "",
         "Exit code: 0\nSandbox: single-use — nothing is kept after this "
         "call.\nOutput:\n1\n"),
    ]
    for doc, session, text in cases:
        r = rendu(CODE, answer(**doc), session=session)
        assert (r.error, r.text) == (None, text), doc
        assert r.files == ()


def test_sortie_longue_coupee_au_milieu(monkeypatch):
    monkeypatch.setattr(code_execution, "MAX_OUTPUT_CHARS", 100)
    r = rendu(CODE, answer(output="DEBUT" + "x" * 5000 + "FIN",
                           files={"a.txt": b"a"}))
    assert r.text.endswith("x" * 47 + "FIN") and "DEBUT" in r.text
    assert "[… 4908 characters omitted …]" in r.text
    # La liste des fichiers est AVANT la sortie : une coupe de l'exécuteur
    # du proxy (max_result_chars) ne l'emporte pas.
    assert r.text.index("- a.txt") < r.text.index("Output:")


def test_erreurs_de_l_outil():
    seen = []
    for args in ({}, {"code": "1"}, {"language": "cobol", "code": "1"},
                 {"language": 3, "code": "1"}, {"language": "python"},
                 {"language": "python", "code": "   "},
                 {"language": "python", "code": ["print(1)"]},
                 {"language": "python", "code": "x" * 200_001}):
        r = rendu(args, seen=seen)
        assert r.error == "invalid_input" and r.text.startswith("Error: "), args
    assert seen == []       # refusé avant tout appel
    for reply, code, said in (
            (httpx.ConnectError("refusé"), "unavailable", "unreachable (ConnectError)"),
            (httpx.ReadTimeout("long"), "unavailable", "unreachable (ReadTimeout)"),
            (429, "too_many_requests", "every sandbox is busy"),
            (503, "unavailable", "failed (HTTP 503)"),
            (401, "unavailable", "failed (HTTP 401)"),
            (b"<html>", "unavailable", "unreadable answer"),
            (b"[1, 2]", "unavailable", "unreadable answer"),
            ({"exit_code": "zéro"}, "unavailable", "unreadable answer")):
        r = rendu(CODE, reply)
        assert r.error == code and said in r.text, reply
        # Le détail de l'exécuteur reste dans le journal du proxy.
        assert "podman" not in r.text and r.files == ()


def test_non_configure(monkeypatch):
    seen = []
    for name in ("URL", "TOKEN"):
        with monkeypatch.context() as m:
            m.setattr(code_execution, name, "")
            r = rendu(CODE, seen=seen)
            assert r.error == "unavailable" and "not configured" in r.text
    assert seen == []


def test_fichier_que_le_magasin_ne_garderait_pas(monkeypatch):
    """Un fichier n'est annoncé comme rendu que s'il peut l'être."""
    reply = answer(files={"petit.txt": b"x" * 10, "gros.bin": b"0" * 5000})
    reply["files"].append({"name": "casse.bin", "size": 3, "data": "%%%"})
    reply["files"] += [{"name": 3, "data": ""}, "x"]
    r = rendu(CODE, reply)
    assert [a.name for a in r.files] == ["petit.txt"]
    assert "- gros.bin: larger than 4000 bytes" in r.text
    assert "- casse.bin: could not be read" in r.text
    # Sans adresse publique, aucun lien ne peut être écrit : rien n'est rendu.
    monkeypatch.setattr(files, "PUBLIC_URL", "")
    r = rendu(CODE, reply)
    assert r.files == () and "Files delivered" not in r.text
    assert "- petit.txt: no public URL is configured on this proxy" in r.text


def test_par_l_executeur_du_proxy(monkeypatch):
    """Par tools.Hosted.run, le chemin de toutes les surfaces : la
    session et le client arrivent à l'exécuteur, les fichiers en
    reviennent."""
    seen = []

    class Branche(code_execution.CodeExecution):
        async def run(self, args, call):
            return await super().run(args, call, transport=executor(
                answer(output="42\n", files={"a.png": PNG}), seen))

    hosted = tools.Hosted([Branche()], tools.Memory(4, 60))
    r = go(hosted.run("code_execution", json.dumps(CODE), 0,
                      endpoint="/v1/chat/completions", client="clientA",
                      session="conv-7"))
    assert r.error is None and [a.name for a in r.files] == ["a.png"]
    sent = json.loads(seen[0].content)
    assert (sent["client"], sent["session"]) == ("clientA", "conv-7")
    # Son propre plafond d'appels, pas celui des outils web.
    monkeypatch.setattr(code_execution, "MAX_CALLS", 2)
    r = go(hosted.run("code_execution", json.dumps(CODE), 2))
    assert r.error == "limit" and len(seen) == 1


# ── le magasin des fichiers rendus ──────────────────────────────────────

def test_magasin_jeton_et_bornes(monkeypatch):
    store = files.Store(ttl=60, max_bytes=100, max_file_bytes=60)
    a = store.put("out/rapport final.pdf", b"%PDF" + b"a" * 36, "clientA")
    b = store.put("b.bin", b"b" * 40)
    # Un jeton par fichier, imprévisible (192 bits), jamais le nom.
    assert a.token != b.token and len(a.token) == 32 and a.owner == "clientA"
    assert store.get(a.token, "rapport_final.pdf") is a
    # Jeton inconnu, nom d'un autre : la même absence.
    for token, name in ((a.token, "b.bin"), (a.token, ""), ("x" * 32, "b.bin"),
                        (b.token + "x", "b.bin")):
        assert store.get(token, name) is None
    assert (len(store), store.size) == (2, 80)
    # Par fichier : refusé. En tout : les plus anciens sortent.
    assert store.put("gros", b"0" * 61) is None
    c = store.put("c.bin", b"c" * 40)
    assert store.get(a.token, a.name) is None and store.get(b.token, "b.bin") is b
    assert (len(store), store.size) == (2, 80)
    # Durée : passé le délai, le fichier n'existe plus.
    clock = files.time.monotonic()
    monkeypatch.setattr(files.time, "monotonic", lambda: clock + 61)
    assert store.get(c.token, "c.bin") is None
    assert store.put("d.bin", b"d") is not None and (len(store), store.size) == (1, 1)


def test_servi_sans_risque():
    """Seule une image matricielle, reconnue à ses octets, s'affiche ;
    tout le reste se télécharge, sous un type inerte."""
    store = files.Store(60, 10_000, 5_000)
    cases = [
        ("a.png", PNG, "image/png", True),
        ("photo", b"\xff\xd8\xff\xe0" + bytes(8), "image/jpeg", True),
        ("a.gif", b"GIF89a" + bytes(8), "image/gif", True),
        ("a.webp", b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp", True),
        # Le nom ment, ou le contenu s'exécuterait : jamais en ligne.
        ("faux.png", b"<html><script>alert(1)</script>", "application/octet-stream", False),
        ("page.html", b"<!doctype html><script>", "application/octet-stream", False),
        ("dessin.svg", b"<svg xmlns='http://www.w3.org/2000/svg' onload='x()'>",
         "application/octet-stream", False),
        ("doc.pdf", b"%PDF-1.7", "application/octet-stream", False),
        ("t.csv", b"a,b\n", "application/octet-stream", False),
    ]
    for name, data, kind, inline in cases:
        stored = store.put(name, data)
        h = stored.headers()
        assert (h["Content-Type"], stored.inline) == (kind, inline), name
        assert h["Content-Disposition"].startswith(
            "inline; " if inline else "attachment; "), name
        assert h["X-Content-Type-Options"] == "nosniff"
        assert h["Content-Security-Policy"].startswith("sandbox")
        assert stored.markdown.startswith("![" if inline else "["), name
    stored = store.put("a.png", PNG)
    assert stored.url == f"https://proxy.test/v1/files/{stored.token}/a.png"
    assert stored.markdown == f"![a.png]({stored.url})"
    assert stored.headers()["Cache-Control"].startswith("private, max-age=")


def test_nom_servi():
    for name, safe in (
            ("graphique.png", "graphique.png"),
            ("out/2026/table.csv", "table.csv"),
            ("../../etc/passwd", "passwd"),
            ("C:\\temp\\x.txt", "x.txt"),
            ("mon rapport (final).pdf", "mon_rapport_final_.pdf"),
            ("a](javascript:alert(1)).png", "a_javascript_alert_1_.png"),
            ('x"\r\nSet-Cookie: a=b.txt', "x_Set-Cookie_a_b.txt"),
            ("été.png", "été.png"),
            (".bashrc", "bashrc"), ("...", "file"), ("", "file"),
            ("a" * 200 + ".xlsx", "a" * 75 + ".xlsx")):
        assert files.safe_name(name) == safe, name
    # Un nom non ASCII : en clair dans filename*, neutralisé dans filename.
    h = files.Store(60, 100, 100).put("été.png", PNG[:20]).headers()
    assert h["Content-Disposition"] == (
        "inline; filename=\"_t_.png\"; filename*=UTF-8''%C3%A9t%C3%A9.png")
    h["Content-Disposition"].encode("latin-1")      # un en-tête émissible


def test_ranger_les_fichiers_d_un_resultat(monkeypatch):
    """files.keep : ce qu'une surface appelle avec Result.files."""
    produced = (tools.Artifact("a.png", "image/png", PNG),
                tools.Artifact("gros.bin", "application/octet-stream", b"0" * 5000),
                tools.Artifact("t.csv", "text/csv", b"a,b\n"))
    kept = files.keep(produced, "clientA")
    assert [s.name for s in kept] == ["a.png", "t.csv"]
    assert files.STORE.get(kept[0].token, "a.png").data == PNG
    assert "\n\n".join(s.markdown for s in kept) == (
        f"![a.png](https://proxy.test/v1/files/{kept[0].token}/a.png)\n\n"
        f"[t.csv](https://proxy.test/v1/files/{kept[1].token}/t.csv)")
    monkeypatch.setattr(files, "PUBLIC_URL", "")
    assert files.keep(produced) == [] and len(files.STORE) == 2
