/*
  Les modèles DU NAVIGATEUR : ceux que le navigateur embarque. Chacun a
  son module (chat/models/ : gemini-nano.js), qui le DÉCRIT comme une
  entrée du catalogue, avec ce qu'il sait faire et sa façon de répondre ;
  le chat ne connaît que cette description (MODEL_CAPS, modelCaps),
  jamais un modèle par son nom.

    BROWSER_MODELS              leurs descriptions
    browserModel(id)            la description d'un identifiant, ou null
    availableBrowserModels()    ceux que ce navigateur a (ou peut
                                télécharger), en entrées de la liste

  Une description :
    id, name, group     l'identifiant, et ce qu'affiche la liste
    vision              lit les images
    byDefault: false    jamais choisi d'office pour une conversation
    caps                ce qu'il sait faire, à côté de MODEL_CAPS (tout
                        ce qu'il ne dit pas lui est accordé)
    answer(messages, live, signal, onStatus)
                        répond sur l'appareil : les messages de l'API
                        (images en data URL) → son texte, écrit à mesure
                        dans live.content ; rend les arguments d'ask_user
                        ({questions}) s'il en pose, sinon null
    available()         ce que le navigateur en dit : « available »,
                        « downloadable », « downloading » ou
                        « unavailable »
*/
import { GEMINI_NANO } from "./models/gemini-nano.js";

// Ce que sait faire un modèle du catalogue (du proxy) : tout.
export const MODEL_CAPS = {
  hostedTools: true,      // les outils du proxy (recherche, mémoire…)
  pageTools: null,        // les outils de la page : null = tous, sinon leurs noms
  reasoning: true,        // le niveau de réflexion
  folder: true,           // un dossier à parcourir
};

// Ce que sait faire `entry` (une entrée de la liste, ou rien).
export const modelCaps = (entry) => ({ ...MODEL_CAPS, ...((entry && entry.caps) || {}) });

export const BROWSER_MODELS = [GEMINI_NANO];

export const browserModel = (id) => BROWSER_MODELS.find((m) => m.id === id) || null;

// Ceux que ce navigateur a : `availability` dit s'il faut d'abord les
// télécharger (« downloadable », « downloading »).
export async function availableBrowserModels() {
  const out = [];
  for (const m of BROWSER_MODELS) {
    let got = "unavailable";
    try { got = await m.available(); } catch { /* API absente ou refusée */ }
    if (got !== "unavailable") out.push({ ...m, browser: true, availability: got });
  }
  return out;
}
