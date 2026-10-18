// Le REGISTRE des outils de la page (web/static/chat/tools), chargé hors
// du navigateur : il vérifie au chargement chaque outil contre son
// interface (PageTool) et les listes TOOL et FAMILY dans les deux sens
// (tools/index.js). Une dérive fait échouer ce script — et `npm run
// check`, et pytest.
//
// Les modules touchent au navigateur en se chargeant (Vue, document,
// navigator) : le minimum est simulé ici, rien n'est exécuté.
const noop = () => {};
globalThis.window = globalThis;
globalThis.document = { addEventListener: noop, hidden: false, createElement: () => ({}) };
globalThis.Vue = new Proxy({}, { get: () => (v) => ({ value: v, run: (f) => f() }) });
globalThis.VueUse = new Proxy({}, { get: () => () => new Proxy({}, { get: () => ({ value: false }) }) });
if (!globalThis.navigator) globalThis.navigator = {};
globalThis.location = { href: "http://localhost/ui" };

const tools = await import("../llm_proxy/web/static/chat/tools/index.js");
const names = tools.FAMILIES.flatMap((f) => f.tools.map((t) => t.name));
console.log(`Outils de la page : ${tools.FAMILIES.length} familles, ${names.length} outils — conformes.`);
