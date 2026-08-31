/**
 * One websocket for the whole authenticated app, with fan-out to subscribers.
 *
 * Why a context instead of calling `useEventSocket` per page: each call to that
 * hook opens its own socket. The shell needs the connection status and pages
 * need the payloads, so the socket is opened once here and every listener is
 * invoked from a single `onEvent`.
 *
 * Subscribers are kept in a ref-held Set, so adding or removing one never
 * re-renders the provider and therefore never reconnects the socket.
 */

import { createContext, useCallback, useContext, useMemo, useRef } from "react";

import { useEventSocket } from "./useEventSocket";

const EventsContext = createContext(null);

export function EventsProvider({ children }) {
  const listenersRef = useRef(new Set());

  const handleEvent = useCallback((payload) => {
    // A throwing listener must not take down the socket handler, so each one
    // is isolated.
    listenersRef.current.forEach((listener) => {
      try {
        listener(payload);
      } catch {
        // Ignore: a broken subscriber is not a transport failure.
      }
    });
  }, []);

  const { state, lastEventAt } = useEventSocket({ enabled: true, onEvent: handleEvent });

  /** Register a listener. Returns the unsubscribe function. */
  const subscribe = useCallback((listener) => {
    listenersRef.current.add(listener);
    return () => listenersRef.current.delete(listener);
  }, []);

  const value = useMemo(
    () => ({ socketState: state, lastEventAt, subscribe }),
    [state, lastEventAt, subscribe],
  );

  return <EventsContext.Provider value={value}>{children}</EventsContext.Provider>;
}

export function useEvents() {
  const context = useContext(EventsContext);
  if (!context) throw new Error("useEvents must be used inside EventsProvider");
  return context;
}
