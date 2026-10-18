"""La provenance des URL (tools/provenance.py) : un outil qui lit une URL
du modèle n'ouvre qu'un SITE déjà apparu dans la conversation. Les sites
d'un texte, d'une conversation, la comparaison, le refus par l'outil, et
la boucle qui transmet les sites connus — enrichis à chaque résultat."""

import pytest

from fakes import FakeUpstream, SEARCH_TURN, ANSWER_TURN, chunk, go, stream, tool_call
from llm_proxy import tools
from llm_proxy.tools import ocr, provenance, transcribe, web_fetch, web_grep
from test_chat_api import chat, post  # noqa: F401 — la fixture `chat`


def test_site():
    assert provenance.site("docs.python.org") == "python.org"
    assert provenance.site("www.bbc.co.uk") == "bbc.co.uk"
    assert provenance.site("service-public.gouv.fr") == "service-public.gouv.fr"
    assert provenance.site("a.b.service-public.gouv.fr") == "service-public.gouv.fr"


def test_sites_d_un_texte():
    t = ("Voir https://docs.python.org/3/library/asyncio.html et regarde frerestoque.fr, "
         "pas config.toml ni app.js ; mail : moi@exemple.com")
    assert provenance.hosts_in_text(t) == {"python.org", "frerestoque.fr"}


def test_sites_d_une_conversation():
    msgs = [
        {"role": "system", "content": "Résumé : on parlait de https://wiki.exemple.org/x"},
        {"role": "user", "content": [{"type": "text", "text": "Lis crates.io/crates/serde"},
                                     {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]},
        {"role": "assistant", "content": "D'après https://doc.rust-lang.org …",
         "tool_calls": [{"id": "c1", "function": {"name": "web_fetch",
                         "arguments": "{\"url\": \"https://routify-file-proxy.aliyuncs.com/x\"}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "Error: https://routify-file-proxy.aliyuncs.com/x returned HTTP 403."},
        {"role": "tool", "tool_call_id": "c2", "content": "1. Serde — https://serde.rs/"},
    ]
    # Ni les arguments d'un appel, ni un résultat en échec.
    assert provenance.hosts_in_messages(msgs) == {"exemple.org", "crates.io", "rust-lang.org", "serde.rs"}


def test_le_refus():
    known = tools.Call(hosts=frozenset({"python.org"}))
    provenance.check("https://docs.python.org/3/", known)               # même site
    provenance.check("https://inconnu.fr/", tools.Call())               # pas de contexte
    with pytest.raises(tools.ToolError) as exc:
        provenance.check("https://routify-file-proxy-sg.oss-ap-southeast-1.aliyuncs.com/f", known)
    assert exc.value.code == "not_allowed" and "web search" in exc.value.message


def test_coupee(monkeypatch):
    monkeypatch.setattr(provenance, "MODE", "off")
    provenance.check("https://inconnu.fr/", tools.Call(hosts=frozenset()))


@pytest.mark.parametrize("tool, args", [
    (web_fetch.TOOL, {"url": "https://inconnu.fr/page"}),
    (web_grep.TOOL, {"url": "https://inconnu.fr/page", "pattern": "x"}),
    (ocr.TOOL, {"url": "https://inconnu.fr/scan.png"}),
    (transcribe.TOOL, {"url": "https://inconnu.fr/son.mp3"}),
])
def test_les_quatre_outils_refusent_avant_toute_requete(tool, args):
    with pytest.raises(tools.ToolError) as exc:
        go(tool.run(args, tools.Call(hosts=frozenset({"exemple.fr"}))))
    assert exc.value.code == "not_allowed"


def test_la_boucle_transmet_les_sites_connus_et_les_enrichit(chat):
    """Q : un site dans la question ; tour 1 : une recherche (ses
    résultats citent github.com) ; tour 2 : une lecture — elle voit les
    deux. Hors conversation (/v1/tools) : pas de contexte."""
    fetch = (tool_call(0, "call_f", "web_fetch", "{\"url\": \"https://github.com/ggml-org/llama.cpp/releases\"}"),
             chunk(finish="tool_calls"))
    chat.replies = [FakeUpstream(stream(*SEARCH_TURN)), FakeUpstream(stream(*fetch)),
                    FakeUpstream(stream(*ANSWER_TURN))]
    post(chat, messages=[{"role": "user", "content": "Compare avec ce que dit lemonde.fr"}])
    first, second = chat.hosted.calls
    assert "lemonde.fr" in first.hosts and "github.com" not in first.hosts
    assert {"lemonde.fr", "github.com", "example.org"} <= second.hosts
    chat.client.post("/v1/tools/web_search", json={"query": "x"})
    assert chat.hosted.calls[-1].hosts is None
