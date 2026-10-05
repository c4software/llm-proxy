"""L'outil hébergé `image_generation` : le module (requête au backend
d'images, bornes), puis la surface Responses jusqu'à la route entière.
Aucun réseau : le client HTTP du backend «essai» est un
`httpx.MockTransport` ; le module, lui, est le vrai."""

import asyncio
import json
import types

import httpx
import pytest

from fakes import (ANSWER_DOC, ANSWER_TURN, FakeUpstream, chat_doc, chunk,
                   feed, hosted_tools, sse_events, stream, tool_call, usage)
from llm_proxy import responses_api as R
from llm_proxy import tools
from llm_proxy.backends import BACKENDS, Backend
from llm_proxy.tools import image_generation as G
from llm_proxy.tools import web_fetch, web_search

# La classe, prise avant que la fixture `proxy` ne la remplace dans le
# paquet par son annuaire.
Hosted = tools.Hosted

# Un PNG d'un pixel : seuls ses premiers octets comptent (format lu).
PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAC"
       "hwGA60e6kgAAAABJRU5ErkJggg==")
PROMPT = "{\"prompt\": \"un hibou\"}"
TEXT = G._text("512x512", "png")
TOOL = {"type": "image_generation", "size": "auto", "quality": "high",
        "output_format": "png", "partial_images": 2, "model": "gpt-image-1"}


@pytest.fixture
def images(monkeypatch):
    """Le module réglé sur `essai/qwen-image`, et le backend «essai» dont
    le client répond par `images.reply(requête)` — une image par défaut.
    `images.sent` : les requêtes parties au backend d'images ;
    `images.hosted` : l'annuaire qui ne porte que cet outil."""
    env = types.SimpleNamespace(sent=[])
    env.reply = lambda request: httpx.Response(
        200, json={"created": 1, "data": [{"b64_json": PNG}]})

    async def handler(request):
        env.sent.append(request)
        reply = env.reply(request)
        return await reply if asyncio.iscoroutine(reply) else reply

    backend = Backend("essai", {"url": "http://backend.invalid",
                                "api_key": "cle-backend"})
    backend.client = httpx.AsyncClient(
        base_url=backend.url, transport=httpx.MockTransport(handler))
    monkeypatch.setitem(BACKENDS, "essai", backend)
    for name, value in (("ENABLED", True), ("MODEL", "essai/qwen-image"),
                        ("SIZE", "512x512"), ("STEPS", 0), ("MAX_CALLS", 2),
                        ("SIZES", ["512x512", "1024x1024"])):
        monkeypatch.setattr(G, name, value)
    env.hosted = Hosted(modules=[G], memory=tools.Memory(8, 60))
    env.bodies = lambda: [json.loads(r.content) for r in env.sent]
    return env


def go(coro):
    return asyncio.run(coro)


def request(**extra):
    return {"model": "essai/qwen", "input": "Dessine un hibou",
            "tools": [TOOL], "stream": True, **extra}


def image_turn(arguments=PROMPT, call_id="call_i"):
    return FakeUpstream(stream(
        tool_call(0, call_id, "image_generation", arguments),
        chunk(finish="tool_calls"), usage(100, 10)))


@pytest.fixture
def route(proxy, images):
    """La route /v1/responses entière : la fixture `proxy`, avec le vrai
    module d'images derrière le faux backend."""
    proxy.hosted = images.hosted
    proxy.images = images
    return proxy


# ── le module ───────────────────────────────────────────────────────────

def test_disabled_by_default_and_declared_only_on_request(images):
    # La configuration d'exemple ne l'active pas (config.py l'a lue avant
    # que la fixture ne règle le module).
    from llm_proxy import config
    assert config.flag("tools.image_generation.enabled", False) is False
    out, ctx = R.to_chat(request(), hosted=images.hosted)
    assert out["tools"] == [G.DEFINITION] and list(ctx.hosted) == [G.NAME]
    assert G.DEFINITION["function"]["parameters"]["required"] == ["prompt"]
    # Le client voit revenir SON outil ; ce qu'il y règle est borné :
    # seule une taille permise est retenue, le reste est ignoré.
    assert ctx.echo["tools"] == [TOOL] and ctx.options == {G.NAME: {}}
    for size, kept in (("1024x1024", {"size": "1024x1024"}),
                       ("4096x4096", {}), (512, {})):
        _, ctx = R.to_chat(request(tools=[{**TOOL, "size": size}]),
                           hosted=images.hosted)
        assert ctx.options == {G.NAME: kept}, size
    # Sans le module, l'outil est ignoré comme avant ; sans la
    # déclaration du client, rien n'est présenté.
    out, ctx = R.to_chat(request(), hosted=hosted_tools())
    assert "tools" not in out and ctx.ignored == ["image_generation"]
    assert "tools" not in R.to_chat(request(tools=[]), hosted=images.hosted)[0]


