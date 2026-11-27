"""Les modèles du proxy vus par `omarchy models` (surfaces/omarchy.py) :
la liste de ce que voit la clé, le chat sous /local-ai/<id>/v1, le nom
préfixé ramené à un identifiant de recette. Les modèles permis d'un
compte : test_backends.py (les modèles permis d'un compte)."""

import json

import httpx2
import pytest

from fakes import FakeUpstream, bearer, fake_backend
from llm_proxy.access import auth
from llm_proxy.storage import stats
from llm_proxy.surfaces import omarchy

KEY = "0f6c2f9e-4b1a-4c7e-9d4e-2a1b3c4d5e6f"
BACKEND_KEY = "sk-backend-secret-omarchy"

CATALOG = [
    {"id": "Mistral_Small-3.2", "type": "text-generation", "max_context_length": 128000},
    {"id": "gemma-vision", "type": "image-text-to-text"},
    {"id": "bge-m3", "type": "text-embeddings-inference"},
    # «essai/a.b» et «essai/a-.b» donnent tous deux «essai.a.b».
    {"id": "a.b", "type": "text-generation"},
    {"id": "a-.b", "type": "text-generation"},
]


@pytest.fixture
def shared(proxy, stats_db, monkeypatch):
    """`proxy`, l'auth active (une clé), le backend «essai» à images
    (plafond de réponse 4096) dont /v1/models rend CATALOG, et la surface
    Omarchy ouverte."""
    def handler(request):
        return httpx2.Response(200, json={"object": "list", "data": CATALOG})
    fake_backend(monkeypatch, "essai", {
        "url": "http://essai.test", "api_key": BACKEND_KEY, "chat_template": True,
        "images": True, "max_tokens": 4096}, handler)
    monkeypatch.setattr(auth, "PROXY_API_KEYS", [KEY])
    monkeypatch.setattr(omarchy, "ENABLED", True)
    return proxy


# ── identifiants ────────────────────────────────────────────────────────

def test_un_nom_prefixe_devient_un_identifiant_de_recette():
    for name, rid in [("albert/Mistral_Small-3.2", "albert.mistral-small-3.2"),
                      ("local/qwen3.8-flash-next", "local.qwen3.8-flash-next"),
                      ("a/b:latest", "a.b-latest"),
                      ("a/-b--c_", "a.b-c"),
                      ("x/../y", "x.y"),
                      ("/", "")]:
        assert omarchy.recipe_id(name) == rid, name


# ── la liste ────────────────────────────────────────────────────────────

def test_la_liste_propose_les_modeles_de_conversation_que_voit_la_cle(shared):
    """Sans clé, une liste vide (pas un 401 : une machine non appairée la
    lit ainsi) ; une clé invalide, 401 ; avec la clé, ses modèles."""
    r = shared.client.get("/local-ai/models.json")
    assert r.status_code == 200 and r.json() == {
        "schema": 1, "ram_total_gib": None, "models": []}
    assert shared.client.get("/local-ai/models.json",
                             headers=bearer("mauvaise")).status_code == 401
    r = shared.client.get("/local-ai/models.json", headers=bearer(KEY))
    assert r.status_code == 200
    body = r.json()
    assert body["schema"] == 1
    got = {m["recipe"]: m for m in body["models"]}
    # Ni l'embedding, ni les deux noms en collision.
    assert sorted(got) == ["essai.gemma-vision", "essai.mistral-small-3.2"]
    small, vision = got["essai.mistral-small-3.2"], got["essai.gemma-vision"]
    assert (small["name"], small["ctx"], small["max_tokens"], small["input"],
            small["unit"], small["kind"]) \
        == ("essai/Mistral_Small-3.2", 128000, 4096, ["text"], "active", "text")
    assert (vision["ctx"], vision["input"]) == (omarchy.DEFAULT_CONTEXT, ["text", "image"])
    assert BACKEND_KEY not in r.text


