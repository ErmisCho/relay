/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** "mock" serves the in-browser scripted backend; anything else uses the Delegator's /demo API. */
  readonly VITE_API_MODE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
