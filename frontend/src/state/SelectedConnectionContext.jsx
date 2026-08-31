/**
 * Which connection the user is working with.
 *
 * Only the connection *id* is remembered, and it is a non-secret server-side
 * identifier. Credentials (token, domain, api_host) stay on the backend, unlike
 * the legacy frontend which kept the Voximplant token in localStorage.
 */

import { createContext, useCallback, useContext, useMemo, useState } from "react";

const STORAGE_KEY = "kit.selectedConnectionId";

const SelectedConnectionContext = createContext(null);

export function SelectedConnectionProvider({ children }) {
  const [selectedId, setSelectedId] = useState(() => {
    try {
      return window.localStorage.getItem(STORAGE_KEY) || null;
    } catch {
      return null;
    }
  });

  const select = useCallback((id) => {
    setSelectedId(id);
    try {
      if (id) window.localStorage.setItem(STORAGE_KEY, id);
      else window.localStorage.removeItem(STORAGE_KEY);
    } catch {
      // Private mode or blocked storage: selection stays in memory only.
    }
  }, []);

  const value = useMemo(() => ({ selectedId, select }), [selectedId, select]);

  return (
    <SelectedConnectionContext.Provider value={value}>
      {children}
    </SelectedConnectionContext.Provider>
  );
}

export function useSelectedConnection() {
  const context = useContext(SelectedConnectionContext);
  if (!context) {
    throw new Error("useSelectedConnection must be used inside SelectedConnectionProvider");
  }
  return context;
}
