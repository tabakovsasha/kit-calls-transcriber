/**
 * Central API layer. Every request in the app goes through here.
 *
 * Responsibilities:
 * - one place that knows the `/api` base path (relative, so the dev proxy or
 *   the production reverse proxy decides the real host);
 * - carries the in-memory access token as a Bearer header;
 * - decodes the backend error envelope {code, message, details} into ApiError;
 * - refreshes the access token once on an expired-token 401 and replays the
 *   original request, with all concurrent callers sharing one refresh.
 *
 * Nothing here logs a token, a password or a cookie.
 */

const API_BASE = "/api";

/** Codes the backend returns when the access token is no longer usable. */
const REFRESHABLE_CODES = new Set(["TOKEN_EXPIRED", "UNAUTHENTICATED"]);

export class ApiError extends Error {
  constructor(status, code, message, details) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }

  /** The current session is gone and the user has to log in again. */
  get isAuthFailure() {
    return this.status === 401 || this.code === "SESSION_REVOKED";
  }

  /** Backend refuses until the forced password change is done. */
  get isPasswordChangeRequired() {
    return this.code === "PASSWORD_CHANGE_REQUIRED";
  }
}

// Access token lives in a module-scoped variable only: never in localStorage,
// never in sessionStorage. A page reload deliberately loses it and the session
// is restored through the refresh cookie instead.
let accessToken = null;
let onSessionLost = null;

// Single-flight refresh: the first 401 starts it, everyone else awaits it.
let refreshPromise = null;

export function setAccessToken(token) {
  accessToken = token;
}

export function getAccessToken() {
  return accessToken;
}

export function clearAccessToken() {
  accessToken = null;
}

/** Registered by the auth provider so the client can force a logout state. */
export function setSessionLostHandler(handler) {
  onSessionLost = handler;
}

async function parseBody(response) {
  if (response.status === 204) return null;
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function toApiError(response, body) {
  if (body && typeof body === "object" && typeof body.code === "string") {
    return new ApiError(response.status, body.code, body.message, body.details);
  }
  return new ApiError(
    response.status,
    `HTTP_${response.status}`,
    typeof body === "string" && body ? body : `Ошибка запроса (${response.status})`,
  );
}

async function rawRequest(path, { method = "GET", body, auth = true, signal } = {}) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (auth && accessToken) headers.Authorization = `Bearer ${accessToken}`;

  const response = await fetch(`${API_BASE}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    // Required so the HttpOnly refresh cookie is sent on /api/auth/* calls.
    credentials: "include",
    signal,
  });

  const payload = await parseBody(response);
  if (!response.ok) throw toApiError(response, payload);
  return payload;
}

/**
 * Exchange the refresh cookie for a new access token.
 *
 * The cookie is HttpOnly: JavaScript cannot read it and does not try to. The
 * browser attaches it because the request is same-origin and inside the
 * cookie's /api/auth path.
 */
async function runRefresh() {
  const data = await rawRequest("/auth/refresh", { method: "POST", auth: false });
  setAccessToken(data.access_token);
  return data;
}

export function refreshSession() {
  if (!refreshPromise) {
    refreshPromise = runRefresh().finally(() => {
      refreshPromise = null;
    });
  }
  return refreshPromise;
}

export async function request(path, options = {}) {
  try {
    return await rawRequest(path, options);
  } catch (error) {
    const canRetry =
      error instanceof ApiError &&
      error.status === 401 &&
      REFRESHABLE_CODES.has(error.code) &&
      options.auth !== false &&
      !options.isRetry;

    if (!canRetry) throw error;

    try {
      await refreshSession();
    } catch (refreshError) {
      // Refresh itself failed: the session is unrecoverable.
      clearAccessToken();
      if (onSessionLost) onSessionLost();
      throw refreshError;
    }

    return rawRequest(path, { ...options, isRetry: true });
  }
}

export const api = {
  get: (path, options) => request(path, { ...options, method: "GET" }),
  post: (path, body, options) => request(path, { ...options, method: "POST", body }),
  put: (path, body, options) => request(path, { ...options, method: "PUT", body }),
  del: (path, options) => request(path, { ...options, method: "DELETE" }),
};
