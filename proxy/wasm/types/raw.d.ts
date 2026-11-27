// Imports bruts de Vite (`import txt from "./fichier?raw"`).
declare module "*?raw" {
  const content: string;
  export default content;
}