def test_run_sends_the_backend_what_gufo_expects(images, monkeypatch):
    result = go(G.run({"prompt": " un hibou "}))
    # Une chaîne pour le modèle, qui porte l'image pour le client.
    assert result == TEXT and "512x512" in TEXT and type(str(result)) is str
    assert (result.b64, result.size, result.format) == (PNG, "512x512", "png")
    (sent,) = images.sent
    assert sent.url == "http://backend.invalid/v1/images/generations"
    assert sent.headers["authorization"] == "Bearer cle-backend"
    # Préfixe retiré, rien que les champs de gufo-media.ts.
    assert images.bodies() == [{"model": "qwen-image", "prompt": "un hibou",
                                "size": "512x512"}]
    # La taille : celle du client d'abord, puis celle du modèle si elle
    # est permise, sinon le défaut ; `steps` s'il est réglé.
    monkeypatch.setattr(G, "STEPS", 8)
    for args, options, size in (
        ({"size": "1024x1024"}, {}, "1024x1024"),
        ({"size": "4096x4096"}, {}, "512x512"),
        ({"size": "1024x1024"}, {"size": "512x512"}, "512x512"),
    ):
        go(G.run({"prompt": "x", **args}, **options))
        assert images.bodies()[-1] == {"model": "qwen-image", "prompt": "x",
                                       "size": size, "steps": 8}


def test_run_failures_are_texts_for_the_model(images, monkeypatch):
    def unreachable(request):
        raise httpx.ConnectError("éteint")

    def slow(request):
        raise httpx.ReadTimeout("trop long")

    for reply, needle in (
        (lambda r: httpx.Response(500, text="CUDA out of memory"),
         "HTTP 500 (CUDA out of memory)"),
        (lambda r: httpx.Response(200, json={"data": [{"url": "http://x"}]}),
         "no `b64_json`"),
        (lambda r: httpx.Response(200, text="<html>"), "no `b64_json`"),
        (unreachable, "unreachable (ConnectError)"),
        (slow, "timed out"),
    ):
        images.reply = reply
        result = go(G.run({"prompt": "x"}))
        assert result.startswith("Error:") and needle in result, result
        assert not isinstance(result, G.Image)
    # Rien ne part : pas de prompt, ou outil sans modèle préfixé.
    images.sent.clear()
    assert go(G.run({"prompt": " "})) == "Error: `prompt` is required."
    for model in ("", "qwen-image", "inconnu/qwen-image"):
        monkeypatch.setattr(G, "MODEL", model)
        assert "not configured" in go(G.run({"prompt": "x"})), model
    assert not images.sent


def test_generation_has_its_own_timeout_and_limit(images, monkeypatch):
    """Le délai commun ([tools].run_timeout, fait pour une recherche) ne
    coupe pas une génération ; c'est celui du module qui vaut. Et le
    nombre d'images par réponse a sa limite, en plus de max_calls."""
    async def slow(request):
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"data": [{"b64_json": PNG}]})

    images.reply = slow
    monkeypatch.setattr(tools, "RUN_TIMEOUT", 0.01)
    monkeypatch.setattr(G, "RUN_TIMEOUT", 5)
    assert go(images.hosted.run(G.NAME, PROMPT, 0)) == TEXT
    monkeypatch.setattr(G, "RUN_TIMEOUT", 0.01)
    assert "timed out" in go(images.hosted.run(G.NAME, PROMPT, 0))
    monkeypatch.setattr(G, "RUN_TIMEOUT", 5)
    sent = len(images.sent)
    result = go(images.hosted.run(G.NAME, PROMPT, 2, same=2))
    assert result.startswith("Error: the limit of 2 image_generation calls")
    assert len(images.sent) == sent


# ── la surface Responses ────────────────────────────────────────────────

