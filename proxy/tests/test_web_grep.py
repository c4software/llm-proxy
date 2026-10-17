"""`web_grep` (tools/web_grep.py) : la recherche d'une expression dans une
page lue par web_fetch.load — même faux web, mêmes réglages que
test_tools.py, dont les fixtures sont reprises ici."""

import httpx2

from llm_proxy import tools
from llm_proxy.tools import web_fetch, web_grep, webcache
from test_tools import Web, dns, go, page, pdf, reglages, rendu  # noqa: F401

DOC = "\n".join([
    "Intro",                            # offset 0
    "",
    "max_tokens limits the answer",     # 7
    "temperature sets randomness",      # 36
    "",
    "Une ligne neutre",                 # 65
    "autre ligne",                      # 82
    "encore une",                       # 94
    "MAX_TOKENS again, in caps",        # 105
    "fin",                              # 131
])


def grep(args, web=None) -> tools.Result:
    web = web or Web(default=page(DOC))
    return rendu(web_grep.TOOL.run({"url": "http://site.test/doc", **args},
                                   tools.Call(),
                                   transport=httpx2.MockTransport(web.handler)))


def corps(result) -> list[str]:
    return result.text.split("\n---\n")[1].split("\n")


def test_lignes_trouvees_avec_contexte_et_offsets():
    """Le contexte se compte en lignes non vides ; deux blocs disjoints
    sont séparés par «--» ; la casse est ignorée par défaut."""
    out = grep({"pattern": "max_tokens"})
    assert out.error is None
    assert out.text == (
        "URL: http://site.test/doc\nContent-Type: text/plain\nCharacters: 134\n"
        "Matches: 2 line(s) for /max_tokens/. Matching lines start with `>`; "
        "every line starts with its character offset in the page.\n\n---\n"
        "  0  Intro\n"
        "> 7  max_tokens limits the answer\n"
        "  36  temperature sets randomness\n"
        "--\n"
        "  94  encore une\n"
        "> 105  MAX_TOKENS again, in caps\n"
        "  131  fin")
    assert out.meta["matches"] == 2 and out.sources[0].url == "http://site.test/doc"
    assert corps(grep({"pattern": "max_tokens", "case_sensitive": True,
                       "context": 0})) == ["> 7  max_tokens limits the answer"]
    # Blocs qui se touchent : fusionnés, sans «--».
    assert corps(grep({"pattern": "neutre|autre"})) == [
        "  36  temperature sets randomness", "> 65  Une ligne neutre",
        "> 82  autre ligne", "  94  encore une"]
    out = grep({"pattern": "n", "max_matches": 2, "context": 0})
    assert "Matches: 8 line(s) for /n/, first 2 shown." in out.text
    assert corps(out) == ["> 0  Intro", "> 7  max_tokens limits the answer"]
    # Bornes ramenées dans les limites, valeurs bizarres ignorées.
    assert len(corps(grep({"pattern": "n", "max_matches": 10**6,
                           "context": "3"}))) == 8
    assert grep({"pattern": "zzz"}).text.endswith("Matches: none for /zzz/.")


def test_ligne_longue_coupee_autour_de_la_correspondance(monkeypatch):
    monkeypatch.setattr(web_grep, "LINE_CHARS", 20)
    texte = "a" * 50 + "CIBLE" + "b" * 50 + "\n" + "c" * 30
    trouve, contexte = corps(grep({"pattern": "cible"}, Web(default=page(texte))))
    assert trouve == "> 0  …" + "a" * 7 + "CIBLE" + "b" * 8 + "…"
    assert contexte == "  106  " + "c" * 20 + "…"


def test_motif_absent_invalide_ou_trop_lent(monkeypatch):
    web = Web(default=page(DOC))
    for args, code, debut in [
            ({"pattern": ""}, "invalid_input", "Error: `pattern` is required."),
            ({"pattern": 3}, "invalid_input", "Error: `pattern` is required."),
            ({"pattern": "x" * 501}, "invalid_input", "Error: `pattern` is longer"),
            ({"pattern": "(a"}, "invalid_input", "Error: invalid regular expression")]:
        out = grep(args, web)
        assert out.error == code and out.text.startswith(debut), args
    assert rendu(web_grep.TOOL.run({"pattern": "a"}, tools.Call())).error == "invalid_input"
    assert web.requests == []           # rien de téléchargé pour un motif refusé
    monkeypatch.setattr(web_grep, "REGEX_TIMEOUT", 0.2)
    out = grep({"pattern": "(x+x+)+y"}, Web(default=page("x" * 5000)))
    assert out.error == "failed" and "more than 0.2 s" in out.text


def test_lecture_et_garde_fous_de_web_fetch(monkeypatch):
    html = ("<html><head><title>Doc</title></head><body><p>alpha</p>"
            "<p>beta</p></body></html>")
    out = grep({"pattern": "beta"}, Web(default=page(html, ct="text/html")))
    assert out.text.startswith("URL: http://site.test/doc\nTitle: Doc\n")
    assert out.text.endswith("  0  alpha\n> 7  beta")
    assert grep({"pattern": "a"}, Web(default=page("x", ct="image/png"))).error \
        == "unsupported"
    # Un PDF : son texte, comme pour web_fetch.
    out = grep({"pattern": "seconde"}, Web(default=page(
        pdf("Premiere page", "Seconde page"), ct="application/pdf")))
    assert out.error is None and "Seconde page" in out.text
    monkeypatch.setattr(web_grep.web_fetch.net, "BLOCKED_DOMAINS", ["site.test"])
    assert grep({"pattern": "a"}).error == "not_allowed"


def test_cache_commun_avec_web_fetch(monkeypatch):
    monkeypatch.setattr(webcache.CACHE, "ttl", 600)
    web = Web(default=page(DOC))
    grep({"pattern": "fin"}, web)
    out = rendu(web_fetch.TOOL.run({"url": "http://site.test/doc", "offset": 131},
                                   tools.Call(),
                                   transport=httpx2.MockTransport(web.handler)))
    assert out.text.endswith("\n---\nfin") and len(web.requests) == 1


def test_resume_prompt_et_attribution():
    assert web_grep.TOOL.summary({"url": "u", "pattern": "p", "context": 2}) == {
        "type": "find_in_page", "url": "u", "pattern": "p"}
    assert "web_fetch" in web_grep.TOOL.prompt({"web_fetch", "web_grep"})
    assert "web_fetch" not in web_grep.TOOL.prompt({"web_grep"})
    h = tools.Hosted([web_fetch.TOOL, web_grep.TOOL], tools.Memory(4, 60))
    item = {"type": "web_search_call", "action": {"type": "find_in_page"}}
    assert h.for_item(item) is web_grep.TOOL
    assert [t.name for t in h.for_responses("web_search")] == ["web_fetch", "web_grep"]
    assert go(h.run("web_grep", '{"url": "http://site.test/", "pattern": "("}', 0)).error \
        == "invalid_input"
