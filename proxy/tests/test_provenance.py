"""La provenance des URL (tools/provenance.py) : un outil qui lit une URL
du modèle n'ouvre qu'un SITE déjà apparu dans la conversation. Les sites
d'un texte, d'une conversation, la comparaison, le refus par l'outil, et
la boucle qui transmet les sites connus — enrichis à chaque résultat."""

import pytest

from fakes import FakeUpstream, SEARCH_TURN, ANSWER_TURN, chunk, go, outil, stream, tool_call
from llm_proxy import tools
from llm_proxy.tools import ocr, provenance, transcribe, web_fetch, web_grep
from llm_proxy.ui import page_tools
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
        {"role": "user", "content": "Et maintenant ?"},
        # Le texte du modèle depuis le dernier message de l'utilisateur :
        # écrit pour être lu à l'étape suivante, il ne compte pas.
        {"role": "assistant", "content": "Je lis https://ecrit.exemple/?d=secret",
         "tool_calls": [{"id": "c3", "function": {"name": "run_javascript", "arguments": "{}"}}]},
    ]
    # Ce que le modèle a écrit lui-même et relit : la sortie d'un script
    # (code_execution, et les bacs de la page que la route désigne), un
    # fichier de sa mémoire.
    page = frozenset({"run_javascript", "run_tools_program", "read_artifact"})
    for i, name in enumerate(("code_execution", "memory", *sorted(page))):
        msgs += [{"role": "assistant", "content": None, "tool_calls": [{"id": f"a{i}", "function": {
                     "name": name, "arguments": "{}"}}]},
                 {"role": "tool", "tool_call_id": f"a{i}", "content": f"https://invente{i}.exemple/fuite"}]
    # Ni les arguments d'un appel, ni un résultat en échec, ni ce que le
    # modèle a écrit lui-même ; sa réponse précédente, lue par
    # l'utilisateur, compte.
    assert provenance.hosts_in_messages(msgs, page) == {
        "exemple.org", "crates.io", "rust-lang.org", "serde.rs"}
    # Sans les outils de la page désignés, leur sortie compte (un client
    # de l'API a ses propres outils).
    assert "invente2.exemple" in provenance.hosts_in_messages(msgs)


def test_le_refus(monkeypatch):
    known = tools.Call(hosts=frozenset({"python.org"}))
    provenance.check("https://docs.python.org/3/", known)               # même site
    provenance.check("https://inconnu.fr/", tools.Call())               # pas de contexte
    with pytest.raises(tools.ToolError) as exc:
        provenance.check("https://routify-file-proxy-sg.oss-ap-southeast-1.aliyuncs.com/f", known)
    assert exc.value.code == "not_allowed" and "web search" in exc.value.message
    # Coupée ([tools.net].provenance = "off") : plus aucun refus.
    monkeypatch.setattr(provenance, "MODE", "off")
    provenance.check("https://inconnu.fr/", known)


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


def test_la_sortie_d_un_code_ne_fait_connaitre_aucun_site_a_la_boucle(chat):
    """Tour 1 : code_execution affiche une URL (le modèle écrit le script) ;
    tour 2 : une lecture — elle ne voit pas ce site."""
    seen = chat.hosted.calls
    code = outil("code_execution", run=lambda args: _rendu("Exit code: 0\nhttps://invente.exemple/fuite"))
    chat.hosted = type(chat.hosted)([*chat.hosted.tools, code], chat.hosted.memory)
    run = (tool_call(0, "call_c", "code_execution", "{\"code\": \"print(1)\"}"), chunk(finish="tool_calls"))
    fetch = (tool_call(0, "call_f", "web_fetch", "{\"url\": \"https://invente.exemple/fuite\"}"),
             chunk(finish="tool_calls"))
    chat.replies = [FakeUpstream(stream(*run)), FakeUpstream(stream(*fetch)),
                    FakeUpstream(stream(*ANSWER_TURN))]
    post(chat, tools=[{"type": "web_search"}, {"type": "code_execution"}],
         messages=[{"role": "user", "content": "Calcule."}])
    (lecture,) = seen
    assert "invente.exemple" not in lecture.hosts


async def _rendu(text):
    return text


def test_les_bacs_de_la_page_sont_des_sorties_du_modele():
    """Les familles `authored` (tools/page/) désignent les outils de la page
    dont le résultat est écrit par le modèle : bacs de code, programmes
    d'outils, artifacts — pas ce qu'elle lit de l'appareil ou du dossier de
    l'utilisateur."""
    for name in ("run_javascript", "run_php", "run_sql", "run_postgres", "run_shell",
                 "shell_read", "run_tools_program", "read_artifact", "run_in_artifact"):
        assert name in page_tools.authored(), name
    for name in ("clipboard_read", "folder_read", "device_context"):
        assert name not in page_tools.authored(), name