def test_la_liste_porte_la_vitesse_mesuree_relue_au_plus_une_fois_par_minute(shared, monkeypatch):
    """decode_tok_s : la vitesse de génération mesurée par le proxy, que le
    panneau d'Omarchy affiche ; ignorée sans clé, relue au plus une fois
    par SPEEDS_TTL."""
    calls, current = [], {"essai/mistral_small-3.2": 50.0}

    def fake_speeds():
        calls.append(1)
        return dict(current)
    monkeypatch.setattr(stats, "decode_speeds", fake_speeds)

    def speed():
        r = shared.client.get("/local-ai/models.json", headers=bearer(KEY))
        return {m["recipe"]: m["decode_tok_s"] for m in r.json()["models"]}

    # Liste vide sans clé : pas de lecture SQLite inutile.
    assert shared.client.get("/local-ai/models.json").status_code == 200
    assert calls == []
    assert speed() == {"essai.mistral-small-3.2": 50.0, "essai.gemma-vision": None}
    current["essai/mistral_small-3.2"] = 200.0
    assert speed()["essai.mistral-small-3.2"] == 50.0 and len(calls) == 1
    later = omarchy.time.monotonic() + omarchy.SPEEDS_TTL
    monkeypatch.setattr(omarchy.time, "monotonic", lambda: later)
    assert speed()["essai.mistral-small-3.2"] == 200.0 and len(calls) == 2


def test_desactivees_les_routes_omarchy_repondent_404(shared, monkeypatch):
    monkeypatch.setattr(omarchy, "ENABLED", False)
    assert shared.client.get("/local-ai/models.json", headers=bearer(KEY)).status_code == 404
    r = shared.client.post("/local-ai/essai.mistral-small-3.2/v1/chat/completions",
                           json={"model": "essai.mistral-small-3.2", "messages": []},
                           headers=bearer(KEY))
    assert r.status_code == 404


# ── le chat ─────────────────────────────────────────────────────────────

def test_le_chat_part_vers_le_modele_du_chemin_sous_son_vrai_nom(shared):
    """Le `model` du corps est ignoré : c'est le chemin qui choisit, même
    quand le corps en nomme un autre."""
    answer = {"id": "c1", "object": "chat.completion", "choices": [{
        "index": 0, "message": {"role": "assistant", "content": "ok"},
        "finish_reason": "stop"}]}
    shared.replies = [FakeUpstream(json.dumps(answer).encode())]
    r = shared.client.post(
        "/local-ai/essai.mistral-small-3.2/v1/chat/completions",
        json={"model": "essai/gemma-vision",
              "messages": [{"role": "user", "content": "été"}]},
        headers=bearer(KEY))
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "ok"
    assert shared.sent[0]["model"] == "Mistral_Small-3.2"
    assert shared.sent[0]["messages"][0]["content"] == "été"
    assert shared.lines[0][1:3] == ("essai", "Mistral_Small-3.2")


def test_le_chat_exige_la_cle_et_un_modele_partage(shared):
    body = {"model": "x", "messages": [{"role": "user", "content": "salut"}]}
    cases = [
        ("essai.mistral-small-3.2", {}, 401),
        ("essai.bge-m3", bearer(KEY), 404),
        ("essai.a.b", bearer(KEY), 404),
        ("inconnu", bearer(KEY), 404),
    ]
    for rid, headers, status in cases:
        r = shared.client.post(f"/local-ai/{rid}/v1/chat/completions",
                               json=body, headers=headers)
        assert r.status_code == status, rid
    assert shared.sent == []


def test_le_catalogue_d_un_modele_partage_porte_son_identifiant(shared):
    r = shared.client.get("/local-ai/essai.mistral-small-3.2/v1/models",
                          headers=bearer(KEY))
    assert r.status_code == 200
    assert [m["id"] for m in r.json()["data"]] == ["essai.mistral-small-3.2"]
    assert shared.client.get("/local-ai/essai.mistral-small-3.2/v1/models").status_code == 401
