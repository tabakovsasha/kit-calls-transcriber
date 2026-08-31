/**
 * Persisted Calls page state.
 *
 * Survives navigation (Calls → Queue → Calls) and reload (F5). Each connection
 * has its own isolated state so switching connections does not show stale rows
 * from the previous connection.
 *
 * The stored keys are:
 *   kit.ui.v1.callsState.<connectionId> → { filters, results, searchedAt }
 *
 * On logout every UI cache is cleared. Signed audio URLs and transcripts are
 * never persisted; audio URLs expire quickly and transcripts are re-fetched on
 * demand after reload.
 */

import { createContext, useCallback, useContext, useMemo } from "react";

import { uiKey, usePersistedState } from "./storage";

const CallsStateContext = createContext(null);

/** `YYYY-MM-DDTHH:MM` in local time. */
function toLocalInput(date) {
  const pad = (value) => String(value).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}`
  );
}

function defaultFilters() {
  const now = new Date();
  const dayAgo = new Date(now.getTime() - 24 * 60 * 60 * 1000);
  return {
    from_date: toLocalInput(dayAgo),
    to_date: toLocalInput(now),
    scenario_id: "",
    min_duration: 0,
    has_recording: true,
    records_limit: 50,
  };
}

/**
 * Force stored data back into a known shape.
 *
 * If the persisted blob is garbage or the schema changed, the fallback is a
 * clean slate. This is UI cache only, so discarding it on version mismatch is
 * cheaper than writing a migration.
 */
function sanitizeState(stored, fallbackFn) {
  const fallback = fallbackFn();
  if (!stored || typeof stored !== "object") return fallback;

  const filters = stored.filters && typeof stored.filters === "object"
    ? { ...defaultFilters(), ...stored.filters }
    : defaultFilters();

  const results =
    stored.results && typeof stored.results === "object" ? stored.results : null;

  // Limit the persisted result set so storage never holds tens of thousands of
  // calls. After a bulk load-more the next reload may lose the tail; that is
  // acceptable for UX, which trades infinite pagination for acceptable storage.
  const maxCalls = 500;
  if (results?.calls?.length > maxCalls) {
    results.calls = results.calls.slice(0, maxCalls);
    results.totalLoaded = Math.min(results.totalLoaded, maxCalls);
    results.canLoadMore = true;
  }

  return {
    filters,
    results,
    searchedAt: typeof stored.searchedAt === "number" ? stored.searchedAt : null,
  };
}

export function CallsStateProvider({ connectionId, children }) {
  const storageKey = connectionId ? uiKey("callsState", connectionId) : null;

  const [state, setState] = usePersistedState(
    storageKey,
    () => ({ filters: defaultFilters(), results: null, searchedAt: null }),
    sanitizeState,
  );

  const setFilters = useCallback((filters) => {
    setState((prev) => ({ ...prev, filters }));
  }, [setState]);

  const setResults = useCallback((results, cursor, canLoadMore, totalLoaded) => {
    setState((prev) => ({
      ...prev,
      results: { calls: results, cursor, canLoadMore, totalLoaded },
      searchedAt: Date.now(),
    }));
  }, [setState]);

  const appendResults = useCallback((newCalls, cursor, canLoadMore, totalLoaded) => {
    setState((prev) => {
      const existing = prev.results?.calls || [];
      return {
        ...prev,
        results: {
          calls: [...existing, ...newCalls],
          cursor,
          canLoadMore,
          totalLoaded,
        },
      };
    });
  }, [setState]);

  const updateCall = useCallback((callId, updates) => {
    setState((prev) => {
      if (!prev.results?.calls) return prev;
      return {
        ...prev,
        results: {
          ...prev.results,
          calls: prev.results.calls.map((call) =>
            call.id === callId ? { ...call, ...updates } : call
          ),
        },
      };
    });
  }, [setState]);

  const clearResults = useCallback(() => {
    setState((prev) => ({ ...prev, results: null, searchedAt: null }));
  }, [setState]);

  const value = useMemo(
    () => ({
      filters: state.filters,
      results: state.results,
      searchedAt: state.searchedAt,
      setFilters,
      setResults,
      appendResults,
      updateCall,
      clearResults,
    }),
    [
      state.filters,
      state.results,
      state.searchedAt,
      setFilters,
      setResults,
      appendResults,
      updateCall,
      clearResults,
    ]
  );

  return (
    <CallsStateContext.Provider value={value}>
      {children}
    </CallsStateContext.Provider>
  );
}

export function useCallsState() {
  const context = useContext(CallsStateContext);
  if (!context) {
    throw new Error("useCallsState must be used inside CallsStateProvider");
  }
  return context;
}
