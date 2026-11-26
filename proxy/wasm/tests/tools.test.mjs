// Le registre des outils de la page (chat/tools/index.js) : les
// DÉCLARATIONS du proxy (/ui/session, `chat.page`) attachées à
// l'EXÉCUTION de la page (install), et le module d'une extension importé
// (installAll). Les déclarations sont écrites ici à la main, à la forme de
// ui/page_tools.py.
import "./setup.mjs";
import { test } from "vitest";
import assert from "node:assert/strict";

const tools = await import("../../llm_proxy/ui/web/static/chat/tools/index.js");

const fn = (name, description) => ({ type: "function", function: { name, description,
  parameters: { type: "object", properties: {} } } });
const page = (families, extra = []) => ({
  families,
  tools: [...families.flatMap((f) => f.tools).map((n) => ({ name: n, spec: fn(n, `déclaré : ${n}`),
                                                            settings: { borne: 3 } })), ...extra],
});

test("install : la déclaration du proxy sur l'exécution de la page, rien sans l'une ou l'autre", () => {
  const errors = tools.install(page([
    { key: "context", label: "Date", hint: "h", on: true, guidance: "", tools: ["device_context", "fantome"] },
    { key: "ask", label: "Questions", fixed: true, guidance: "Consigne figée.", tools: ["ask_user"] },
    { key: "inconnue", label: "?", tools: ["device_context"] },
  ]));
  assert.deepEqual(errors, ["outil « fantome » : déclaré, sans exécution",
                            "famille « inconnue » : déclarée, sans exécution"]);
  assert.deepEqual(tools.FAMILIES.map((f) => f.key), ["context", "ask"]);
  assert.deepEqual(tools.menuFamilies().map((f) => f.key), ["context"], "une famille fixed n'est pas au menu");
  assert.deepEqual(tools.defaultFamilies(), ["context"]);
  const ctx = {};
  assert.deepEqual(tools.specs(ctx, ["context"]).map((s) => s.function.description),
                   ["déclaré : device_context", "déclaré : ask_user"]);
  assert.equal(tools.guidance(ctx, []), "Consigne figée.");
  assert.ok(tools.isTool("device_context") && !tools.isTool("fantome") && !tools.isTool("get_location"),
            "une exécution sans déclaration (get_location) n'est pas un outil de la page");
});

test("installAll : le module d'une extension rend son exécution ; hors déclaration, écarté", async () => {
  const declared = page([], [{ name: "vibrer", spec: fn("vibrer", "Fait vibrer."), settings: { max_ms: 500 },
                               module: "/ui/extensions/vib/vib.js?v=1" },
                             { name: "flou", spec: fn("flou", "x"), module: "/ui/extensions/flou/flou.js?v=1" }]);
  declared.families = [{ key: "vib", label: "Vibreur", tools: ["vibrer"] },
                       { key: "flou", label: "Flou", tools: ["flou"] }];
  const modules = {
    "/ui/extensions/vib/vib.js?v=1": { default: (ext) => ({ tools: [new (class extends ext.PageTool {
      constructor() { super({ name: "vibrer" }); }
      /** @override */
      run() { return `vibré ${this.settings.max_ms} ms`; }
    })()] }) },
    // Une exécution d'un autre nom que celui que l'extension déclare.
    "/ui/extensions/flou/flou.js?v=1": { default: (ext) => ({ tools: [new (class extends ext.PageTool {
      constructor() { super({ name: "autre" }); }
      /** @override */
      run() { return "x"; }
    })()] }) },
  };
  const errors = await tools.installAll(declared, async (url) => modules[url]);
  assert.ok(errors.some((e) => e.includes("« flou » : sans exécution")), errors.join("; "));
  assert.deepEqual(tools.FAMILIES.map((f) => f.key), ["vib"]);
  assert.deepEqual((await tools.run("vibrer", "{}", {})).result, "vibré 500 ms");
  assert.equal(tools.specs({}, ["vib"])[0].function.description, "Fait vibrer.");
  const failed = await tools.installAll(declared, async () => { throw new Error("réseau"); });
  assert.ok(failed.some((e) => e.includes("réseau")) && !tools.isTool("vibrer"));
});

test("une carte d'un outil non installé s'affiche encore, mais ses boutons ne font rien", async () => {
  tools.install(page([{ key: "context", label: "Date", tools: ["device_context"] }]));
  assert.ok(!tools.isTool("speak"), "speak n'est pas déclaré : refusé par l'admin");
  const kept = { id: "k", name: "speak", text: "bonjour" };
  assert.equal(tools.keptView(kept).title, "Lu à voix haute", "l'exécution connue rend toujours la carte");
  assert.equal(await tools.keptAct(kept, "say", {}), "Outil indisponible sur ce proxy.");
  assert.equal(tools.keptView({ id: "k", name: "inconnu" }).title, "Outil indisponible sur ce proxy");
  assert.equal(tools.placement({ name: "ask_user" }), "sheet", "l'exécution reste connue");
  assert.equal(tools.view({ name: "inconnu" }).actions.length, 0);
  await assert.rejects(tools.act({ name: "ask_user" }, "x", {}), /indisponible/);
});

test("installAll : une famille non déclarée, ou une installation dépassée, n'installe rien", async () => {
  const declared = { families: [{ key: "vib", label: "Vibreur", tools: ["vibrer2"] }],
    tools: [{ name: "vibrer2", spec: fn("vibrer2", "x"), module: "/m.js" }] };
  const mod = (key) => ({ default: (ext) => {
    const t = new (class extends ext.PageTool { constructor() { super({ name: "vibrer2" }); } run() { return "ok"; } })();
    return { tools: [t], families: [{ key, tools: [t] }] };
  } });
  const errors = await tools.installAll(declared, async () => mod("autre"));
  assert.ok(errors.some((e) => e.includes("« autre » : non déclarée")) && !tools.isTool("vibrer2"));
  let release;
  const slow = tools.installAll(declared, () => new Promise((r) => { release = () => r(mod("vib")); }));
  await tools.installAll({ families: [], tools: [] }, async () => null);   // une session plus récente
  release();
  assert.ok((await slow).includes("installation dépassée par une plus récente"));
  assert.ok(!tools.isTool("vibrer2"));
});
