// Ce que la page trouve en global et que lib.dom ne décrit pas : la
// vérification des types du code de la page (tsconfig.json).

interface Window {
  EyeDropper?: any;
  BarcodeDetector?: any;
  showDirectoryPicker?: (options?: object) => Promise<any>;
}
