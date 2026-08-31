import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

/**
 * Dev server config.
 *
 * The application code never names a host: it always calls relative `/api/*`
 * and `/ws`, and this proxy is the only place that knows where the backend
 * lives. That keeps a single browser origin, so the HttpOnly refresh cookie
 * (path=/api/auth) is a same-origin cookie and CORS never enters the picture.
 *
 * `host: true` binds 0.0.0.0 so the dev server is reachable from another
 * machine on the LAN, not only through loopback on the server itself.
 */
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const backend = env.BACKEND_ORIGIN || "http://127.0.0.1:8000";

  return {
    plugins: [react()],
    server: {
      host: true,
      port: Number(env.FRONTEND_PORT || 5173),
      strictPort: true,
      proxy: {
        "/api": { target: backend, changeOrigin: false },
        "/ws": { target: backend, ws: true, changeOrigin: false },
      },
    },
    preview: {
      host: true,
      port: Number(env.FRONTEND_PORT || 5173),
    },
  };
});
