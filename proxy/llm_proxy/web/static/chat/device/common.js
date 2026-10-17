// Ce que partagent les familles d'outils du navigateur.
export const fn = (name, description, properties = {}, required = []) => ({
  type: "function", function: { name, description,
    parameters: { type: "object", properties, ...(required.length ? { required } : {}) } } });

export const refused = (what) => ({ result: `L'utilisateur a refusé ${what}.` });
export const REFUSE = { id: "refuse", label: "Refuser" };
export const clip = (s, n) => (s.length > n ? s.slice(0, n) + `\n… (${s.length - n} caractères coupés)` : s);
