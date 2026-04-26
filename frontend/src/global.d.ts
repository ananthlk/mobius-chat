declare global {
  interface Window {
    API_BASE?: string;
    /** RAG app base URL for "Open in new tab" (document reader). Set in index.html or build env. */
    RAG_APP_BASE?: string;
    /** RAG API base for direct downloads (e.g. same host as FastAPI `/documents/...`). Optional. */
    RAG_API_BASE?: string;
    /** Active chat thread id — written from inside the chat init scope so module-level
     * code (e.g. the email-thread feedback button) can read it without leaking the
     * inner ``currentThreadId`` variable. */
    __mobiusChatThreadId?: string | null;
  }
}

export {};
