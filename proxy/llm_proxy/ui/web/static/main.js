// @ts-check
/*
  L'entrée de la page (index.html) : l'application (App.vue), et les
  composants que plusieurs vues nomment sans les importer — le chat et la
  mémoire (aussi dans Configuration → Mémoire).
*/
import { createApp } from "vue";
import App from "./App.vue";
import ChatPanel from "./chat/ChatPanel.vue";
import MemoryPanel from "./memory/MemoryPanel.vue";

createApp(App)
  .component("memory-panel", MemoryPanel)
  .component("chat-panel", ChatPanel)
  .mount("#app");
