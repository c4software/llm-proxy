/*
  Les outils que la PAGE exécute (le proxy les rend en `tool_calls`, comme
  à tout client) — leur registre :

    ask_user                            ask.js        attend l'utilisateur
    folder_list, folder_read,           workspace.js  si un dossier est ouvert
      folder_search
    folder_write, folder_edit           workspace.js  attendent son accord
    save_file, show_artifact            outputs.js
    device_context, notify              device.js     notify : son accord, s'il
                                                      n'a pas encore permis
    get_location                        device.js     attend son accord

  Deux familles : ceux qui s'exécutent aussitôt (run), et ceux qui
  ATTENDENT l'utilisateur (prepare → une carte dans le fil, m.waiting ;
  sa réponse devient leur résultat). La boucle de ChatPanel.respond
  s'arrête sur une attente et reprend à la réponse — même après un
  rechargement : tout l'état est dans le message.
*/
import * as ask from "./ask.js";
import * as outputs from "./outputs.js";
import * as workspace from "./workspace.js";
import * as device from "./device.js";

// Les déclarations pour une conversation : `ws`, son dossier ouvert.
export function toolsFor(ws) {
  return [ask.TOOL, ...outputs.TOOLS, ...device.TOOLS,
          ...(ws ? [...workspace.TOOLS, ...workspace.WRITE_TOOLS] : [])];
}

export const isPageTool = (name) => name === "ask_user" || outputs.TOOL_NAMES.has(name)
  || device.TOOL_NAMES.has(name) || workspace.TOOL_NAMES.has(name) || workspace.WRITE_NAMES.has(name);
export const waitsForUser = (name) => name === "ask_user" || device.waits(name)
  || workspace.WRITE_NAMES.has(name);

const parse = (json) => { try { return json ? JSON.parse(json) : {}; } catch { return null; } };

// Un outil qui s'exécute aussitôt → {result, artifact?}.
export async function run(ctx, call) {
  const args = parse(call.arguments);
  if (args === null) return { result: "Error: arguments illisibles (JSON attendu)." };
  if (call.name === "save_file") return { result: outputs.saveFile(ctx.live, args) };
  if (call.name === "show_artifact") {
    const { id, result } = outputs.showArtifact(ctx.conv, args);
    return { result, artifact: id };
  }
  if (device.TOOL_NAMES.has(call.name)) return { result: device.run(call.name, args) };
  if (workspace.TOOL_NAMES.has(call.name)) {
    if (!ctx.ws) return { result: "Error: aucun dossier ouvert (l'utilisateur doit le rouvrir)." };
    return { result: await workspace.run(ctx.ws, call.name, call.arguments) };
  }
  return { result: `Error: outil inconnu « ${call.name} ».` };
}

// Un outil qui attend l'utilisateur → la carte ({kind, …}), ou {error}.
export async function prepare(ctx, call) {
  const args = parse(call.arguments);
  if (args === null) return { error: "Error: arguments illisibles (JSON attendu)." };
  if (call.name === "ask_user") return ask.prepare(args);
  if (device.TOOL_NAMES.has(call.name)) return device.prepare(call.name, args);
  if (!ctx.ws) return { error: "Error: aucun dossier ouvert (l'utilisateur doit le rouvrir)." };
  return workspace.prepareWrite(ctx.ws, call.name, args);
}
