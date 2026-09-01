/**
 * Typed wrappers over the backend endpoints the app actually uses.
 *
 * Grouped by backend router so the shape of the REST surface stays visible in
 * one file instead of being spread across components.
 */

import { api } from "./client";

export const authApi = {
  login: (email, password) =>
    api.post("/auth/login", { email, password }, { auth: false }),
  logout: () => api.post("/auth/logout", undefined, { auth: false }),
  me: () => api.get("/auth/me"),
  changePassword: (currentPassword, newPassword) =>
    api.post("/auth/change-password", {
      current_password: currentPassword,
      new_password: newPassword,
    }),
  wsTicket: () => api.post("/auth/ws-ticket"),
  sessions: () => api.get("/auth/sessions"),
};

export const callsApi = {
  /**
   * Search call history for one connection.
   *
   * `cursor` is an opaque forward-only token issued by the upstream history API
   * and passed through by the backend; it goes in the query string because the
   * body carries the filter set.
   */
  search: (filters, cursor) => {
    const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
    return api.post(`/calls/search${query}`, filters);
  },
  scenarios: (connectionId) =>
    api.get(`/calls/scenarios?connection_id=${encodeURIComponent(connectionId)}`),
  // Mints a fresh short-lived signed playback URL. The upstream record URL
  // (which embeds an access token) never reaches the browser.
  audioUrl: (connectionId, callId) =>
    api.post("/calls/audio-url", { connection_id: connectionId, call_id: callId }),
};

export const queueApi = {
  get: (connectionId) =>
    api.get(
      connectionId
        ? `/queue?connection_id=${encodeURIComponent(connectionId)}`
        : "/queue",
    ),
  // whisper_model is intentionally omitted: the backend applies its configured
  // default, and model management is not part of this stage.
  add: (connectionId, callIds) =>
    api.post("/queue/add", { connection_id: connectionId, call_ids: callIds }),
  // Requeue failed/skipped items.
  retry: () => api.post("/queue/retry"),
  // Cancel all QUEUED items (processing ones finish on their own).
  stop: () => api.post("/queue/stop"),
  // Remove terminal items (done/failed/skipped/canceled).
  clear: (statuses) => api.post("/queue/clear", { statuses }),
  // Cancel one queued item. Rejected by the backend for any other status.
  cancelItem: (itemId) => api.post(`/queue/${itemId}/cancel`),
  // Requeue one failed/skipped/canceled item.
  retryItem: (itemId) => api.post(`/queue/${itemId}/retry`),
  // Delete one queue item by ID (must not be PROCESSING).
  deleteItem: (itemId) => api.del(`/queue/${itemId}`),
};

export const connectionsApi = {
  list: () => api.get("/connections"),
  create: ({ label, apiHost, domain, accessToken, isDefault, verify }) =>
    api.post("/connections", {
      label,
      api_host: apiHost,
      domain,
      access_token: accessToken,
      is_default: Boolean(isDefault),
      verify: verify !== false,
    }),
  // Only non-secret fields; the token is rotated through its own endpoint.
  update: (id, patch) => api.put(`/connections/${id}`, patch),
  remove: (id) => api.del(`/connections/${id}`),
  verify: (id) => api.post(`/connections/${id}/verify`),
  rotateToken: (id, accessToken, verify = true) =>
    api.post(`/connections/${id}/rotate-token`, {
      access_token: accessToken,
      verify,
    }),
};

export const settingsApi = {
  hardware: () => api.get("/settings/hardware"),
  models: () => api.get("/settings/whisper/models"),
  downloadModel: (modelName) => api.post(`/settings/whisper/models/${modelName}/download`),
  deleteModel: (modelName) => api.del(`/settings/whisper/models/${modelName}`),
  current: () => api.get("/settings/current"),
  setDefaultModel: (whisperModel) => api.put("/settings/default-model", { whisper_model: whisperModel }),
  setProfile: (profile) => api.put("/settings/performance-profile", { profile }),
};
