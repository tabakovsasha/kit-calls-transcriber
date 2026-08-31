/**
 * Reusable realtime layer over the backend websocket.
 *
 * Handshake, per the backend contract in app/api/routers/ws.py:
 *   POST /api/auth/ws-ticket  -> short-lived HMAC ticket (TTL 60s)
 *   WS   /ws?ticket=...       -> owner-scoped event stream
 *
 * Rules this hook enforces:
 * - a *new* ticket is minted for every connect attempt, including reconnects,
 *   because a ticket expires quickly and is bound to the live session;
 * - reconnect uses bounded exponential backoff with jitter, so a backend
 *   restart does not turn into a reconnect storm;
 * - unmount or logout cancels timers, stops reconnecting and closes the socket;
 * - the ticket is kept out of React state and never logged.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { authApi } from "../api/endpoints";

const BASE_DELAY_MS = 1000;
const MAX_DELAY_MS = 30000;
const PING_INTERVAL_MS = 25000;
// Close code the backend uses to reject a handshake it will not accept.
const WS_POLICY_VIOLATION = 1008;

function socketUrl(ticket) {
  // Same origin as the page, so the Vite proxy (dev) or the reverse proxy
  // (later) decides where /ws actually lands. No host is hardcoded.
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${window.location.host}/ws?ticket=${encodeURIComponent(ticket)}`;
}

export function useEventSocket({ enabled, onEvent }) {
  const [state, setState] = useState("idle");
  const [lastEventAt, setLastEventAt] = useState(null);

  const socketRef = useRef(null);
  const reconnectTimerRef = useRef(null);
  const pingTimerRef = useRef(null);
  const attemptRef = useRef(0);
  // Guards every async continuation: once false, nothing may reconnect.
  const activeRef = useRef(false);
  // Kept in a ref so a changing callback does not force a reconnect.
  const onEventRef = useRef(onEvent);

  useEffect(() => {
    onEventRef.current = onEvent;
  }, [onEvent]);

  const clearTimers = useCallback(() => {
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    if (pingTimerRef.current) {
      clearInterval(pingTimerRef.current);
      pingTimerRef.current = null;
    }
  }, []);

  const teardown = useCallback(() => {
    clearTimers();
    const socket = socketRef.current;
    socketRef.current = null;
    if (socket) {
      // Drop handlers first: a close we asked for must not trigger reconnect.
      socket.onopen = null;
      socket.onmessage = null;
      socket.onerror = null;
      socket.onclose = null;
      if (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING) {
        socket.close(1000, "client shutdown");
      }
    }
  }, [clearTimers]);

  const connect = useCallback(async () => {
    if (!activeRef.current) return;

    setState(attemptRef.current === 0 ? "connecting" : "reconnecting");

    let ticket;
    try {
      // Fresh ticket per attempt; also fails fast when the session is gone.
      const response = await authApi.wsTicket();
      ticket = response.ticket;
    } catch {
      scheduleReconnect();
      return;
    }

    if (!activeRef.current) return;

    let socket;
    try {
      socket = new WebSocket(socketUrl(ticket));
    } catch {
      scheduleReconnect();
      return;
    }
    socketRef.current = socket;

    socket.onopen = () => {
      if (!activeRef.current) {
        socket.close(1000, "no longer active");
        return;
      }
      attemptRef.current = 0;
      setState("open");

      // The backend answers "ping" with {"type":"pong"}; this keeps idle
      // proxies from dropping the connection.
      pingTimerRef.current = setInterval(() => {
        if (socket.readyState === WebSocket.OPEN) socket.send("ping");
      }, PING_INTERVAL_MS);
    };

    socket.onmessage = (event) => {
      let payload;
      try {
        payload = JSON.parse(event.data);
      } catch {
        return;
      }
      if (payload?.type === "pong") return;
      setLastEventAt(new Date());
      if (onEventRef.current) onEventRef.current(payload);
    };

    socket.onerror = () => {
      // onclose always follows; reconnect is handled there only.
    };

    socket.onclose = (event) => {
      clearTimers();
      socketRef.current = null;
      if (!activeRef.current) return;

      if (event.code === WS_POLICY_VIOLATION) {
        // The server refused this credential. Retrying with another ticket of
        // the same session would just repeat the rejection.
        setState("rejected");
        return;
      }
      scheduleReconnect();
    };

    function scheduleReconnect() {
      if (!activeRef.current) return;
      const attempt = attemptRef.current;
      attemptRef.current = attempt + 1;
      const capped = Math.min(BASE_DELAY_MS * 2 ** attempt, MAX_DELAY_MS);
      // Jitter spreads reconnects when many tabs wake up together.
      const delay = capped / 2 + Math.random() * (capped / 2);
      setState("reconnecting");
      reconnectTimerRef.current = setTimeout(connect, delay);
    }
  }, [clearTimers]);

  useEffect(() => {
    if (!enabled) {
      activeRef.current = false;
      teardown();
      setState("idle");
      return undefined;
    }

    activeRef.current = true;
    attemptRef.current = 0;
    connect();

    return () => {
      activeRef.current = false;
      teardown();
    };
  }, [enabled, connect, teardown]);

  return { state, lastEventAt };
}
