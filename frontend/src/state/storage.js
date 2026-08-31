/**
 * Versioned, corruption-tolerant browser storage for non-secret UI state.
 *
 * Scope of what may live here: filters, column choices, normalized call rows
 * already rendered in the table, a pagination cursor. Nothing authenticating or
 * authorizing may be written through this module — no JWT, no refresh token, no
 * ws-ticket, no Voximplant token, no signed audio URL, no upstream record_url.
 * Access tokens stay in memory and the refresh token stays in an HttpOnly
 * cookie, so a persisted UI cache can never widen the blast radius of XSS.
 *
 * Every key carries a version. Bumping VERSION retires old payloads instead of
 * feeding a changed shape into components, which is cheaper and safer than
 * writing migrations for throwaway UI state.
 */

import { useCallback, useEffect, useRef, useState } from "react";

const NAMESPACE = "kit";
const VERSION = "v1";

/** All UI state keys share this prefix, which makes logout cleanup exact. */
export const UI_PREFIX = `${NAMESPACE}.ui.${VERSION}.`;

/** `kit.ui.v1.callsState.<connectionId>` and friends. */
export function uiKey(name, scope = null) {
  return scope ? `${UI_PREFIX}${name}.${scope}` : `${UI_PREFIX}${name}`;
}

/**
 * Read and JSON-parse a key.
 *
 * A corrupted or hand-edited value must never break the app, so any parse
 * failure is treated as "no value" and the bad entry is dropped.
 */
export function readJson(key, fallback = null) {
  let raw;
  try {
    raw = window.localStorage.getItem(key);
  } catch {
    // Private mode or storage disabled: behave like an empty cache.
    return fallback;
  }
  if (raw === null) return fallback;

  try {
    const parsed = JSON.parse(raw);
    return parsed === null || parsed === undefined ? fallback : parsed;
  } catch {
    try {
      window.localStorage.removeItem(key);
    } catch {
      // Nothing else to do; the fallback keeps the UI usable.
    }
    return fallback;
  }
}

/** Serialize and write a key. Quota errors are non-fatal by design. */
export function writeJson(key, value) {
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
    return true;
  } catch {
    // QuotaExceededError or blocked storage: the session continues in memory.
    return false;
  }
}

export function removeKey(key) {
  try {
    window.localStorage.removeItem(key);
  } catch {
    // Ignore: nothing to clean up if storage is unavailable.
  }
}

/**
 * `useState` that survives navigation and reload.
 *
 * `key` may be null, which yields plain in-memory state. That matters for
 * connection-scoped state: before a connection is picked there is nothing
 * meaningful to persist, and writing to a "null" bucket would leak one
 * connection's rows into another's.
 *
 * `sanitize` runs on the value read from storage. It is the single place where a
 * stale or hostile payload is forced back into a known shape.
 */
export function usePersistedState(key, defaultValue, sanitize) {
  const makeDefault = useCallback(
    () => (typeof defaultValue === "function" ? defaultValue() : defaultValue),
    [defaultValue],
  );

  const load = useCallback(() => {
    if (!key) return makeDefault();
    const stored = readJson(key, null);
    if (stored === null) return makeDefault();
    if (!sanitize) return stored;
    try {
      return sanitize(stored, makeDefault());
    } catch {
      return makeDefault();
    }
  }, [key, makeDefault, sanitize]);

  const [value, setValue] = useState(load);

  // Re-read when the key changes (e.g. the user switched connection), so each
  // scope shows its own state instead of the previous scope's leftovers.
  const activeKey = useRef(key);
  useEffect(() => {
    if (activeKey.current === key) return;
    activeKey.current = key;
    setValue(load());
  }, [key, load]);

  useEffect(() => {
    if (!key) return;
    writeJson(key, value);
  }, [key, value]);

  return [value, setValue];
}

/**
 * Drop every UI-state key this module owns.
 *
 * Called on logout so the next user on the same browser profile never sees the
 * previous user's call rows or filters. Keys outside UI_PREFIX (such as the
 * selected connection id) are removed by their own owners.
 */
export function clearUiState() {
  try {
    const doomed = [];
    for (let i = 0; i < window.localStorage.length; i += 1) {
      const key = window.localStorage.key(i);
      if (key && key.startsWith(UI_PREFIX)) doomed.push(key);
    }
    doomed.forEach((key) => window.localStorage.removeItem(key));
    return doomed.length;
  } catch {
    return 0;
  }
}