def test_stream_renders_the_image_to_the_client_and_a_text_to_the_model(route):
    route.replies = [image_turn(), FakeUpstream(stream(*ANSWER_TURN))]
    r = route.client.post("/v1/responses", json=request())
    ev = sse_events(r.content)
    kinds = [name for name, _ in ev]
    assert kinds == [
        "response.created", "response.in_progress",
        "response.output_item.added",
        "response.image_generation_call.in_progress",
        "response.image_generation_call.generating",
        "response.image_generation_call.completed",
        "response.output_item.done",
        "response.output_item.added", "response.content_part.added",
        "response.output_text.delta", "response.output_text.done",
        "response.content_part.done", "response.output_item.done",
        "response.completed"]
    added, done = ev[2][1]["item"], ev[6][1]["item"]
    assert added == {"id": added["id"], "type": "image_generation_call",
                     "status": "in_progress"}
    assert added["id"].startswith("ig_")
    assert [(d["output_index"], d["item_id"]) for _, d in ev[3:6]] == [
        (0, added["id"])] * 3
    assert done == {
        "id": added["id"], "type": "image_generation_call",
        "status": "completed", "result": PNG, "revised_prompt": "un hibou",
        "size": "512x512", "output_format": "png"}
    final = ev[-1][1]["response"]
    assert final["output"][0] == done and final["status"] == "completed"
    assert final["usage"]["input_tokens"] == 250
    # Le modèle, lui, reçoit l'appel et une phrase — jamais l'image.
    one, two = route.sent
    assert [t["function"]["name"] for t in one["tools"]] == [G.NAME]
    assert two["messages"][len(one["messages"]):] == [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": added["id"], "type": "function", "function": {
                "name": G.NAME, "arguments": PROMPT}}]},
        {"role": "tool", "tool_call_id": added["id"], "content": TEXT}]
    assert PNG not in json.dumps(route.sent)
    # La mémoire ne garde que le texte, en `str` nu.
    kept = route.hosted.memory.recall(added["id"])
    assert kept == {"name": G.NAME, "arguments": PROMPT, "result": TEXT}
    assert type(kept["result"]) is str
    # Une ligne de stats, celle de la conversation : l'image n'y est pas.
    assert len(route.lines) == 1 and route.lines[0][6:8] == (250, 15)
    assert len(route.images.sent) == 1


def test_json_mode_and_client_size(route):
    route.replies = [
        FakeUpstream(chat_doc({"content": None, "tool_calls": [
            {"id": "call_i", "function": {"name": G.NAME, "arguments": json.dumps(
                {"prompt": "un hibou", "size": "512x512"})}}]},
            "tool_calls", 100, 10)),
        FakeUpstream(ANSWER_DOC)]
    resp = route.client.post("/v1/responses", json=request(
        stream=False, tools=[{**TOOL, "size": "1024x1024"}])).json()
    assert [o["type"] for o in resp["output"]] == [
        "image_generation_call", "message"]
    # La taille posée par le client sur son outil l'emporte sur le modèle.
    assert resp["output"][0] == {
        "id": resp["output"][0]["id"], "type": "image_generation_call",
        "status": "completed", "result": PNG, "revised_prompt": "un hibou",
        "size": "1024x1024", "output_format": "png"}
    assert route.images.bodies()[0]["size"] == "1024x1024"
    assert route.sent[1]["messages"][-1]["content"] == G._text("1024x1024", "png")


def test_backend_failure_is_a_failed_item_and_a_text_for_the_model(route):
    """Le backend d'images en erreur, puis une image de trop : l'élément
    est `failed`, sans image ni événement `.completed` ; le modèle lit
    l'erreur et conclut — la réponse, elle, aboutit."""
    route.images.reply = lambda r: httpx.Response(503, text="modèle absent")
    route.replies = [image_turn(), FakeUpstream(stream(*ANSWER_TURN))]
    ev = sse_events(route.client.post("/v1/responses", json=request()).content)
    assert "response.image_generation_call.completed" not in [n for n, _ in ev]
    final = ev[-1][1]["response"]
    assert final["status"] == "completed"
    assert final["output"][0] == {
        "id": final["output"][0]["id"], "type": "image_generation_call",
        "status": "failed", "result": None, "revised_prompt": "un hibou"}
    assert route.sent[1]["messages"][-1]["content"] == (
        "Error: the image backend returned HTTP 503 (modèle absent).")

    # Trois images demandées d'un coup, deux permises par réponse.
    route.images.reply = lambda r: httpx.Response(
        200, json={"data": [{"b64_json": PNG}]})
    route.images.sent.clear()
    route.replies = [FakeUpstream(stream(
        *[tool_call(i, f"call_{i}", G.NAME, PROMPT) for i in range(3)],
        chunk(finish="tool_calls"), usage(10, 1))),
        FakeUpstream(stream(*ANSWER_TURN))]
    ev = sse_events(route.client.post("/v1/responses", json=request()).content)
    assert [o["status"] for o in ev[-1][1]["response"]["output"][:3]] == [
        "completed", "completed", "failed"]
    assert len(route.images.sent) == 2
    assert "limit of 2" in route.sent[-1]["messages"][-1]["content"]


