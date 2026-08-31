/**
 * Session state for the whole app.
 *
 * Lifecycle on mount: try `POST /api/auth/refresh`. If the HttpOnly refresh
 * cookie is still valid the backend returns a fresh access token plus the user,
 * which is how a page reload stays logged in without any token in web storage.
 * If it fails, the app simply shows the login screen.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

import {
  ApiError,
  clearAccessToken,
  refreshSession,
  setAccessToken,
  setSessionLostHandler,
} from "../api/client";
import { authApi } from "../api/endpoints";
import { clearUiState } from "../state/storage";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [status, setStatus] = useState("restoring");

  // Restore the session once, on first mount.
  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        const data = await refreshSession();
        if (!cancelled) {
          setUser(data.user);
          setStatus("authenticated");
        }
      } catch {
        // No usable refresh cookie: an anonymous visitor, not an error.
        if (!cancelled) {
          clearAccessToken();
          setStatus("anonymous");
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, []);

  // The API client calls this when a refresh attempt fails mid-session.
  useEffect(() => {
    setSessionLostHandler(() => {
      setUser(null);
      setStatus("anonymous");
    });
    return () => setSessionLostHandler(null);
  }, []);

  const login = useCallback(async (email, password) => {
    const data = await authApi.login(email, password);
    setAccessToken(data.access_token);
    setUser(data.user);
    setStatus("authenticated");
    return data.user;
  }, []);

  const logout = useCallback(async () => {
    try {
      // Revokes the session server-side and clears the refresh cookie.
      await authApi.logout();
    } catch {
      // A failed logout must not trap the user in an authenticated shell.
    } finally {
      clearAccessToken();
      // Drop this user's cached UI state (Calls rows, filters, queue table
      // preferences) so the next login on this browser starts clean.
      clearUiState();
      setUser(null);
      setStatus("anonymous");
    }
  }, []);

  const refreshUser = useCallback(async () => {
    const fresh = await authApi.me();
    setUser(fresh);
    return fresh;
  }, []);

  /**
   * Forced first-login password change.
   *
   * The backend revokes every other session and clears must_change_password.
   * The current session stays valid, so re-reading /auth/me is enough to leave
   * the change-password screen.
   */
  const changePassword = useCallback(
    async (currentPassword, newPassword) => {
      await authApi.changePassword(currentPassword, newPassword);
      try {
        return await refreshUser();
      } catch (error) {
        // If the current session was invalidated too, fall back to a clean login.
        if (error instanceof ApiError && error.isAuthFailure) {
          clearAccessToken();
          setUser(null);
          setStatus("anonymous");
        }
        throw error;
      }
    },
    [refreshUser],
  );

  const value = useMemo(
    () => ({
      user,
      status,
      isAuthenticated: status === "authenticated" && Boolean(user),
      mustChangePassword: Boolean(user?.must_change_password),
      login,
      logout,
      refreshUser,
      changePassword,
    }),
    [user, status, login, logout, refreshUser, changePassword],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used inside AuthProvider");
  return context;
}
