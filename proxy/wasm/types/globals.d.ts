// Ce que la page trouve en global et que lib.dom ne décrit pas : la
// vérification des types du code de la page (tsconfig.json).

// Vue 3, chargé par <script> (vue.global.prod.js), sans types.
declare const Vue: any;

interface Window {
  EyeDropper?: any;
  BarcodeDetector?: any;
  showDirectoryPicker?: (options?: object) => Promise<any>;
}