def test_loop_turn_and_client_replay_send_the_same_bytes(images):
    """Le client renvoie l'élément AVEC son image : il redevient l'appel
    et le texte que le modèle avait lu, octet pour octet ce que le tour 2
    de la boucle a envoyé — et l'image ne part jamais au modèle."""
    hosted = images.hosted
    req = request(input=[{"type": "message", "role": "user",
                          "content": "Dessine un hibou"}])
    _, ctx = R.to_chat(req, hosted=hosted)
    t = R.Translator(200, "text/event-stream", ctx)
    feed(t, image_turn().body)
    t.resolve(t.pending[0], go(hosted.run(G.NAME, PROMPT, 0)))
    looped, _ = R.to_chat({**req, "input": req["input"] + t.output},
                          hosted=hosted)
    t.next_turn()
    final = sse_events(feed(t, stream(*ANSWER_TURN)))[-1][1]["response"]
    client_copy = json.loads(json.dumps(final["output"]))
    assert client_copy[0]["result"] == PNG
    encode = lambda doc: json.dumps(doc, ensure_ascii=False)

    def replay(items, memory=hosted.memory):
        again, ctx = R.to_chat(
            {**req, "input": req["input"] + items},
            hosted=Hosted(modules=[G], memory=memory))
        assert PNG not in encode(again) and not ctx.ignored
        return again["messages"]

    n = len(looped["messages"])
    assert [m["role"] for m in looped["messages"][-2:]] == ["assistant", "tool"]
    assert encode(replay(client_copy)[:n]) == encode(looped["messages"])
    # La forme courte de la doc d'OpenAI (l'id seul) : la mémoire suffit.
    assert encode(replay([{"type": "image_generation_call",
                           "id": client_copy[0]["id"]}])) \
        == encode(looped["messages"])

    # Mémoire perdue (proxy redémarré) : l'appel est reconstruit du
    # `revised_prompt`. L'élément entier redonne le texte de la boucle ;
    # ce que Codex en garde (id, status, revised_prompt, result), un
    # texte sans taille — qui ne demande pas de relancer l'outil.
    lost = tools.Memory(8, 60)
    call, text = replay(client_copy[:1], lost)[-2:]
    assert call["tool_calls"][0]["function"] == {
        "name": G.NAME, "arguments": PROMPT} and text["content"] == TEXT
    codex = {k: client_copy[0][k] for k in (
        "type", "id", "status", "revised_prompt", "result")}
    assert replay([codex], lost)[-1]["content"] == G.REPLAYED
    assert replay([{**codex, "status": "failed", "result": None}],
                  lost)[-1]["content"] == G.FAILED


def test_not_presented_on_messages_nor_callable_on_v1_tools(route, monkeypatch):
    # /v1/messages : seule la recherche est branchée.
    route.hosted = Hosted(modules=[web_search, web_fetch, G],
                          memory=tools.Memory(8, 60))
    route.replies = [FakeUpstream(chat_doc({"content": "Non."}, "stop", 5, 1))]
    r = route.client.post("/v1/messages", json={
        "model": "essai/qwen", "max_tokens": 16,
        "messages": [{"role": "user", "content": "Dessine un hibou"}],
        "tools": [{"type": "image_generation"},
                  {"type": "web_search_20250305", "name": "web_search"}]})
    assert r.status_code == 200
    assert [t["function"]["name"] for t in route.sent[0]["tools"]] == [
        "web_search"]
    # /v1/tools : ni listé ni appelable — GET et POST disent la même chose.
    monkeypatch.setattr(tools, "enabled", lambda: route.hosted.modules)
    listed = [t["name"] for t in route.client.get("/v1/tools").json()["data"]]
    assert listed == ["web_search", "web_fetch"]
    r = route.client.post("/v1/tools/image_generation", json={"prompt": "x"})
    assert r.status_code == 404 and r.json()["error"]["type"] == "unknown_tool"
    assert not route.images.sent
